#!/usr/bin/env python3
"""Delete only authenticated, trashed Dermixen output files from Syburg.

One-shot janitor, intended to run every minute via systemd timer. No inbound
port and no WordPress database credentials are necessary. The worker token
stays in root's mode-0600 env file and is never printed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(os.environ.get("DCMIX_OUTPUT_ROOT", "/srv/dancecore/media/generated-mixes"))
ENV_FILE = Path(os.environ.get("DCMIX_WORKER_ENV", "/root/dancecore-dermixen-runtime/worker.env"))
BASE = "https://dancecore.party/wp-json/dcmix/v1/"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA = re.compile(r"^[a-f0-9]{64}$")


def secret() -> str:
    for line in ENV_FILE.read_text(encoding="ascii").splitlines():
        if line.startswith("DCMIX_WORKER_TOKEN="):
            value = line.partition("=")[2]
            if SHA.fullmatch(value):
                return value
    raise RuntimeError("Missing worker token")


def api(token: str, endpoint: str, body: dict) -> dict:
    req = Request(BASE + endpoint, method="POST",
                  headers={"Authorization": "Bearer " + token,
                           "Content-Type": "application/json",
                           "User-Agent": "Dancecore-Mix-Janitor/1"},
                  data=json.dumps(body).encode("utf-8"))
    try:
        with urlopen(req, timeout=25) as response:
            content = response.read(512_000)
            return json.loads(content)
    except HTTPError as error:
        raise RuntimeError(endpoint + " returned HTTP " + str(error.code)) from error
    except URLError as error:
        raise RuntimeError(endpoint + " unavailable") from error


def cleanup(item: dict) -> bool:
    if not isinstance(item, dict) or set(item) != {"id", "sha256"}:
        raise ValueError("Unexpected tombstone format")
    identifier, digest = item["id"], item["sha256"]
    if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
        raise ValueError("Unapproved output UUID")
    if not isinstance(digest, str) or not SHA.fullmatch(digest):
        raise ValueError("Missing SHA-256")
    # The name must be exactly a UUID + .mp3 in this dedicated directory.
    folder = ROOT.resolve(strict=True)
    target = ROOT / (identifier + ".mp3")
    if target.parent.resolve(strict=True) != folder:
        raise ValueError("Output escaped storage mount")
    try:
        meta = target.lstat()
    except FileNotFoundError:
        print("already absent:", identifier)
        return True
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise ValueError("Refusing non-regular / hard-linked output")
    checksum = hashlib.sha256()
    with target.open("rb") as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            checksum.update(block)
    actual = checksum.hexdigest()
    if actual != digest:
        raise ValueError("Storage content differs from WordPress deletion receipt")
    target.unlink()
    print("deleted generated mix:", identifier)
    return True


def main() -> None:
    token = secret()
    reply = api(token, "worker/cleanup/list", {})
    items = reply.get("items")
    if not isinstance(items, list) or len(items) > 15:
        raise RuntimeError("Invalid cleanup list response")
    for item in items:
        try:
            if cleanup(item):
                ack = api(token, "worker/cleanup/ack", {"id": item["id"]})
                if ack.get("status") != "deleted":
                    raise RuntimeError("Unexpected cleanup receipt")
                print("acknowledged:", item["id"])
        except (ValueError, RuntimeError, OSError) as error:
            print("cleanup refused:", str(error), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print("cleanup unavailable:", str(error), file=sys.stderr)
        sys.exit(1)
