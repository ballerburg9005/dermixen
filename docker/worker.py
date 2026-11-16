#!/usr/bin/env python3
"""Unprivileged Dermixen queue worker. Pulls jobs, renders, reports via HTTPS.

The worker never serves HTTP, receives arbitrary source URLs, or modifies
WordPress. Audio is read only from /media, output is written only to /output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MEDIA = Path(os.getenv("DCMIX_MEDIA_DIR", "/media")).resolve()
WORK = Path(os.getenv("DCMIX_WORK_DIR", "/work")).resolve()
OUTPUT = Path(os.getenv("DCMIX_OUTPUT_DIR", "/output")).resolve()
PRIVATE_OUTPUT = Path(os.getenv("DCMIX_PRIVATE_OUTPUT_DIR", "/private-output")).resolve()
API = os.getenv("DCMIX_API", "https://dancecore.party/wp-json/dcmix/v1").rstrip("/")
POLL = max(5, min(300, int(os.getenv("DCMIX_POLL_SECONDS", "30"))))
TOKEN = os.getenv("DCMIX_WORKER_TOKEN", "")
MAX_INPUTS = 60
MAX_AUDIO_BYTES = 4 * 1024 ** 3
SUPPORTED = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
FILE_PATTERN = re.compile(r"^20[0-9]{2}/(?:0[1-9]|1[0-2])/[A-Za-z0-9_ .()+-]{1,190}\.(?:mp3|wav|flac|m4a|aac|ogg|opus)$", re.I)
IDENTIFIER = re.compile(r"^[a-f0-9]{8}-[a-f0-9-]{27,36}$")
STOP = False


def stop(_signum, _frame):
    global STOP
    STOP = True


def request(endpoint: str, payload: dict) -> dict:
    """Authenticate over HTTPS. Keep credentials entirely out of logs."""
    if not TOKEN:
        raise RuntimeError("DCMIX_WORKER_TOKEN is required for poll mode")
    if not API.startswith("https://"):
        raise RuntimeError("DCMIX_API must use HTTPS")
    body = json.dumps(payload, separators=(",", ":")).encode()
    req = Request(API + "/" + endpoint, data=body, method="POST",
                  headers={"Authorization": "Bearer " + TOKEN,
                           "Content-Type": "application/json",
                           "User-Agent": "Dancecore-Dermixen/1"})
    try:
        with urlopen(req, timeout=35) as response:
            data = response.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise ValueError("WordPress response exceeded 256 KiB")
            response_obj = json.loads(data)
            if not isinstance(response_obj, dict):
                raise ValueError("Unexpected WordPress response")
            return response_obj
    except HTTPError as error:
        raise RuntimeError("WordPress " + endpoint + " HTTP " + str(error.code)) from error
    except URLError as error:
        raise RuntimeError("WordPress unavailable: " + str(error.reason)) from error


def source_path(relative: str) -> Path:
    """Only WordPress year/month audio paths under the read-only media mount."""
    if not isinstance(relative, str) or not FILE_PATTERN.fullmatch(relative):
        raise ValueError("Invalid or unapproved relative media path")
    source = (MEDIA / relative).resolve(strict=True)
    if not source.is_relative_to(MEDIA) or not source.is_file():
        raise ValueError("Media path escaped the read-only media mount")
    if source.suffix.lower() not in SUPPORTED or source.stat().st_size == 0:
        raise ValueError("Invalid media audio file")
    return source


def validate_job(job: dict) -> tuple[str, list[Path], int, int, int, str]:
    if not isinstance(job, dict):
        raise ValueError("Job must be an object")
    job_id = job.get("id")
    if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id):
        raise ValueError("Invalid job identifier")
    # Only WordPress chooses source files; never accept URLs or shell arguments.
    tracks = job.get("tracks")
    if not isinstance(tracks, list) or not 2 <= len(tracks) <= MAX_INPUTS:
        raise ValueError("Job must contain 2-60 tracks")
    paths = []
    for item in tracks:
        if not isinstance(item, dict) or set(item) - {"media_path", "sha256", "track_id"}:
            raise ValueError("Unexpected source fields")
        source = source_path(item.get("media_path"))
        declared_sha = item.get("sha256")
        if declared_sha is not None:
            if not isinstance(declared_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", declared_sha):
                raise ValueError("Invalid source hash")
            digest = hashlib.file_digest(source.open("rb"), "sha256").hexdigest()
            if digest != declared_sha:
                raise ValueError("Source checksum mismatch")
        paths.append(source)
    if len(set(paths)) != len(paths):
        raise ValueError("A track is duplicated")
    if sum(p.stat().st_size for p in paths) > MAX_AUDIO_BYTES:
        raise ValueError("Job exceeds total input size budget")
    bars = job.get("bars", 8)
    advance = job.get("advance_bars", 4)
    seed = job.get("seed", 42)
    for name, value, low, high in (("bars", bars, 1, 16),
                                    ("advance_bars", advance, 0, 8),
                                    ("seed", seed, 0, 2**31-1)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer from {low} to {high}")
    visibility = job.get("visibility", "public")
    if visibility not in ("public", "private") or not isinstance(visibility, str):
        raise ValueError("Invalid output visibility")
    return job_id, paths, bars, advance, seed, visibility


def execute_job(job: dict) -> dict:
    job_id, paths, bars, advance, seed, visibility = validate_job(job)
    jobdir = WORK / "jobs" / job_id
    jobdir.mkdir(parents=True, exist_ok=False)
    try:
        sources = jobdir / "sources.json"
        sources.write_text(json.dumps([str(p) for p in paths]), encoding="utf-8")
        result_mp3 = jobdir / (job_id + ".mp3")
        command = [
            sys.executable, "/app/scripts/mix-random.py", str(MEDIA),
            "--files-json", str(sources), "-o", str(result_mp3),
            "--seed", str(seed), "--bars", str(bars),
            "--advance-bars", str(advance), "--master", "original",
            "--dermixen", "/usr/local/bin/dermixen",
        ]
        print(f"rendering {job_id}: {len(paths)} verified tracks", flush=True)
        with subprocess.Popen(command, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              start_new_session=True) as process:
            started = time.monotonic()
            assert process.stdout is not None
            for line in process.stdout:
                print(line.rstrip(), flush=True)
                if time.monotonic() - started > 10800 or STOP:
                    os.killpg(process.pid, signal.SIGTERM)
                    raise RuntimeError("Job exceeded 3-hour limit or worker was stopped")
            returncode = process.wait(timeout=20)
            if returncode:
                raise RuntimeError(f"Dermixen renderer failed with exit {returncode}")
        if not result_mp3.is_file() or result_mp3.stat().st_size < 1024:
            raise RuntimeError("No valid MP3 was produced")
        digest = hashlib.file_digest(result_mp3.open("rb"), "sha256").hexdigest()
        playlist = json.loads(result_mp3.with_suffix(".tracklist.json").read_text())
        transitions = playlist.get("transitions", [])
        # Private and public outputs go to disjoint bind mounts. Never stage
        # private audio in the public origin's media directory, even briefly.
        root = PRIVATE_OUTPUT if visibility == "private" else OUTPUT
        root.mkdir(parents=True, exist_ok=True)
        destination = root / (job_id + ".mp3")
        staging = root / ("." + job_id + ".upload")
        if destination.exists():
            raise RuntimeError("An output with this identifier already exists")
        try:
            with result_mp3.open("rb") as inp, staging.open("xb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(staging, destination)
            if visibility == "private":
                destination.chmod(0o640)
        finally:
            staging.unlink(missing_ok=True)
        print(f"published {destination.name}: {destination.stat().st_size} bytes", flush=True)
        return {"id": job_id, "status": "completed",
                "output_path": ("private-mixes/" if visibility == "private" else "generated-mixes/") + destination.name,
                "sha256": digest, "bytes": destination.stat().st_size,
                "transitions": transitions}
    finally:
        # Keep finished media, delete disposable WAV/project/cache files.
        shutil.rmtree(jobdir, ignore_errors=True)


def receipts_dir() -> Path:
    directory = WORK / "pending-reports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def post_receipts() -> None:
    for receipt in sorted(receipts_dir().glob("*.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            request("worker/report", payload)
            receipt.unlink()
            print("reported " + str(payload.get("id")) + ": " +
                  str(payload.get("status")), flush=True)
        except (RuntimeError, OSError, ValueError) as error:
            print(f"report pending ({receipt.name}): {error}", file=sys.stderr)
            return


def poll() -> None:
    if not TOKEN:
        raise RuntimeError("Set DCMIX_WORKER_TOKEN in the env file before starting poll mode")
    print("Dermixen worker: polling " + API + " (no inbound port)", flush=True)
    while not STOP:
        try:
            post_receipts()
            if list(receipts_dir().glob("*.json")):
                time.sleep(POLL)
                continue
            answer = request("worker/claim", {"worker": "dermixen-v1"})
            job = answer.get("job")
            if job is not None:
                identifier = job.get("id") if isinstance(job, dict) else None
                try:
                    report = execute_job(job)
                except Exception as error:
                    print("job failed: " + str(error), file=sys.stderr, flush=True)
                    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
                        raise
                    report = {"id": identifier, "status": "failed",
                              "error": str(error)[:250]}
                file = receipts_dir() / (report["id"] + ".json")
                file.write_text(json.dumps(report), encoding="utf-8")
                post_receipts()
        except (RuntimeError, OSError, ValueError, TypeError) as error:
            print("worker: " + str(error), file=sys.stderr, flush=True)
        for _ in range(POLL):
            if STOP:
                break
            time.sleep(1)


def local(args: argparse.Namespace) -> None:
    """Local proof-of-function with a mounted folder, no WordPress/token."""
    path = Path(args.folder).resolve()
    if not path.is_dir():
        raise ValueError("Input folder does not exist")
    command = [sys.executable, "/app/scripts/mix-random.py", str(path),
               "-o", str(Path(args.output).resolve()), "--bars", str(args.bars),
               "--advance-bars", str(args.advance_bars),
               "--master", "original", "--dermixen", "/usr/local/bin/dermixen"]
    subprocess.run(command, check=True)


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("poll", help="Claim verified WordPress jobs and report completion")
    sub.add_parser("health", help="Confirm binaries are installed; does not need network")
    test = sub.add_parser("local", help="Render mounted folder locally, no token")
    test.add_argument("folder")
    test.add_argument("-o", "--output", default="/work/test-mix.mp3")
    test.add_argument("--bars", type=int, default=8)
    test.add_argument("--advance-bars", type=int, default=4)
    args = parser.parse_args()
    if args.mode == "health":
        subprocess.run(["/usr/local/bin/dermixen", "--help"], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL)
        print("Dermixen and FFmpeg present; target mounts are " +
              ", ".join(map(str, (MEDIA, WORK, OUTPUT))))
    elif args.mode == "local":
        local(args)
    else:
        poll()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print("fatal: " + str(error), file=sys.stderr)
        sys.exit(1)