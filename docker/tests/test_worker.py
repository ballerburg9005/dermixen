"""Security and job-protocol smoke tests for the container worker."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

WORKER = Path(__file__).parents[1] / "worker.py"
spec = importlib.util.spec_from_file_location("dcmix_worker", WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        (self.media / "2026" / "11").mkdir(parents=True)
        self.song1 = self.media / "2026/11/first.mp3"
        self.song2 = self.media / "2026/11/second.mp3"
        self.song1.write_bytes(b"music-a")
        self.song2.write_bytes(b"music-b")
        self.patch_media = patch.object(worker, "MEDIA", self.media.resolve())
        self.patch_media.start()
        self.addCleanup(self.patch_media.stop)
        self.good = {"id": "391f4526-03de-4096-8872-156857381111",
                     "tracks": [
                       {"media_path": "2026/11/first.mp3",
                        "sha256": hashlib.sha256(b"music-a").hexdigest()},
                       {"media_path": "2026/11/second.mp3"}],
                     "bars": 8, "advance_bars": 4, "seed": 42}

    def test_valid_job_paths_and_checksum(self):
        identifier, files, bars, earlier, seed, visibility = worker.validate_job(self.good)
        self.assertEqual(identifier, self.good["id"])
        self.assertEqual(files, [self.song1, self.song2])
        self.assertEqual((bars, earlier, seed), (8, 4, 42))
        self.assertEqual(visibility, "public")

    def test_private_visibility_and_reject_untrusted_modes(self):
        job = dict(self.good, visibility="private")
        self.assertEqual(worker.validate_job(job)[-1], "private")
        for value in ("something-else", "../private", 1, None, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    worker.validate_job(dict(self.good, visibility=value))

    def test_traversal_and_unapproved_media_rejected(self):
        for bad in ["../2026/11/first.mp3", "/etc/passwd", "other.mp3",
                    "2026/11/../../secret.mp3", "2026/11/danger.php",
                    "https://evil.example/music.mp3"]:
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    worker.source_path(bad)

    def test_symlink_escape_rejected(self):
        other = self.root / "outside.mp3"
        other.write_bytes(b"out")
        (self.media / "2026/11/escape.mp3").symlink_to(other)
        with self.assertRaises(ValueError):
            worker.source_path("2026/11/escape.mp3")

    def test_duplicate_and_wrong_sha_rejected(self):
        duplicate = json.loads(json.dumps(self.good))
        duplicate["tracks"].append({"media_path": "2026/11/first.mp3"})
        with self.assertRaisesRegex(ValueError, "duplicated"):
            worker.validate_job(duplicate)
        altered = json.loads(json.dumps(self.good))
        altered["tracks"][0]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            worker.validate_job(altered)

    def test_untrusted_arguments_and_excessive_tracks_rejected(self):
        bad = json.loads(json.dumps(self.good))
        bad["tracks"][0]["url"] = "https://evil.example/"
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            worker.validate_job(bad)
        bad = json.loads(json.dumps(self.good))
        bad["bars"] = 500
        with self.assertRaises(ValueError):
            worker.validate_job(bad)
        bad = json.loads(json.dumps(self.good))
        bad["tracks"] = bad["tracks"] * 40
        with self.assertRaises(ValueError):
            worker.validate_job(bad)

    def test_token_required_before_external_requests(self):
        with patch.object(worker, "TOKEN", ""):
            with self.assertRaisesRegex(RuntimeError, "TOKEN"):
                worker.request("worker/claim", {"worker": "test"})


if __name__ == "__main__":
    unittest.main()