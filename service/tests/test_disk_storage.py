"""Synthetic disk files and signed HTTP capabilities; no external storage."""
import hashlib
import io
import json
import os
import time
from unittest.mock import Mock, patch

import pytest
from flask import g
from itsdangerous import TimestampSigner

from app import storage
from app.models import Account, Message, Profile, MessageState

BASE = "https://archive.test"
KEY = "accounts/personal/media/" + "a" * 64
CONTENT = b"synthetic private image"


@pytest.fixture
def archived(application, tmp_path):
    root = (tmp_path / "private-media").resolve()
    target = root / KEY
    target.parent.mkdir(parents=True)
    target.write_bytes(CONTENT)
    target.chmod(0o600)
    media = {"object_key": KEY, "size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest(),
             "mime_type": "image/png", "filename": "photo.png"}
    application.config.update(STORAGE_BACKEND="disk", MEDIA_ROOT=str(root))
    with application.extensions["db_session"].begin() as db:
        db.add(Message(id="disk-message", account_id="personal", message_id="disk-wa", chat_jid="chat@example",
                       sender_jid="sender@example", timestamp="2026-10-27T00:00:00Z", media=json.dumps(media)))
    return root, target, media


def link(application, media):
    with application.test_request_context(base_url=BASE):
        g.db = application.extensions["db_session"]()
        return storage.media_url(media, account_id="personal", message_id="disk-message")


def test_roundtrip_and_private_capability(application, client, archived):
    _, _, media = archived
    with application.app_context(), patch.object(storage, "client", side_effect=AssertionError("S3 not needed")):
        assert storage.read_media(media, max_bytes=1024) == CONTENT
    url = link(application, media)
    response = client.get(url, base_url=BASE)
    assert response.status_code == 200 and response.data == CONTENT
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    response.close()


def test_missing_wrong_expired_and_tampered_tokens(application, client, archived):
    _, _, media = archived
    url = link(application, media)
    old = int(time.time()) - 301
    with patch.object(TimestampSigner, "get_timestamp", return_value=old):
        expired = link(application, media)
    for bad in ("/media/download", "/media/download?token=wrong", url + "tampered", expired, url + "&token=second"):
        response = client.get(bad, base_url=BASE)
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"
    assert client.post(url, base_url=BASE).status_code == 405


@pytest.mark.parametrize("change", ["unassigned", "assignment-replaced", "unconfigured", "missing-account", "missing-message", "object-replaced", "deleted"])
def test_link_rechecks_retention_assignment_and_object(application, client, archived, change):
    _, _, media = archived
    url = link(application, media)
    with application.extensions["db_session"].begin() as db:
        if change == "unassigned": db.delete(db.get(Profile, "personal"))
        if change == "assignment-replaced": db.get(Profile, "personal").creation_key = "replacement-profile"
        if change == "missing-account": db.delete(db.get(Account, "personal"))
        if change == "missing-message": db.delete(db.get(Message, "disk-message"))
        if change == "object-replaced": db.get(Message, "disk-message").media = json.dumps({**media, "object_key": KEY[:-1] + "b"})
        if change == "deleted": db.add(MessageState(account_id="personal", chat_jid="chat@example", message_id="disk-wa", deleted_at="2026-10-27T00:00:01Z"))
    if change == "unconfigured": application.config["ACCOUNTS"] = []
    assert client.get(url, base_url=BASE).status_code == 404
    if change != "assignment-replaced":
        with pytest.raises(ValueError): link(application, media)


def test_disk_bounds_checksum_and_regular_file(application, client, archived):
    _, target, media = archived
    url = link(application, media)
    with application.app_context():
        with pytest.raises(ValueError, match="size limit"): storage.read_media(media, max_bytes=1)
        target.write_bytes(b"X" * len(CONTENT))
        with pytest.raises(ValueError, match="integrity"): storage.read_media(media)
    assert client.get(url, base_url=BASE).status_code == 404
    target.unlink()
    os.mkfifo(target)
    with application.app_context(), pytest.raises(ValueError): storage.read_media(media)
    assert client.get(url, base_url=BASE).status_code == 404


