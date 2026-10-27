"""One durable queue slot, Kubernetes reconciliation, and webhook outbox dispatch.

Run separately from HTTP. Transcription and webhook dispatch use independent
PostgreSQL advisory locks and processes; callback I/O cannot stall reconciliation.
"""
import copy
import json
import logging
import os
import secrets
import signal
import time
from pathlib import Path
from contextlib import contextmanager
from cryptography.fernet import Fernet
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from sqlalchemy import select, text
from .config import settings
from .models import database, Job, WorkerAttempt, now
from .auth import digest
from .events import deliver, _process_context, stop_process

LOG = logging.getLogger(__name__)
LOCK_ID = 841734901
WEBHOOK_LOCK_ID = 841734902
WEBHOOK_TICK_DEADLINE = 25


@contextmanager
def controller_lock(engine, lock_id=LOCK_ID):
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Controller requires PostgreSQL advisory locking")
    with engine.connect() as conn:
        acquired = conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id})
        conn.commit()  # Session advisory lock survives; no idle transaction during I/O.
        try: yield acquired
        finally:
            if acquired:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
                    conn.commit()
                except Exception:
                    conn.invalidate()  # Never return a still-locked session to the pool.
                    raise


def job_manifest(template, job):
    manifest = copy.deepcopy(template)
    manifest.setdefault("metadata", {})["name"] = job.job_name
    labels = manifest["metadata"].setdefault("labels", {})
    labels.update({"app.kubernetes.io/component": "transcription-worker", "whatsapp-mcp/job-id": job.id})
    # TTL is added only after durable terminal callback/reconciliation.
    manifest["spec"].pop("ttlSecondsAfterFinished", None)
    manifest["spec"]["backoffLimit"] = 0
    containers = manifest["spec"]["template"]["spec"]["containers"]
    if len(containers) != 1: raise ValueError("Worker template must contain exactly one container")
    env = containers[0].setdefault("env", [])
    env[:] = [x for x in env if x["name"] not in ("JOB_ID", "JOB_TOKEN")]
    env.extend([{"name": "JOB_ID", "value": job.id}, {"name": "JOB_TOKEN", "value": job._raw_token}])
    return manifest


def release(job, failed=False):
    job.slot, job.attempt_token, job.attempt_secret = None, None, None
    if failed:
        job.state = "failed" if job.attempt >= 3 else "queued"
        job.available_at = now()+60*2**job.attempt
        job.error = job.error or "worker_terminated_without_result"
    job.updated_at = now()


def reconcile(sessions, api, namespace, template, encryption_key, asr_enabled=True):
    # Disabled means pause: retain queue, attempt identity and callback credentials.
    # In particular, a missing Kubernetes Job must never recreate work while off.
    if not asr_enabled:
        return
    fernet = Fernet(encryption_key.encode())
    with sessions.begin() as db:
        job = db.scalar(select(Job).where(Job.slot == 1).with_for_update())
        if job is None:
            job = db.scalar(select(Job).where(Job.state == "queued", Job.available_at <= now()).order_by(Job.available_at, Job.id).limit(1).with_for_update(skip_locked=True))
            if job is None: return
            raw = secrets.token_urlsafe(32)
            job.slot, job.state, job.attempt = 1, "running", job.attempt+1
            job.job_name = "wa-asr-"+job.id.replace("-", "")[:24]+"-"+str(job.attempt)
            job.attempt_token, job.attempt_secret = digest(raw), fernet.encrypt(raw.encode()).decode()
            job.updated_at = now()
            db.flush()
            db.add(WorkerAttempt(name=job.job_name, job_id=job.id))
        job_id = job.id
    # The attempt is committed before creating a Job. Repeated creates use one name.
    with sessions.begin() as db:
        job = db.get(Job, job_id, with_for_update=True)
        try:
            existing = api.read_namespaced_job(job.job_name, namespace)
        except ApiException as e:
            if e.status != 404: raise
            if job.state != "running":
                release(job, failed=job.state != "completed")
                attempt = db.get(WorkerAttempt, job.job_name)
                if attempt: attempt.terminal, attempt.cleanup_scheduled = True, True
                return
            job._raw_token = fernet.decrypt(job.attempt_secret.encode()).decode()
            try:
                api.create_namespaced_job(namespace, job_manifest(template, job))
            except ApiException as create_error:
                if create_error.status != 409: raise
            return
        labels = existing.metadata.labels or {}
        if labels.get("whatsapp-mcp/job-id") != job.id:
            raise RuntimeError("Worker Job identity mismatch")
        terminal = bool(existing.status.succeeded or existing.status.failed)
        if not terminal: return  # Pending capacity has no short scheduling deadline.
        failed = job.state != "completed"
        release(job, failed=failed)
        attempt = db.get(WorkerAttempt, job.job_name)
        if attempt: attempt.terminal = True
        # Results/retry state must commit before Kubernetes cleanup.
    schedule_cleanup(sessions, api, namespace)


