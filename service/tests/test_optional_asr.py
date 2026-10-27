"""Synthetic optional-ASR policy, persistence and controller checks."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import controller
from app.config import settings
from app.models import Delivery, Event, Job, WorkerAttempt
from test_api import audio, payload
from test_auth import BASE
from test_controller import FakeKubernetes, TEMPLATE, queued
from test_events import outbox


def configuration(**overrides):
    return settings({"SECRET_KEY": "s" * 32, "TOKEN_ENCRYPTION_KEY": "e" * 44,
                     "INTERNAL_TOKEN": "i" * 32, **overrides})


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True),
    ("false", False), ("TRUE", True), ("FALSE", False), ("1", True), ("0", False)])
def test_asr_flag_parsing(value, expected):
    assert configuration(ASR_ENABLED=value)["ASR_ENABLED"] is expected


@pytest.mark.parametrize("value", [None, 0, 1, [], {}, "", "yes", " false "])
def test_asr_rejects_unvalidated_flags(value):
    with pytest.raises(ValueError, match="ASR_ENABLED"):
        configuration(ASR_ENABLED=value)


def test_asr_and_storage_environment_defaults_and_overrides(monkeypatch):
    for key in ("ASR_ENABLED", "STORAGE_BACKEND", "MEDIA_ROOT"):
        monkeypatch.delenv(key, raising=False)
    defaults = configuration()
    assert defaults["ASR_ENABLED"] is True
    assert defaults["STORAGE_BACKEND"] == "s3"
    assert defaults["MEDIA_ROOT"] == "/media"
    monkeypatch.setenv("ASR_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "disk")
    # Configuration validation must not touch a filesystem or require this to exist.
    monkeypatch.setenv("MEDIA_ROOT", "/synthetic/not-created/archive")
    configured = configuration()
    assert configured["ASR_ENABLED"] is False
    assert configured["STORAGE_BACKEND"] == "disk"
    assert configured["MEDIA_ROOT"] == "/synthetic/not-created/archive"


@pytest.mark.parametrize("value", [None, True, "", "/", "media", "//media", "/media/", "/media/../private", "/media/./archive", "/media//archive", "/media\x00", "/media\n"])
def test_storage_rejects_unsafe_roots(value):
    with pytest.raises(ValueError, match="MEDIA_ROOT"):
        configuration(MEDIA_ROOT=value)


@pytest.mark.parametrize("value", [None, True, "", "local", "S3", []])
def test_storage_rejects_unknown_backends(value):
    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        configuration(STORAGE_BACKEND=value)


def snapshot(application):
    with application.extensions["db_session"]() as db:
        return [(j.id, j.state, j.slot, j.attempt, j.job_name, j.attempt_token, j.attempt_secret,
                 j.available_at, j.updated_at, j.result) for j in db.scalars(select(Job).order_by(Job.id))]


def test_disabled_controller_preserves_queued_and_missing_running_attempts(application):
    queued(application)
    sessions = application.extensions["db_session"]
    key = application.config["TOKEN_ENCRYPTION_KEY"]
    api = FakeKubernetes()
    controller.reconcile(sessions, api, "test", TEMPLATE, key)
    assert len(api.creates) == 1
    api.jobs.clear()  # Persisted running state exists but Kubernetes Job disappeared.
    before = snapshot(application)
    controller.reconcile(sessions, None, "test", None, key, asr_enabled=False)
    controller.reconcile(sessions, api, "test", TEMPLATE, key, asr_enabled=False)
    assert snapshot(application) == before
    assert len(api.creates) == 1
    with sessions() as db:
        assert len(list(db.scalars(select(WorkerAttempt)))) == 1
    # Re-enabling resumes the committed attempt rather than allocating a new one.
    controller.reconcile(sessions, api, "test", TEMPLATE, key, asr_enabled=True)
    assert len(api.creates) == 2
    assert api.creates[0]["metadata"]["name"] == api.creates[1]["metadata"]["name"]


def test_disabled_controller_main_dispatches_without_kubernetes_or_template(application, monkeypatch):
    calls = []
    monkeypatch.setattr(controller, "settings", lambda: dict(application.config, ASR_ENABLED=False))
    monkeypatch.setattr(controller, "database", lambda url: (SimpleNamespace(dispose=lambda: calls.append("dispose")), None))
    monkeypatch.setattr(controller.config, "load_incluster_config", lambda: pytest.fail("disabled ASR loaded Kubernetes"))
    monkeypatch.setattr(controller.client, "BatchV1Api", lambda: pytest.fail("disabled ASR constructed Kubernetes API"))
    monkeypatch.delenv("WORKER_JOB_TEMPLATE_FILE", raising=False)
    class Dispatcher:
        def poll(self, c):
            assert c["ASR_ENABLED"] is False
            calls.append("webhook")
        def close(self): calls.append("close")
    monkeypatch.setattr(controller, "WebhookDispatcher", Dispatcher)
    def stop(seconds): raise KeyboardInterrupt
    monkeypatch.setattr(controller.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt): controller.main()
    assert calls == ["webhook", "close", "dispose"]


def test_existing_worker_can_complete_while_asr_disabled(application, client, internal):
    queued(application, 1)
    api = FakeKubernetes()
    controller.reconcile(application.extensions["db_session"], api, "test", TEMPLATE, application.config["TOKEN_ENCRYPTION_KEY"])
    env = {e["name"]: e for e in api.creates[0]["spec"]["template"]["spec"]["containers"][0]["env"]}
    application.config["ASR_ENABLED"] = False
    result = {"text": "synthetic transcript", "language": None, "segments": [], "engine": "test", "model": "test"}
    headers = internal | {"X-Job-Token": env["JOB_TOKEN"]["value"]}
    response = client.post("/internal/jobs/" + env["JOB_ID"]["value"] + "/complete", base_url=BASE, headers=headers, json=result)
    assert response.status_code == 200
    with application.extensions["db_session"]() as db:
        job = db.scalar(select(Job))
        assert job.state == "completed" and job.slot == 1
        assert db.scalar(select(Event).where(Event.event_type == "whatsapp.transcription.completed"))


def test_disabled_asr_webhook_tick_still_delivers_outbox(application, monkeypatch):
    from app import events
    sessions, _ = outbox(application, monkeypatch)
    application.config["ASR_ENABLED"] = False
    @contextmanager
    def lock(engine, lock_id):
        assert lock_id == controller.WEBHOOK_LOCK_ID
        yield True
    sent = []
    monkeypatch.setattr(controller, "controller_lock", lock)
    monkeypatch.setattr(controller, "deliver", lambda sessions, key, **kwargs: events.deliver(sessions, key, post=lambda *args: (sent.append(args) or (200, b"{}"))))
    controller.webhook_tick(application.config)
    assert len(sent) == 1
    with sessions() as db:
        assert db.scalar(select(Delivery)).delivered


def test_disabled_ingestion_archives_audio_and_text_without_automatic_backfill(application, client, internal):
    from app.models import Message
    application.config["ASR_ENABLED"] = False
    audio_record = payload(message_id="audio-off", media=audio())
    assert client.post("/internal/messages", base_url=BASE, headers=internal, json=audio_record).status_code == 200
    assert client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(message_id="text-off")).status_code == 200
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Message)))) == 2
        assert db.scalar(select(Message).where(Message.message_id == "audio-off")).media
        assert not list(db.scalars(select(Job)))
        assert len(list(db.scalars(select(Event)))) == 2
    application.config["ASR_ENABLED"] = True
    # The persisted audio record remains unqueued, including duplicate bridge delivery.
    response = client.post("/internal/messages", base_url=BASE, headers=internal, json=audio_record)
    assert response.status_code == 200 and response.json["duplicate"]
    with application.extensions["db_session"]() as db:
        assert not list(db.scalars(select(Job)))
    assert client.post("/internal/messages", base_url=BASE, headers=internal,
                       json=payload(message_id="audio-on", media=audio())).status_code == 200
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Job)))) == 1
