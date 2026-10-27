import copy
from types import SimpleNamespace as NS
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import pytest
from kubernetes.client.rest import ApiException
from app.api import ingest
from app.models import Job, Message, now
from app.controller import reconcile
from test_api import payload, audio
from flask import g

TEMPLATE = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {}, "spec": {"ttlSecondsAfterFinished": 300, "template": {"spec": {"containers": [{"name": "worker", "image": "test", "env": [{"name": "INTERNAL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "secret", "key": "internal"}}}]}]}}}}

class FakeKubernetes:
    def __init__(self): self.jobs, self.creates, self.patches = {}, [], []
    def read_namespaced_job(self, name, namespace):
        if name not in self.jobs: raise ApiException(status=404)
        return self.jobs[name]
    def create_namespaced_job(self, namespace, body):
        self.creates.append(copy.deepcopy(body))
        name = body["metadata"]["name"]
        if name in self.jobs: raise ApiException(status=409)
        self.jobs[name] = NS(metadata=NS(labels=body["metadata"]["labels"]), status=NS(succeeded=None, failed=None))
    def patch_namespaced_job(self, name, namespace, patch): self.patches.append((name,patch))


def queued(application, count=2):
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        for i in range(count): ingest(payload(message_id="message-"+str(i), media=audio()))
        g.db.commit(); g.db.close()


def tick(application, api):
    reconcile(application.extensions["db_session"], api, "test", TEMPLATE, application.config["TOKEN_ENCRYPTION_KEY"])


def test_single_global_job_pending_and_crash_reconcile(application):
    queued(application)
    api = FakeKubernetes()
    tick(application, api); tick(application, api); tick(application, api)
    assert len(api.creates) == 1
    manifest = api.creates[0]
    assert "ttlSecondsAfterFinished" not in manifest["spec"]
    env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(x["name"] == "JOB_TOKEN" and len(x["value"]) >= 32 for x in env)
    assert any(x["name"] == "INTERNAL_TOKEN" and "valueFrom" in x for x in env)
    with application.extensions["db_session"]() as db:
        active = list(db.scalars(select(Job).where(Job.slot == 1)))
        assert len(active) == 1
        assert active[0].attempt == 1
        assert active[0].attempt_secret != next(x["value"] for x in env if x["name"] == "JOB_TOKEN")


def test_controller_preserves_worker_pod_scheduling_when_submitting_job(application):
    queued(application, 1)
    template = copy.deepcopy(TEMPLATE)
    placement = {"nodeSelector": {"example.invalid/workload": "transcription"},
                 "tolerations": [{"key": "example.invalid/workload", "operator": "Equal",
                                  "value": "transcription", "effect": "NoSchedule"}],
                 "affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                     {"weight": 1, "preference": {"matchExpressions": [
                         {"key": "kubernetes.io/arch", "operator": "In", "values": ["amd64"]}]}}]}}}
    template["spec"]["template"]["spec"].update(copy.deepcopy(placement))
    original = copy.deepcopy(template)
    api = FakeKubernetes()
    reconcile(application.extensions["db_session"], api, "test", template, application.config["TOKEN_ENCRYPTION_KEY"])
    pod = api.creates[0]["spec"]["template"]["spec"]
    assert {key: pod[key] for key in placement} == placement
    assert template == original


def test_completion_keeps_slot_until_kubernetes_terminal(application):
    queued(application); api = FakeKubernetes(); tick(application,api)
    with application.extensions["db_session"].begin() as db:
        job = db.scalar(select(Job).where(Job.slot == 1)); job.state = "completed"
    tick(application,api)
    assert len(api.creates) == 1
    name = api.creates[0]["metadata"]["name"]
    api.jobs[name].status.succeeded = 1
    tick(application,api); tick(application,api)
    assert len(api.creates) == 2
    assert api.patches[0][1] == {"spec": {"ttlSecondsAfterFinished": 300}}


def test_failed_worker_durable_retry_and_stale_token_replaced(application):
    queued(application,1); api = FakeKubernetes(); tick(application,api)
    name = api.creates[0]["metadata"]["name"]
    api.jobs[name].status.failed = 1
    with application.extensions["db_session"]() as db:
        original = db.scalar(select(Job)).attempt_token
    tick(application,api)
    with application.extensions["db_session"].begin() as db:
        job = db.scalar(select(Job)); assert job.state == "queued" and job.slot is None
        job.available_at = now()-1
    tick(application,api)
    assert len(api.creates) == 2
    assert api.creates[1]["metadata"]["name"] != name
    with application.extensions["db_session"]() as db: assert db.scalar(select(Job)).attempt_token != original


def test_database_enforces_singleton_even_without_controller_lock(application):
    queued(application)
    with pytest.raises(IntegrityError):
        with application.extensions["db_session"].begin() as db:
            for job in db.scalars(select(Job)): job.slot = 1


def test_cleanup_failure_is_durable_after_result_commit(application):
    from app.controller import schedule_cleanup
    from app.models import WorkerAttempt
    queued(application, 1); api = FakeKubernetes(); tick(application, api)
    name = api.creates[0]["metadata"]["name"]
    api.jobs[name].status.succeeded = 1
    with application.extensions["db_session"].begin() as db:
        job = db.scalar(select(Job)); job.state = "completed"
    original_patch = api.patch_namespaced_job
    def fail_patch(*a): raise ApiException(status=503)
    api.patch_namespaced_job = fail_patch
    with pytest.raises(ApiException): tick(application, api)
    with application.extensions["db_session"]() as db:
        job = db.scalar(select(Job)); attempt = db.get(WorkerAttempt, name)
        assert job.state == "completed" and job.slot is None
        assert attempt.terminal and not attempt.cleanup_scheduled
    api.patch_namespaced_job = original_patch
    schedule_cleanup(application.extensions["db_session"], api, "test")
    with application.extensions["db_session"]() as db: assert db.get(WorkerAttempt, name).cleanup_scheduled


def test_blocked_webhook_process_never_blocks_reconciliation_and_is_reaped(application, monkeypatch, tmp_path):
    import multiprocessing
    import time
    from contextlib import contextmanager
    from app import controller, events
    from app.models import Delivery
    from test_events import outbox
    outbox(application, monkeypatch)
    queued(application, 1)
    ready = tmp_path/'webhook-ready'
    @contextmanager
    def fake_lock(engine, lock_id=controller.LOCK_ID):
        assert lock_id == controller.WEBHOOK_LOCK_ID
        yield True
    def blocked(*args):
        ready.write_text('ready')
        time.sleep(30)
    monkeypatch.setattr(controller, 'controller_lock', fake_lock)
    monkeypatch.setattr(controller, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(events, 'pinned_post', blocked)
    before = {p.pid for p in multiprocessing.active_children()}
    dispatcher = controller.WebhookDispatcher()
    try:
        start = time.monotonic()
        dispatcher.poll(application.config)
        assert time.monotonic()-start < 1
        deadline = time.monotonic()+3
        while not ready.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert ready.exists()
        pid = dispatcher.process.pid
        api = FakeKubernetes()
        tick(application, api)
        assert len(api.creates) == 1
        dispatcher.poll(application.config)
        assert dispatcher.process.pid == pid  # Never add overlapping dispatcher children.
        with application.extensions['db_session']() as db:
            assert sum(item.attempts for item in db.scalars(select(Delivery))) == 1
        dispatcher.started_at -= controller.WEBHOOK_TICK_DEADLINE+1
        dispatcher.poll(application.config)
        assert dispatcher.process.pid != pid
        assert all(p.pid != pid for p in multiprocessing.active_children())
    finally:
        dispatcher.close()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_dispatcher_sigterm_reaps_nested_request_child(monkeypatch, tmp_path):
    import multiprocessing
    import os
    import signal
    import time
    from app import controller, events
    child_pid_file = tmp_path/'request-child'
    def operation():
        child_pid_file.write_text(str(os.getpid()))
        time.sleep(30)
    def dispatch(c):
        signal.signal(signal.SIGTERM, controller._shutdown)
        events._bounded_call(operation, (), 30)
    monkeypatch.setattr(controller, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(events, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(controller, 'webhook_tick', dispatch)
    dispatcher = controller.WebhookDispatcher()
    try:
        dispatcher.poll({})
        deadline = time.monotonic()+3
        while not child_pid_file.exists() and time.monotonic() < deadline: time.sleep(.01)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
    finally:
        dispatcher.close()
    with pytest.raises(ProcessLookupError): os.kill(child_pid, 0)


def test_controller_and_webhook_use_distinct_session_advisory_locks():
    from app.controller import LOCK_ID, WEBHOOK_LOCK_ID, controller_lock
    assert LOCK_ID != WEBHOOK_LOCK_ID
    class Connection:
        calls = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scalar(self, statement, params):
            self.calls.append(('lock', params['key'])); return True
        def execute(self, statement, params): self.calls.append(('unlock', params['key']))
        def commit(self): self.calls.append(('commit',))
    conn = Connection()
    engine = NS(dialect=NS(name='postgresql'), connect=lambda: conn)
    with controller_lock(engine, WEBHOOK_LOCK_ID) as locked:
        assert locked and conn.calls == [('lock', WEBHOOK_LOCK_ID), ('commit',)]
    assert conn.calls[-2:] == [('unlock', WEBHOOK_LOCK_ID), ('commit',)]
