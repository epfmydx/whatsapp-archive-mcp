"""Private object storage and short-lived, account-bound disk capabilities."""
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path

import boto3
from botocore.config import Config
from flask import abort, current_app, g, request, send_file
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

DISK_KEY = re.compile(r"accounts/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})/media/([a-f0-9]{64})\Z")
MAX_OBJECT_BYTES = 1024 * 1024 * 1024


def backend():
    value = current_app.config.get("STORAGE_BACKEND", "s3")
    if value not in ("s3", "disk"):
        raise ValueError("Invalid storage backend")
    return value


def client(public=False):
    c = current_app.config
    return boto3.client("s3", endpoint_url=(c["S3_PUBLIC_ENDPOINT_URL"] if public else None) or c["S3_ENDPOINT_URL"] or None,
                        region_name=c["S3_REGION"] or "us-east-1", aws_access_key_id=c["S3_ACCESS_KEY_ID"],
                        aws_secret_access_key=c["S3_SECRET_ACCESS_KEY"],
                        config=Config(signature_version="s3v4", s3={"addressing_style": c["S3_ADDRESSING_STYLE"] or "path"},
                                      connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}))


def _key(key):
    match = DISK_KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise ValueError("Invalid archived media reference")
    return match.group(1)


def _root():
    root = current_app.config.get("MEDIA_ROOT", "/media")
    if not isinstance(root, str) or not root.startswith("/") or root == "/" or root.startswith("//") or os.path.normpath(root) != root or any(ord(c) <= 32 or ord(c) == 127 for c in root):
        raise ValueError("Invalid media root")
    return root


def _disk_open(key):
    _key(key)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in (_root().strip("/") + "/" + key).split("/")[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        raw = os.open(key.rsplit("/", 1)[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(raw).st_mode):
                raise ValueError("Archived media is unavailable")
            stream = os.fdopen(raw, "rb")
        except Exception:
            os.close(raw)
            raise
        return stream
    except OSError:
        raise ValueError("Archived media is unavailable") from None
    finally:
        os.close(fd)


def _bounds(media, max_bytes):
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_OBJECT_BYTES:
        raise ValueError("Invalid media read bound")
    size = media.get("size")
    if type(size) is not int or not 0 <= size <= max_bytes:
        raise ValueError("Archived media exceeds size limit")
    checksum = media.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        raise ValueError("Invalid archived media checksum")
    return size, checksum.lower()


def _verify_stream(stream, size, checksum):
    digest, remaining = hashlib.sha256(), size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise ValueError("Archived media integrity check failed")
        remaining -= len(block)
        digest.update(block)
    if stream.read(1) or not hmac.compare_digest(digest.hexdigest(), checksum):
        raise ValueError("Archived media integrity check failed")


def read_media(media, *, max_bytes=5 * 1024 * 1024):
    """Return bounded bytes; disk files also match retained size and checksum."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_OBJECT_BYTES:
        raise ValueError("Invalid media read bound")
    disk = backend() == "disk"
    if disk:
        size, checksum = _bounds(media, max_bytes)
        stream = _disk_open(media["object_key"])
    else:
        obj = client().get_object(Bucket=current_app.config["S3_BUCKET"], Key=media["object_key"])
        stream = obj["Body"]
    try:
        body = stream.read(max_bytes + 1)
    finally:
        stream.close()
    if len(body) > max_bytes:
        raise ValueError("Archived media exceeds size limit")
    if disk and (len(body) != size or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), checksum)):
        raise ValueError("Archived media integrity check failed")
    return body


def _signer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="private-disk-media-v1")


def _retained_media(account_id, message_id, key, profile_key=None):
    from .models import Message, Profile
    from .profiles import require_account
    from .message_updates import is_deleted
    require_account(account_id)
    profile = g.db.get(Profile, account_id)
    message = g.db.get(Message, message_id)
    if (profile_key is not None and profile.creation_key != profile_key) or not message or message.account_id != account_id or not message.media or is_deleted(message):
        raise ValueError("Archived media is unavailable")
    media = json.loads(message.media)
    if not isinstance(media, dict) or _key(key) != account_id or media.get("object_key") != key:
        raise ValueError("Archived media is unavailable")
    return profile, media


def media_url(media, *, account_id=None, message_id=None):
    if backend() == "s3":
        return client(public=True).generate_presigned_url("get_object", Params={"Bucket": current_app.config["S3_BUCKET"],
            "Key": media["object_key"], "ResponseContentDisposition": "attachment"}, ExpiresIn=300)
    key = media["object_key"]
    profile, retained = _retained_media(account_id, message_id, key)
    _bounds(retained, MAX_OBJECT_BYTES)
    token = _signer().dumps({"a": account_id, "m": message_id, "k": key, "p": profile.creation_key})
    return current_app.config["APP_BASE_URL"].rstrip("/") + "/media/download?token=" + token


def install_storage(app):
    with app.app_context():
        if backend() == "disk":
            root = Path(_root()).resolve()
            if app.static_folder and (root == Path(app.static_folder).resolve() or Path(app.static_folder).resolve() in root.parents):
                raise ValueError("Media root must be outside public assets")

    @app.get("/media/download")
    def disk_media_download():
        # The signature is a private GET capability, issued only after MCP
        # authentication; browser sessions or public static mounts are not used.
        stream = None
        try:
            if backend() != "disk":
                raise ValueError()
            tokens = request.args.getlist("token")
            if len(tokens) != 1 or len(tokens[0]) > 4096:
                raise ValueError()
            payload = _signer().loads(tokens[0], max_age=300)
            if not isinstance(payload, dict) or set(payload) != {"a", "m", "k", "p"} or not all(isinstance(value, str) for value in payload.values()):
                raise ValueError()
            _, media = _retained_media(payload["a"], payload["m"], payload["k"], payload["p"])
            size, checksum = _bounds(media, MAX_OBJECT_BYTES)
            stream = _disk_open(payload["k"])
            if os.fstat(stream.fileno()).st_size != size:
                raise ValueError()
            _verify_stream(stream, size, checksum)
            stream.seek(0)
        except (BadSignature, SignatureExpired, ValueError, TypeError, KeyError):
            if stream is not None:
                stream.close()
            abort(404, "Archived media is unavailable")
        filename = str(media.get("filename", "attachment")).replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(c for c in filename if c.isprintable())[:180].strip() or "attachment"
        mime = media.get("mime_type", "")
        if not isinstance(mime, str) or not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", mime) or mime in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            mime = "application/octet-stream"
        response = send_file(stream, mimetype=mime, as_attachment=True, download_name=filename,
                             conditional=False, etag=False, max_age=0)
        response.content_length = size
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.call_on_close(stream.close)
        return response
