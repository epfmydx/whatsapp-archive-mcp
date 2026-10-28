"""One leased CPU transcription attempt. Never logs media, transcript, or credentials."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from uuid import UUID

PINS = json.loads(Path(__file__).with_name("pins.json").read_text())


class WorkerError(Exception):
    """Only static, non-sensitive descriptions are passed to callbacks."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise WorkerError("internal API redirected")


def bounded_int(env, key, default, low, high):
    raw = env.get(key, default)
    if type(raw) not in (str, int):
        raise WorkerError("invalid worker bounds")
    try:
        value = int(raw)
    except ValueError:
        raise WorkerError("invalid worker bounds") from None
    if value < low or (high is not None and value > high):
        raise WorkerError("invalid worker bounds")
    return value


class Config:
    def __init__(self, env=os.environ):
        self.job_id = str(UUID(env["JOB_ID"]))
        self.token = env["JOB_TOKEN"]
        self.internal_token = env["INTERNAL_TOKEN"]
        if not re.fullmatch(r"[A-Za-z0-9_\-.]{16,256}", self.token):
            raise WorkerError("invalid attempt token")
        self.app_url = env["APP_INTERNAL_URL"].rstrip("/")
        url = urlsplit(self.app_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise WorkerError("invalid internal API URL")
        self.threads = bounded_int(env, "WORKER_THREADS", 1, 1, None)
        self.chunk_seconds = bounded_int(env, "AUDIO_CHUNK_SECONDS", 30, 1, 30)
        self.max_seconds = bounded_int(env, "MAX_AUDIO_SECONDS", 1800, 1, 1800)
        self.max_bytes = bounded_int(env, "MAX_MEDIA_BYTES", 104857600, 1, 104857600)
        self.model_path = Path(env.get("MODEL_PATH", "/model-cache/" + PINS["model"]))
        if env.get("MODEL_URL", PINS["model_url"]) != PINS["model_url"] or env.get("MODEL_SHA256", PINS["model_sha256"]) != PINS["model_sha256"]:
            raise WorkerError("unsupported model provenance")
        self.storage_backend = env.get("STORAGE_BACKEND", "s3")
        if self.storage_backend not in ("s3", "disk"):
            raise WorkerError("invalid storage backend")
        self.media_root = env.get("MEDIA_ROOT", "/media")
        if self.storage_backend == "disk" and (not os.path.isabs(self.media_root) or self.media_root == "/" or self.media_root.startswith("//") or os.path.normpath(self.media_root) != self.media_root or any(ord(c) <= 32 or ord(c) == 127 for c in self.media_root)):
            raise WorkerError("invalid media root")
        self.env = env

    def api(self, suffix="", payload=None):
        request = Request(self.app_url + "/internal/jobs/" + self.job_id + suffix,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self.internal_token,
                                   "X-Job-Token": self.token, "Content-Type": "application/json"})
        # No redirect may forward internal credentials to a different endpoint.
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise WorkerError("internal API response too large")
        return json.loads(data) if data else None


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model(cfg):
    target = cfg.model_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".model.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_file() and target.stat().st_size == PINS["model_size"] and sha256_file(target) == PINS["model_sha256"]:
            return target
        # Only this immutable approved URL is accepted. HF may redirect to its CDN;
        # no internal credentials are attached to the model request.
        temporary = target.with_suffix(".download")
        try:
            with urlopen(PINS["model_url"], timeout=60) as source, temporary.open("wb") as dest:
                count = 0
                deadline = time.monotonic() + 1800
                while block := source.read(1024 * 1024):
                    count += len(block)
                    if count > PINS["model_size"] or time.monotonic() > deadline:
                        raise WorkerError("model download exceeded bounds")
                    dest.write(block)
            if count != PINS["model_size"] or sha256_file(temporary) != PINS["model_sha256"]:
                raise WorkerError("model integrity check failed")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def validate_job(job, cfg):
    if not isinstance(job, dict) or job.get("id") != cfg.job_id:
        raise WorkerError("invalid claimed job")
    account = job.get("account_id", "")
    key = job.get("object_key", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", account) or not isinstance(key, str) or len(key) > 1024 or not key.startswith("accounts/" + account + "/media/") or ".." in key or "\x00" in key:
        raise WorkerError("invalid media reference")
    if not str(job.get("mime_type", "")).startswith("audio/"):
        raise WorkerError("unsupported media type")
    size, checksum = job.get("size"), job.get("sha256")
    if type(size) is not int or not 0 < size <= cfg.max_bytes:
        raise WorkerError("invalid archived media size")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise WorkerError("invalid archived media checksum")
    return key


def disk_media(cfg, key):
    if not isinstance(key, str) or not re.fullmatch(r"accounts/[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}/media/[a-f0-9]{64}", key):
        raise WorkerError("invalid media reference")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = (cfg.media_root.strip("/") + "/" + key).split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        raw = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(raw)
            if not stat.S_ISREG(info.st_mode):
                raise WorkerError("archived media unavailable")
            body = os.fdopen(raw, "rb")
        except Exception:
            os.close(raw)
            raise
        return {"ContentLength": info.st_size, "Body": body}
    except OSError:
        raise WorkerError("archived media unavailable") from None
    finally:
        os.close(fd)


def download_media(cfg, key, dest, *, expected_size, expected_sha256, client=None):
    if type(expected_size) is not int or not 0 < expected_size <= cfg.max_bytes:
        raise WorkerError("invalid archived media size")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise WorkerError("invalid archived media checksum")
    if cfg.storage_backend == "disk":
        response = disk_media(cfg, key)
    else:
        if client is None:
            import boto3
            from botocore.config import Config as S3Config
            client = boto3.client("s3", endpoint_url=cfg.env.get("S3_ENDPOINT_URL") or None, region_name=cfg.env.get("S3_REGION", "us-east-1"),
                                  aws_access_key_id=cfg.env.get("S3_ACCESS_KEY_ID"), aws_secret_access_key=cfg.env.get("S3_SECRET_ACCESS_KEY"),
                                  config=S3Config(s3={"addressing_style": cfg.env.get("S3_ADDRESSING_STYLE", "path")},
                                                  connect_timeout=15, read_timeout=60, retries={"max_attempts": 2}))
        response = client.get_object(Bucket=cfg.env["S3_BUCKET"], Key=key)
    temporary = None
    with contextlib.closing(response["Body"]) as body:
        if type(response["ContentLength"]) is not int or response["ContentLength"] != expected_size:
            raise WorkerError("archived media size mismatch")
        total = 0
        checksum = hashlib.sha256()
        deadline = time.monotonic() + 300
        try:
            raw, temporary = tempfile.mkstemp(prefix=".media-", dir=dest.parent)
            with os.fdopen(raw, "wb") as out:
                while block := body.read(min(1024 * 1024, expected_size - total + 1)):
                    total += len(block)
                    if total > expected_size or time.monotonic() > deadline:
                        raise WorkerError("media exceeded download bounds")
                    checksum.update(block)
                    out.write(block)
                if total != expected_size or checksum.hexdigest() != expected_sha256.lower():
                    raise WorkerError("archived media integrity check failed")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, dest)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)