def schedule_cleanup(sessions, api, namespace):
    with sessions.begin() as db:
        attempts = list(db.scalars(select(WorkerAttempt).where(WorkerAttempt.terminal.is_(True), WorkerAttempt.cleanup_scheduled.is_(False)).limit(100)))
        for attempt in attempts:
            try:
                api.patch_namespaced_job(attempt.name, namespace, {"spec": {"ttlSecondsAfterFinished": 300}})
                attempt.cleanup_scheduled = True
            except ApiException as error:
                if error.status == 404: attempt.cleanup_scheduled = True
                else: raise



def _shutdown(signum, frame):
    raise SystemExit(0)


def webhook_tick(c):
    """Disposable child: its own engine/lock, one bounded durable dispatch."""
    # Spawn starts a fresh interpreter; main's INFO level is not inherited.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, _shutdown)
    engine, sessions = database(c["DATABASE_URL"])
    try:
        with controller_lock(engine, WEBHOOK_LOCK_ID) as locked:
            if locked:
                deliver(sessions, c["TOKEN_ENCRYPTION_KEY"], allowed_clients=c["OAUTH_CLIENTS"], allowed_accounts=c["ACCOUNTS"],
                        dynamic_enabled=c["OAUTH_DYNAMIC_REGISTRATION_ENABLED"], relay_config=c)
    except Exception as error:
        LOG.error("Webhook tick failed (%s)", type(error).__name__)
    finally:
        engine.dispose()


class WebhookDispatcher:
    """One supervised child at a time; polling never waits for callback I/O."""
    def __init__(self):
        self.process = None
        self.started_at = 0

    def poll(self, c):
        if self.process is not None:
            if self.process.is_alive() and time.monotonic()-self.started_at < WEBHOOK_TICK_DEADLINE:
                return
            self.close()
        self.process = _process_context().Process(target=webhook_tick, args=(c,))
        self.started_at = time.monotonic()
        self.process.start()

    def close(self):
        if self.process is not None:
            stop_process(self.process)
            self.process = None


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    c = settings()
    engine, sessions = database(c["DATABASE_URL"])
    api, template = None, None
    if c["ASR_ENABLED"]:
        config.load_incluster_config()
        api = client.BatchV1Api()
        template = json.loads(Path(os.environ["WORKER_JOB_TEMPLATE_FILE"]).read_text())
    namespace = os.environ.get("WORKER_NAMESPACE", "whatsapp-mcp")
    signal.signal(signal.SIGTERM, _shutdown)
    dispatcher = WebhookDispatcher()
    try:
        while True:
            try:
                dispatcher.poll(c)
                if not c["ASR_ENABLED"]:
                    time.sleep(5)
                    continue
                with controller_lock(engine) as locked:
                    if locked:
                        schedule_cleanup(sessions, api, namespace)
                        reconcile(sessions, api, namespace, template, c["TOKEN_ENCRYPTION_KEY"])
            except Exception as error:
                # Exception messages from providers may contain secrets; type alone is safe.
                LOG.error("Controller tick failed (%s)", type(error).__name__)
            time.sleep(5)
    finally:
        dispatcher.close()
        engine.dispose()

if __name__ == "__main__": main()