@pytest.mark.parametrize("component", ["file", "account", "root"])
def test_symlinks_cannot_read_files(application, client, archived, component, tmp_path):
    root, target, media = archived
    url = link(application, media)
    if component == "file":
        other = tmp_path / "outside"
        other.write_bytes(CONTENT)
        target.unlink()
        target.symlink_to(other)
    else:
        source = root if component == "root" else root / "accounts/personal"
        moved = source.with_name(source.name + "-moved")
        source.rename(moved)
        source.symlink_to(moved, target_is_directory=True)
    with application.app_context(), pytest.raises(ValueError): storage.read_media(media)
    assert client.get(url, base_url=BASE).status_code == 404


def test_traversal_and_unassigned_accounts_rejected(application, archived):
    _, _, media = archived
    for key in ("../secret", "accounts/personal/media/../../secret", "/etc/passwd", KEY + "/extra", KEY.replace("personal", "other")):
        with application.test_request_context(base_url=BASE):
            g.db = application.extensions["db_session"]()
            with pytest.raises(ValueError): storage.media_url({**media, "object_key": key}, account_id="personal", message_id="disk-message")
        if "other" not in key:
            with application.app_context(), pytest.raises(ValueError): storage.read_media({**media, "object_key": key})
    with application.test_request_context(base_url=BASE):
        g.db = application.extensions["db_session"]()
        with pytest.raises(ValueError): storage.media_url(media, account_id="unknown", message_id="disk-message")


def test_s3_default_and_bounded_read_compatibility(application):
    application.config.pop("STORAGE_BACKEND", None)
    media = {"object_key": "existing-arbitrary-key", "size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest()}
    fake = Mock()
    fake.generate_presigned_url.return_value = "https://signed.test/object"
    body = io.BytesIO(CONTENT)
    fake.get_object.return_value = {"Body": body}
    with application.app_context(), patch.object(storage, "client", return_value=fake):
        assert storage.media_url(media) == "https://signed.test/object"
        assert fake.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300
        assert storage.read_media(media) == CONTENT
        assert body.closed
        # Existing S3 reads never depended on recorded SHA or exact size.
        fake.get_object.return_value = {"Body": io.BytesIO(CONTENT)}
        assert storage.read_media({"object_key": "existing-arbitrary-key"}) == CONTENT
        stream = Mock()
        stream.read.return_value = b"X" * 11
        fake.get_object.return_value = {"Body": stream}
        with pytest.raises(ValueError): storage.read_media({**media, "size": 10}, max_bytes=10)
        stream.read.assert_called_once_with(11)
        stream.close.assert_called_once()


def test_backend_and_root_validation(application):
    with application.app_context():
        application.config["STORAGE_BACKEND"] = "unknown"
        with pytest.raises(ValueError): storage.backend()
        application.config["STORAGE_BACKEND"] = "disk"
        for root in ("relative", "/", "/media/../other"):
            application.config["MEDIA_ROOT"] = root
            with pytest.raises(ValueError): storage._root()
        application.config["MEDIA_ROOT"] = application.static_folder
        with pytest.raises(ValueError): storage.install_storage(application)


def test_missing_file_and_safe_disposition(application, client, archived):
    _, target, media = archived
    unsafe = {**media, "filename": "../../unsafe\r\nname.html", "mime_type": "text/html"}
    with application.extensions["db_session"].begin() as db:
        db.get(Message, "disk-message").media = json.dumps(unsafe)
    url = link(application, unsafe)
    response = client.get(url, base_url=BASE)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert "unsafe" in response.headers["Content-Disposition"]
    assert ".." not in response.headers["Content-Disposition"]
    assert "\r" not in response.headers["Content-Disposition"]
    assert "\n" not in response.headers["Content-Disposition"]
    response.close()
    target.unlink()
    assert client.get(url, base_url=BASE).status_code == 404
