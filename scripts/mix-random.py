#!/usr/bin/env python3
"""Build a randomized DJ mix from a folder using Dermixen's existing engine.

This is a CLI orchestrator, not an audio mixer. It delegates analysis,
beatmatching, transitions, and rendering to the Dermixen binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import subprocess
import sys

NATIVE = {'.mp3', '.wav', '.flac', '.m4a', '.mp4'}
CONVERT = {'.ogg', '.oga', '.opus', '.webm', '.aac', '.wma', '.aif', '.aiff'}
SUPPORTED = NATIVE | CONVERT


def find_dermixen(specified: str | None) -> str:
    if specified:
        path = shutil.which(specified)
        if path:
            return path
        raise RuntimeError(f'Dermixen executable not found: {specified}')
    repo = Path(__file__).resolve().parent.parent
    for candidate in (repo / 'target/release/dermixen', repo / 'target/debug/dermixen'):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which('dermixen')
    if found:
        return found
    raise RuntimeError('Dermixen binary not found. Build with: cargo build -p dermixen-cli --release')


def run(command: list[str]) -> None:
    print('+', shlex.join(command), flush=True)
    subprocess.run(command, check=True)


def discover(folder: Path, output: Path, cache: Path) -> list[Path]:
    files = []
    for path in folder.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        absolute = path.resolve()
        if absolute == output or cache in absolute.parents:
            continue
        files.append(absolute)
    return sorted(set(files), key=lambda path: str(path))


def transcode(source: Path, cache: Path) -> Path:
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg is required for OGG/Opus/WebM/AAC/WMA/AIFF inputs')
    cache.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:20]
    dest = cache / (digest + '.wav')
    if dest.is_file() and dest.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return dest
    tmp = cache / (digest + '.partial.wav')
    try:
        run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
             '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '2', '-ar', '44100',
             '-c:a', 'pcm_s16le', str(tmp)])
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Music folder (scanned recursively)')
    parser.add_argument('-o', '--output', type=Path, default=Path('random-mix.mp3'),
                        help='Output MP3 or WAV (default: random-mix.mp3)')
    parser.add_argument('--seed', type=int, help='Random seed to reproduce track order')
    parser.add_argument('--limit', type=int, help='Maximum tracks; default: all')
    parser.add_argument('--preset', choices=('beatmix', 'bass-swap', 'blend', 'cut'),
                        default='beatmix', help='Dermixen transition preset (default: beatmix)')
    parser.add_argument('--bars', type=int, default=8,
                        help='Transition length in bars for beatmix/bass-swap (default: 8)')
    parser.add_argument('--force', action='store_true', help='Replace an earlier mix with the same name')
    parser.add_argument('--dermixen', help='Path to an existing Dermixen CLI executable')
    args = parser.parse_args(argv)

    folder = args.folder.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not folder.is_dir():
        parser.error(f'not a directory: {folder}')
    if output.suffix.lower() not in ('.mp3', '.wav'):
        parser.error('output must end with .mp3 or .wav')
    if args.limit is not None and args.limit < 2:
        parser.error('--limit must be at least 2')
    if args.bars < 1 or args.bars > 32:
        parser.error('--bars must be between 1 and 32')
    document = output.with_suffix('.dmx')
    manifest = output.with_suffix('.tracklist.json')
    cache = output.with_suffix('.sources')
    tracks = discover(folder, output, cache)
    if len(tracks) < 2:
        parser.error(f'found only {len(tracks)} supported audio file(s); need at least two')
    if not args.force and any(path.exists() for path in (output, document)):
        parser.error(f'{output} or {document} already exists (use --force to replace)')
    if document in tracks:
        parser.error('output mix document must not be an input file')

    try:
        dermixen = find_dermixen(args.dermixen)
        order = list(tracks)
        rng = random.Random(args.seed)
        rng.shuffle(order)
        if args.limit is not None:
            order = order[:args.limit]
        print(f'Found {len(tracks)} audio files; mixing {len(order)} in randomized order.', flush=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        # The mix document is preserved, as is the converted-source cache;
        # both allow preview, editing, and reproducible later renders.
        if document.exists():
            document.unlink()
        run([dermixen, 'mix', 'new', str(document)])
        playlist = []
        for index, source in enumerate(order, 1):
            prepared = source if source.suffix.lower() in NATIVE else transcode(source, cache)
            print(f'[{index}/{len(order)}] {source}', flush=True)
            run([dermixen, 'mix', 'add', str(document), str(prepared),
                 '--preset', args.preset, '--bars', str(args.bars)])
            playlist.append({'source': str(source), 'analyzed_file': str(prepared)})
        run([dermixen, 'render', str(document), str(output)])
        manifest.write_text(json.dumps({'preset': args.preset, 'bars': args.bars,
                                        'seed': args.seed, 'tracks': playlist}, indent=2) + '\n',
                            encoding='utf-8')
        print(f'Finished: {output}\nEditable mix: {document}\nTracklist: {manifest}', flush=True)
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
