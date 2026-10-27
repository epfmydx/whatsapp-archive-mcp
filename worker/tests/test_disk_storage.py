"""Synthetic shared-disk downloads; no model or S3 requests."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_worker import worker, config

KEY = "accounts/personal/media/" + "a" * 64
CONTENT = b"synthetic audio"


class DiskStorageTests(unittest.TestCase):
    def test_config_selects_backend_and_validates_root(self):
        self.assertEqual(config().storage_backend, "s3")
        self.assertEqual(config(STORAGE_BACKEND="disk").media_root, "/media")
        for override in ({"STORAGE_BACKEND": "unknown"}, {"STORAGE_BACKEND": "disk", "MEDIA_ROOT": "relative"},
                         {"STORAGE_BACKEND": "disk", "MEDIA_ROOT": "/"}, {"STORAGE_BACKEND": "disk", "MEDIA_ROOT": "/media/../other"}):
            with self.subTest(override=override), self.assertRaises(worker.WorkerError): config(**override)

    def fixture(self, directory):
        root = directory.resolve() / "media"
        target = root / KEY
        target.parent.mkdir(parents=True)
        target.write_bytes(CONTENT)
        cfg = config(STORAGE_BACKEND="disk", MEDIA_ROOT=str(root))
        # Disk configuration and downloads require no S3 credentials/endpoint.
        cfg.env.pop("S3_BUCKET", None)
        self.assertNotIn("S3_ACCESS_KEY_ID", cfg.env)
        return root, target, cfg

    def download(self, cfg, destination, **kwargs):
        worker.download_media(cfg, KEY, destination, expected_size=len(CONTENT),
                              expected_sha256=kwargs.pop("sha256", hashlib.sha256(CONTENT).hexdigest()), **kwargs)

    def test_roundtrip_and_output_private(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            _, _, cfg = self.fixture(directory)
            dest = directory / "input"
            self.download(cfg, dest)
            self.assertEqual(dest.read_bytes(), CONTENT)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(directory.glob(".media-*")), [])

    def test_integrity_or_size_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            _, target, cfg = self.fixture(directory)
            dest = directory / "input"
            dest.write_bytes(b"previous verified file")
            for value in (b"short", b"X" * len(CONTENT), CONTENT + b"extra"):
                target.write_bytes(value)
                with self.assertRaises(worker.WorkerError): self.download(cfg, dest)
                self.assertEqual(dest.read_bytes(), b"previous verified file")
                self.assertEqual(list(directory.glob(".media-*")), [])
            target.write_bytes(CONTENT)
            with self.assertRaises(worker.WorkerError): self.download(cfg, dest, sha256="b" * 64)
            cfg.max_bytes = 1
            with self.assertRaises(worker.WorkerError): self.download(cfg, dest)

    def test_unsafe_references_and_non_regular_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            root, target, cfg = self.fixture(directory)
            for key in ("../secret", "accounts/personal/media/../../secret", KEY + "/extra", "/etc/passwd"):
                with self.subTest(key=key), self.assertRaises(worker.WorkerError): worker.disk_media(cfg, key)
            outside = directory / "outside"
            outside.write_bytes(CONTENT)
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaises(worker.WorkerError): self.download(cfg, directory / "input")
            target.unlink()
            os.mkfifo(target)
            with self.assertRaises(worker.WorkerError): self.download(cfg, directory / "input")
            target.unlink()
            target.write_bytes(CONTENT)
            account = root / "accounts/personal"
            account.rename(account.with_name("other"))
            account.symlink_to(account.with_name("other"), target_is_directory=True)
            with self.assertRaises(worker.WorkerError): self.download(cfg, directory / "input")

    def test_failed_output_write_cleans_only_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            _, _, cfg = self.fixture(directory)
            dest = directory / "input"
            dest.write_bytes(b"previous")
            with patch.object(worker.os, "replace", side_effect=OSError("synthetic")), self.assertRaises(OSError):
                self.download(cfg, dest)
            self.assertEqual(dest.read_bytes(), b"previous")
            self.assertEqual(list(directory.glob(".media-*")), [])
