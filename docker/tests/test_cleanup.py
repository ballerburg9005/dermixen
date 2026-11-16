"""Offline safety tests for generated mix deletion.

These tests never contact WordPress and only manipulate temporary MP3 files.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CLEANUP = Path(__file__).resolve().parents[1] / "cleanup.py"
spec = importlib.util.spec_from_file_location("dcmix_cleanup", CLEANUP)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class TestGeneratedMixCleanup(unittest.TestCase):
    JOB = "f92f6921-a904-4689-9219-6df4582056d5"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patcher = patch.object(cleanup, "ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.path = self.root / (self.JOB + ".mp3")
        self.path.write_bytes(b"test-generated-audio" * 120)
        self.other = self.root / "other-mix.mp3"
        self.other.write_bytes(b"untouched audio" * 20)
        self.item = {
            "id": self.JOB,
            "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }

    def test_deletes_only_requested_matching_generated_mix(self):
        self.assertTrue(cleanup.cleanup(self.item))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.other.exists())
        self.assertEqual(self.other.read_bytes(), b"untouched audio" * 20)

    def test_missing_file_is_idempotent(self):
        self.path.unlink()
        self.assertTrue(cleanup.cleanup(self.item))
        self.assertTrue(self.other.exists())

    def test_wrong_digest_preserves_file(self):
        bad = dict(self.item, sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "differs"):
            cleanup.cleanup(bad)
        self.assertTrue(self.path.exists())

    def test_invalid_or_extra_fields_rejected(self):
        bad_ids = ["../" + self.JOB, "../../etc/passwd",
                   "not-a-uuid", self.JOB + ".mp3",
                   self.JOB.upper(), "f" * 36]
        for bad_id in bad_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    cleanup.cleanup(dict(self.item, id=bad_id))
        with self.assertRaises(ValueError):
            cleanup.cleanup(dict(self.item, arbitrary_path="/etc/passwd"))
        self.assertTrue(self.path.exists())

    def test_symlink_target_not_removed(self):
        outside = self.root.parent / (self.root.name + "-other.mp3")
        outside.write_bytes(b"unrelated")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            cleanup.cleanup(self.item)
        self.assertEqual(outside.read_bytes(), b"unrelated")


if __name__ == "__main__":
    unittest.main()
