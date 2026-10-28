"""Synthetic fixtures and fake native subprocesses; these do not measure ASR quality."""
import io
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker


def config(**extra):
    return worker.Config({"JOB_ID": "932a7034-bc96-4356-b886-a9b6e9a44959", "JOB_TOKEN": "synthetic-attempt-token", "INTERNAL_TOKEN": "synthetic-internal", "APP_INTERNAL_URL": "http://app:8000", "S3_BUCKET": "synthetic", **extra})


class WorkerTests(unittest.TestCase):
    def test_attempt_token_and_bounds(self):
        for override in ({"JOB_TOKEN": "bad\nheader"}, {"WORKER_THREADS": "0"}, {"AUDIO_CHUNK_SECONDS": "31"}, {"MAX_AUDIO_SECONDS": "1801"}, {"MODEL_URL": "http://untrusted/model"}, {"MODEL_SHA256": "0" * 64}, {"APP_INTERNAL_URL": "http://user:pass@app"}):
            with self.subTest(override=override), self.assertRaises(worker.WorkerError):
                config(**override)

    def test_threads_are_positive_integers_without_fixed_server_ceiling(self):
        self.assertEqual(config().threads, 1)
        for count in (1, 2, 4, 16, 64, 256, 999):
            with self.subTest(count=count):
                self.assertEqual(config(WORKER_THREADS=str(count)).threads, count)
        for value in ("0", "-1", "", "1.5", "2e3", "true", "NaN", None, True, False, 1.5):
            with self.subTest(value=value), self.assertRaises(worker.WorkerError):
                config(WORKER_THREADS=value)

    def test_configured_thread_count_reaches_native_executable(self):
        for count in (4, 64, 256):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                calls = []

                def fake(args, timeout):
                    calls.append(args)
                    if args[0] == "ffprobe":
                        return '{"format":{"duration":"11.0"}}'
                    if args[0] == "ffmpeg":
                        return ""
                    return json.dumps({"file": str(root / "chunk.wav"), "text": "synthetic"})

                worker.transcribe(root / "audio", root / "model", config(WORKER_THREADS=str(count)), root, fake)
                native = next(args for args in calls if args[0] == "transcribe-cli")
                self.assertEqual(native[native.index("--threads") + 1], str(count))

    def test_claim_cannot_reference_other_account_or_arbitrary_url(self):
        cfg = config()
        valid = {"id": cfg.job_id, "account_id": "personal", "object_key": "accounts/personal/media/synthetic", "mime_type": "audio/ogg", "size": 4, "sha256": hashlib.sha256(b"1234").hexdigest()}
        self.assertEqual(worker.validate_job(valid, cfg), valid["object_key"])
        for key in ("https://untrusted/audio", "accounts/other/media/a", "accounts/personal/media/../secret"):
            with self.assertRaises(worker.WorkerError):
                worker.validate_job({**valid, "object_key": key}, cfg)
        for fields in ({"size": True}, {"size": 0}, {"size": cfg.max_bytes + 1}, {"size": None}, {"sha256": None}, {"sha256": "invalid"}):
            with self.subTest(fields=fields), self.assertRaises(worker.WorkerError):
                worker.validate_job({**valid, **fields}, cfg)

    def test_download_checks_declared_and_actual_size(self):
        class FakeS3:
            def get_object(self, **kwargs):
                return {"ContentLength": 4, "Body": io.BytesIO(b"12345")}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(worker.WorkerError):
            worker.download_media(config(MAX_MEDIA_BYTES="4"), "fixture", Path(tmp) / "input", expected_size=4, expected_sha256=hashlib.sha256(b"1234").hexdigest(), client=FakeS3())

    def test_download_rejects_same_length_substitution_and_cleans_partial_file(self):
        original = b"original audio"
        substituted = b"replaced audio"
        body = io.BytesIO(substituted)
        client = unittest.mock.MagicMock()
        client.get_object.return_value = {"ContentLength": len(original), "Body": body}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "input"
            with self.assertRaisesRegex(worker.WorkerError, "integrity check failed"):
                worker.download_media(config(), "fixture", dest, expected_size=len(original), expected_sha256=hashlib.sha256(original).hexdigest(), client=client)
            self.assertFalse(dest.exists())
        self.assertTrue(body.closed)

    def test_download_matches_authenticated_size_and_hash(self):
        original = b"original audio"
        for declared_size, content in ((len(original), original), (len(original) + 1, original), (len(original), original[:-1])):
            with self.subTest(declared_size=declared_size, content=content), tempfile.TemporaryDirectory() as tmp:
                body = io.BytesIO(content)
                client = unittest.mock.MagicMock()
                client.get_object.return_value = {"ContentLength": declared_size, "Body": body}
                dest = Path(tmp) / "input"
                kwargs = dict(expected_size=len(original), expected_sha256=hashlib.sha256(original).hexdigest().upper(), client=client)
                if declared_size == len(original) and content == original:
                    worker.download_media(config(), "fixture", dest, **kwargs)
                    self.assertEqual(dest.read_bytes(), original)
                else:
                    with self.assertRaises(worker.WorkerError):
                        worker.download_media(config(), "fixture", dest, **kwargs)
                    self.assertFalse(dest.exists())
                self.assertTrue(body.closed)

    def test_duration_limits_reject_nonfinite_and_overlong(self):
        for duration in ("NaN", "Infinity", "1801", "0", "-1"):
            with self.subTest(duration=duration), self.assertRaises(worker.WorkerError):
                worker.probe_duration(Path("fixture"), config(), lambda *a: json.dumps({"format": {"duration": duration}}))

    def test_fake_subprocess_chunking_and_no_invented_timestamps(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def fake(args, timeout):
                calls.append(args)
                if args[0] == "ffprobe":
                    return '{"format":{"duration":"31.0"}}'
                if args[0] == "ffmpeg":
                    return ""
                return '{"type":"batch_header","load_ms":10.0}\n' + json.dumps({"file": str(root / "chunk.wav"), "text": "synthetic fixture"})
            result = worker.transcribe(root / "input", root / "model", config(), root, fake)
        self.assertEqual(result["text"], "synthetic fixture\nsynthetic fixture")
        self.assertEqual(result["segments"], [])
        self.assertIsNone(result["language"])
        native = [c for c in calls if c[0] == "transcribe-cli"]
        self.assertEqual(len(native), 2)
        self.assertEqual(native[0][native[0].index("--backend") + 1], "cpu")
        self.assertEqual(native[0][native[0].index("--threads") + 1], "1")

    def test_native_errors_and_malformed_output_are_never_success(self):
        for output in ('not-json', '{"file":"chunk.wav","text":"partial","error":"truncated"}', '{"file":"wrong","text":"valid"}', '[]'):
            with self.subTest(output=output), self.assertRaises(worker.WorkerError):
                worker.parse_transcript(output, Path("chunk.wav"))
        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1)
        with self.assertRaises(worker.WorkerError):
            worker.run_process(["synthetic-failing-executable"], 1, fake)

    def test_callback_contains_attempt_headers_and_private_route(self):
        cfg = config()
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        opener = unittest.mock.MagicMock()
        opener.open.return_value = response
        with patch.object(worker, "build_opener", return_value=opener):
            cfg.api("/complete", {"text": "synthetic", "segments": []})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://app:8000/internal/jobs/" + cfg.job_id + "/complete")
        self.assertEqual(request.get_header("X-job-token"), cfg.token)
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-internal")
        self.assertEqual(request.get_method(), "POST")

    def test_model_cache_hash_failure_never_installs(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg.model_path = Path(tmp) / "model.gguf"
            stream = unittest.mock.MagicMock()
            stream.__enter__.return_value.read.side_effect = [b"not-a-model", b""]
            with patch.object(worker, "urlopen", return_value=stream), self.assertRaises(worker.WorkerError):
                worker.ensure_model(cfg)
            self.assertFalse(cfg.model_path.exists())
            self.assertFalse(cfg.model_path.with_suffix(".download").exists())


if __name__ == "__main__":
    unittest.main()