def process_limits():
    # Prevent a failed native executable flooding disk with output. The largest
    # per-chunk PCM here is <2 MB; original S3 media is downloaded by Python.
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_process(args, timeout, runner=subprocess.run):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = runner(args, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                            timeout=timeout, check=False, preexec_fn=process_limits,
                            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
        except (subprocess.TimeoutExpired, OSError):
            raise WorkerError("audio subprocess unavailable or timed out") from None
        if result.returncode:
            raise WorkerError("audio subprocess failed")
        stdout.seek(0)
        data = stdout.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise WorkerError("audio subprocess output too large")
        return data.decode("utf-8", errors="strict")


def probe_duration(path, cfg, run=run_process):
    output = run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "ogg,mp3,wav,mov,matroska,webm,flac,aac", "-show_entries", "format=duration", "-of", "json", str(path)], 30)
    try:
        duration = float(json.loads(output)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        raise WorkerError("invalid audio duration") from None
    if not math.isfinite(duration) or not 0 < duration <= cfg.max_seconds:
        raise WorkerError("audio exceeded duration limit")
    return duration


def parse_transcript(output, chunk):
    try:
        records = [json.loads(line) for line in output.splitlines() if line.strip()]
    except ValueError:
        raise WorkerError("invalid native transcription output") from None
    # The pinned CLI emits a timing-only batch_header before per-file JSON.
    if records and isinstance(records[0], dict) and records[0].get("type") == "batch_header":
        header = records.pop(0)
        if set(header) != {"type", "load_ms"} or not isinstance(header["load_ms"], (int, float)) or not math.isfinite(header["load_ms"]):
            raise WorkerError("invalid native transcription header")
    if len(records) != 1 or not isinstance(records[0], dict):
        raise WorkerError("invalid native transcription output")
    record = records[0]
    text = record.get("text")
    if record.get("file") != str(chunk) or "error" in record or not isinstance(text, str) or len(text) > 32000 or any(ord(c) < 32 and c not in "\n\t\r" for c in text):
        raise WorkerError("invalid native transcription output")
    return text.strip()


def transcribe(media, model, cfg, directory, run=run_process):
    duration = probe_duration(media, cfg, run)
    parts = []
    for index in range(math.ceil(duration / cfg.chunk_seconds)):
        chunk = directory / "chunk.wav"
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "ogg,mp3,wav,mov,matroska,webm,flac,aac", "-i", str(media),
             "-ss", str(index * cfg.chunk_seconds), "-t", str(cfg.chunk_seconds), "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "1", "-y", str(chunk)], 120)
        listing = directory / "batch.txt"
        listing.write_text(str(chunk) + "\n")
        output = run(["transcribe-cli", "-q", "-m", str(model), "--backend", "cpu", "--threads", str(cfg.threads), "--n-ctx", "2048", "--timestamps", "none", "--batch", str(listing), "--batch-size", "1", "--batch-jsonl"], 900)
        parts.append(parse_transcript(output, chunk))
        chunk.unlink(missing_ok=True)
        if sum(map(len, parts)) > 100000:
            raise WorkerError("transcript exceeded size limit")
    return {"text": "\n".join(part for part in parts if part), "language": None, "segments": [],
            "engine": "transcribe.cpp@" + PINS["runtime_revision"], "model": PINS["model"]}


def main():
    cfg = Config()
    # Do not fail an attempt which could not be claimed: 403/409 means the token
    # is stale; controller reconciliation handles the durable current attempt.
    job = cfg.api()
    try:
        key = validate_job(job, cfg)
        model = ensure_model(cfg)
        with tempfile.TemporaryDirectory(prefix="asr-") as temp:
            directory = Path(temp)
            media = directory / "original"
            download_media(cfg, key, media, expected_size=job["size"], expected_sha256=job["sha256"])
            result = transcribe(media, model, cfg, directory)
    except Exception as error:
        message = str(error) if isinstance(error, WorkerError) else "transcription processing failed"
        cfg.api("/fail", {"error": message[:200]})
        return 1
    cfg.api("/complete", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
