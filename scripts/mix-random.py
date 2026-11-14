#!/usr/bin/env python3
"""Create a randomized folder mix with Dermixen, without losing short tracks.

This orchestrates Dermixen's existing analysis, beatmatched transitions, and
rendering. FFmpeg is only used for unsupported input codecs and final mastering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

SAMPLE_RATE = 44100
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


def run(command: list[str], capture: bool = False) -> str:
    print('+', shlex.join(command), flush=True)
    result = subprocess.run(command, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ''


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
    dest = cache / (digest + '.f32.wav')
    if dest.is_file() and dest.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return dest
    tmp = cache / (digest + '.partial.f32.wav')
    try:
        run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
             '-i', str(source), '-map', '0:a:0', '-vn', '-ac', '2', '-ar', '44100',
             '-c:a', 'pcm_f32le', str(tmp)])
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def analyze(dermixen: str, path: Path) -> dict:
    """Ask Dermixen itself for the grid, sample duration and sound extent."""
    record = json.loads(run([dermixen, 'analyze', str(path), '--json'], capture=True))
    if not isinstance(record, dict):
        raise RuntimeError(f'invalid analysis record for {path}')
    return record


def trailing_silence_start(source: Path, duration: float) -> float | None:
    """Locate a genuinely silent *ending*, not a musical break in the middle.

    Unlike Dermixen's full-song extent/anchor guess, this only skips a tail
    that FFmpeg confirms stays silent right up to the recording's end.
    """
    if not shutil.which('ffmpeg'):
        return None
    try:
        completed = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-i', str(source),
             '-af', 'silencedetect=noise=-48dB:d=0.6', '-f', 'null', '-'],
            text=True, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    # FFmpeg may report a silence_end at EOF or leave the end open.
    starts = re.findall(r'silence_start:\s*([\d.]+)', completed.stderr)
    ends = re.findall(r'silence_end:\s*([\d.]+)', completed.stderr)
    if not starts:
        return None
    candidate = float(starts[-1])
    final_end = float(ends[-1]) if len(ends) >= len(starts) else duration
    if final_end < duration - 0.5 or duration - candidate < 1.5:
        return None
    # Never treat a long, nearly quiet composition as disposable.
    return max(duration * 0.70, min(duration, candidate))


def track_timing(record: dict, bars: int, outgoing_preset: str,
                 detected_silence_at: float | None = None,
                 advance_bars: int = 0) -> dict:
    """Conservative anchors: most music plays fully, even if kick anchors misfire.

    An analyzer's arbitrary early outro is ignored. A fade starts near the
    real file ending (or a verified trailing-silence boundary) and lasts
    exactly the preset's duration. The last notes fade rather than being cut.
    """
    length = int(record['length_samples'])
    grid = record['grid']
    bpm = float(grid['bpm'])
    first_beat = int(grid['first_beat_sample'])
    if length <= 0 or not math.isfinite(bpm) or not (20 <= bpm <= 999):
        raise RuntimeError('invalid decoded length or BPM from Dermixen')
    duration = length / SAMPLE_RATE
    sound_end = (duration if detected_silence_at is None else
                 max(duration * 0.70, min(duration, detected_silence_at)))
    # Crossfade reaches zero at the end of actual audio, not 7% before it.
    fade_beats = 0 if outgoing_preset == 'cut' else 4 * bars
    desired_fade_start = sound_end - 60.0 * fade_beats / bpm
    first_seconds = first_beat / SAMPLE_RATE
    outgoing_beat = (math.ceil if outgoing_preset == 'cut' else math.floor)
    outro_beat = outgoing_beat((desired_fade_start - first_seconds) * bpm / 60.0)
    last_beat = math.floor((duration - first_seconds) * bpm / 60.0)
    if outgoing_preset != 'cut':
        outro_beat -= 4 * advance_bars
    outro_beat = min(last_beat, max(1, outro_beat))
    if outro_beat <= fade_beats + 1:
        raise RuntimeError('track too short for requested transition bars; use --bars 1')
    return {
        'bpm': bpm, 'duration_seconds': duration, 'sound_end_seconds': sound_end,
        'trailing_silence_seconds': duration - sound_end,
        'outro_advance_bars': advance_bars if outgoing_preset != 'cut' else 0,
        'analyzed_intro_beat': (record.get('anchors') or {}).get('intro_beat'),
        'analyzed_outro_beat': (record.get('anchors') or {}).get('outro_beat'),
        'intro_beat': 0, 'outro_beat': outro_beat,
    }


def validate_layout(dermixen: str, document: Path, entries: list[dict],
                    expected_count: int) -> dict:
    """Refuse cached BPM mismatches and long unintended gaps before rendering.

    Dermixen persists its analyzed beat grids in a library. After the beat
    analyzer changes, that cache may still contain old half-time grids.
    Each mix must use the fresh grid on which its outro beats were calculated.
    """
    layout = json.loads(run([dermixen, 'mix', 'show', str(document), '--json'],
                            capture=True))
    placed = layout.get('tracks')
    if not isinstance(placed, list) or len(placed) != expected_count:
        raise RuntimeError('mix show returned the wrong number of tracks')
    for index, track in enumerate(placed):
        fresh = entries[index]['record']
        actual_grid = track['grid']
        intended_grid = fresh['grid']
        actual_bpm = float(actual_grid['bpm'])
        expected_bpm = float(intended_grid['bpm'])
        if abs(actual_bpm - expected_bpm) > max(0.01, expected_bpm * 0.001):
            raise RuntimeError(
                f'track {index + 1} has BPM {actual_bpm:.2f} in the mix, '
                f'but analysis gave {expected_bpm:.2f}. '
                'The beat grid is stale. Refusing to render silence.')
        actual_first = int(actual_grid['first_beat_sample'])
        expected_first = int(intended_grid['first_beat_sample'])
        if abs(actual_first - expected_first) > 2:
            raise RuntimeError(
                f'track {index + 1} first beat does not match the fresh analysis')
        if int(track['length_samples']) != int(fresh['length_samples']):
            raise RuntimeError(
                f'track {index + 1} source length does not match fresh analysis')
        start = float(track['start_seconds'])
        end = float(track['end_seconds'])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise RuntimeError(f'track {index + 1} has an invalid position')
        if index:
            last_end = float(placed[index - 1]['end_seconds'])
            if start > last_end + 1.0:
                raise RuntimeError(
                    f'INVALID MIX: {start - last_end:.1f}s gap between '
                    f'track {index} and track {index + 1}. '
                    'Rendering stopped. Check the beat grids and anchors.')
    return layout


def parse_advance_overrides(specifications: list[str], count: int) -> dict[int, int]:
    """Read 1-based outgoing handover numbers from N:BARS options."""
    overrides = {}
    for spec in specifications:
        match = re.fullmatch(r'([1-9][0-9]*):([0-9]+)', spec)
        if match is None:
            raise ValueError(f'--advance-transition {spec!r} must be N:BARS')
        index, bars = map(int, match.groups())
        if index >= count:
            raise ValueError(f'transition {index} does not exist in this {count}-track mix')
        if not 0 <= bars <= 8:
            raise ValueError('--advance-transition bars must be between 0 and 8')
        if index in overrides:
            raise ValueError(f'--advance-transition {index} was given twice')
        overrides[index] = bars
    return overrides


def mix_timestamp(seconds: float) -> str:
    tenths = round(max(0, seconds) * 10)
    minutes, rest = divmod(tenths, 600)
    return f'{minutes}:{rest / 10:04.1f}'


def transition_cues(layout: dict, entries: list[dict], output: Path) -> list[dict]:
    """Print seek points based on actual start/end positions in mix show."""
    lines = ['Transition cues (seek four seconds before each incoming track):']
    cues = []
    tracks = layout['tracks']
    for index in range(1, len(tracks)):
        incoming_at = float(tracks[index]['start_seconds'])
        outgoing_ends = float(tracks[index - 1]['end_seconds'])
        seek_at = max(0.0, incoming_at - 4.0)
        before = Path(entries[index - 1]['source']).stem
        after = Path(entries[index]['source']).stem
        lines.append(f'  {index}. SEEK {mix_timestamp(seek_at)}'
                     f'  | INCOMING {mix_timestamp(incoming_at)}'
                     f'  | previous source ends {mix_timestamp(outgoing_ends)}')
        lines.append(f'     {before} -> {after}')
        cues.append({
            'number': index, 'from': str(entries[index - 1]['source']),
            'to': str(entries[index]['source']),
            'seek_seconds': round(seek_at, 3),
            'incoming_start_seconds': round(incoming_at, 3),
            'previous_source_end_seconds': round(outgoing_ends, 3),
            'outro_advance_bars': entries[index - 1]['timing']['outro_advance_bars'],
        })
    path = output.with_suffix('.transitions.txt')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n' + '\n'.join(lines), flush=True)
    print(f'Saved transition cues: {path}', flush=True)
    return cues


def join_preset(requested: str, previous: dict, following: dict) -> str:
    if requested != 'auto':
        return requested
    # A short melodic or hard-ending dancecore track must never be cut off
    # simply because its neighbor has a different detected tempo. Use
    # Dermixen's real smooth beatmatched crossfade. Large mismatches still
    # require better phrase/tempo planning in a future engine iteration.
    return 'beatmix'


def master_uniform(source: Path, output: Path, ceiling_db: float) -> None:
    """Apply only a shared attenuation when the complete mix exceeds a ceiling.

    No track is individually normalized and there is no limiter/compressor.
    The floating-point intermediate retains over-range decoded MP3 samples.
    FFmpeg astats reads those samples without first clipping them to int16.
    """
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg is required for the unclipped float master')
    result = subprocess.run(
        ['ffmpeg', '-nostdin', '-hide_banner', '-i', str(source),
         '-af', 'astats=metadata=0:reset=0', '-f', 'null', '-'],
        check=True, capture_output=True, text=True)
    overall = result.stderr.rfind('] Overall')
    if overall < 0:
        raise RuntimeError('FFmpeg did not report overall floating-point peak statistics')
    matches = re.findall(r'Peak level dB:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)|-inf)',
                         result.stderr[overall:])
    if not matches:
        raise RuntimeError('FFmpeg did not report a floating-point peak level')
    peak_text = matches[0]
    measured_db = float(peak_text)
    if not math.isfinite(measured_db) and measured_db != -math.inf:
        raise RuntimeError(f'invalid floating-point master peak: {peak_text}')
    # Never raise the overall level. A single common attenuation protects
    # the export while preserving the recorded differences between songs.
    attenuation_db = min(0.0, ceiling_db - measured_db)
    print(f'Master: input peak {measured_db:+.2f} dBFS, '
          f'global attenuation {attenuation_db:+.2f} dB. '
          'Every track keeps its original relative level.',
          flush=True)
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               '-i', str(source), '-af', f'volume={attenuation_db:.6f}dB']
    if output.suffix.lower() == '.mp3':
        command += ['-c:a', 'libmp3lame', '-b:a', '320k', str(output)]
    else:
        command += ['-c:a', 'pcm_s16le', str(output)]
    run(command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Music folder (recursive)')
    parser.add_argument('-o', '--output', type=Path, default=Path('random-mix.mp3'))
    parser.add_argument('--seed', type=int, help='Random seed to reproduce order')
    parser.add_argument('--limit', type=int, help='Maximum tracks; default all')
    parser.add_argument('--preset', choices=('auto', 'beatmix', 'bass-swap', 'blend', 'cut'),
                        default='auto', help='Auto uses smooth beatmatched fades, never hard cuts')
    parser.add_argument('--bars', type=int, default=8,
                        help='Transition bars (default 8; about 6-11 seconds at 180-300 BPM)')
    parser.add_argument('--advance-bars', type=int, default=0,
                        help='Move all outgoing transitions this many bars earlier (default 0)')
    parser.add_argument('--advance-transition', action='append', default=[],
                        metavar='N:BARS',
                        help='Move only transition N by BARS, e.g. 2:2 for track 2 -> 3')
    parser.add_argument('--track-gain-db', type=float, default=0.0,
                        help='Same gain for all tracks, default 0 dB (original levels)')
    parser.add_argument('--master', choices=('safe', 'peak', 'none'), default='safe',
                        help='Safe: shared attenuation only, no compressor or boosts (default)')
    parser.add_argument('--peak-db', type=float, default=-1.0,
                        help='Export peak ceiling in dBFS (default -1)')
    parser.add_argument('--force', action='store_true', help='Replace existing mix/output')
    parser.add_argument('--dermixen', help='Existing Dermixen executable')
    args = parser.parse_args(argv)
    folder = args.folder.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not folder.is_dir():
        parser.error(f'not a directory: {folder}')
    if output.suffix.lower() not in ('.mp3', '.wav'):
        parser.error('output must end in .mp3 or .wav')
    if args.limit is not None and args.limit < 2:
        parser.error('--limit must be at least 2')
    if not 1 <= args.bars <= 16:
        parser.error('--bars must be between 1 and 16')
    if not 0 <= args.advance_bars <= 8:
        parser.error('--advance-bars must be between 0 and 8')
    if not math.isfinite(args.track_gain_db) or not -18 <= args.track_gain_db <= 0:
        parser.error('--track-gain-db must be between -18 and 0')
    if not math.isfinite(args.peak_db) or not -12 <= args.peak_db <= -0.5:
        parser.error('--peak-db must be between -12 and -0.5')
    document = output.with_suffix('.dmx')
    manifest = output.with_suffix('.tracklist.json')
    cache = output.with_suffix('.sources')
    tracks = discover(folder, output, cache)
    if len(tracks) < 2:
        parser.error(f'found only {len(tracks)} supported audio file(s); need two')
    if not args.force and any(path.exists() for path in (output, document)):
        parser.error(f'{output} or {document} exists (use --force to replace)')
    try:
        dermixen = find_dermixen(args.dermixen)
        rng = random.Random(args.seed)
        rng.shuffle(tracks)
        if args.limit is not None:
            tracks = tracks[:args.limit]
        advances = parse_advance_overrides(args.advance_transition, len(tracks))
        print(f'Mixing {len(tracks)} shuffled tracks with {args.bars}-bar fades, preserving track endings.', flush=True)
        output.parent.mkdir(parents=True, exist_ok=True)

        # Analyze the complete playlist before changing any files so every
        # track's outro is chosen knowing the transition to its successor.
        entries = []
        for index, source in enumerate(tracks, 1):
            prepared = source if source.suffix.lower() in NATIVE else transcode(source, cache)
            record = analyze(dermixen, prepared)
            entries.append({'source': source, 'prepared': prepared, 'record': record})
            print(f'Analyzed [{index}/{len(tracks)}]: {source.name}', flush=True)

        for index, entry in enumerate(entries):
            next_entry = entries[index + 1] if index + 1 < len(entries) else None
            if next_entry is None:
                outgoing_preset = 'cut'  # no outgoing transition on the last track
            else:
                a = {'bpm': float(entry['record']['grid']['bpm'])}
                b = {'bpm': float(next_entry['record']['grid']['bpm'])}
                outgoing_preset = join_preset(args.preset, a, b)
            entry['outgoing_preset'] = outgoing_preset
            advance = advances.get(index + 1, args.advance_bars) if next_entry else 0
            silence_at = (trailing_silence_start(entry['prepared'],
                                                  entry['record']['length_samples'] / SAMPLE_RATE)
                          if next_entry is not None else None)
            entry['timing'] = track_timing(entry['record'], args.bars,
                                           outgoing_preset, silence_at, advance)
            entry['track_gain_db'] = args.track_gain_db

        if document.exists():
            document.unlink()
        run([dermixen, 'mix', 'new', str(document)])
        playlist = []
        for index, entry in enumerate(entries):
            timing = entry['timing']
            incoming_preset = entries[index - 1]['outgoing_preset'] if index > 0 else 'cut'
            source = entry['source']
            print(f'[{index+1}/{len(entries)}] {source.name} | {timing["bpm"]:.1f} BPM | '
                  f'{incoming_preset} in | outro at beat {timing["outro_beat"]} '
                  f'(analyzer suggested {timing["analyzed_outro_beat"]}; '
                  f'fade target {timing["sound_end_seconds"]:.1f}s; '
                  f'advance {timing["outro_advance_bars"]} bars)', flush=True)
            run([dermixen, 'mix', 'add', str(document), str(entry['prepared']),
                 '--preset', incoming_preset, '--bars', str(args.bars),
                 '--intro', '0', '--outro', str(timing['outro_beat']),
                 '--bpm', str(timing['bpm']),
                 '--first-beat', str(entry['record']['grid']['first_beat_sample'] / SAMPLE_RATE),
                 '--gain', str(entry['track_gain_db'])])
            # Catch disagreements caused by stale library data immediately.
            layout = validate_layout(dermixen, document, entries, index + 1)
            if index:
                previous = layout['tracks'][index - 1]
                current = layout['tracks'][index]
                print(f'  Verified handover: {float(current["start_seconds"]):.1f}s; '
                      f'previous track ends at {float(previous["end_seconds"]):.1f}s.',
                      flush=True)
            playlist.append({
                'source': str(source), 'analyzed_file': str(entry['prepared']),
                'incoming_preset': incoming_preset, 'outgoing_preset': entry['outgoing_preset'],
                'track_gain_db': entry['track_gain_db'],
                'source_loudness': entry['record'].get('loudness'),
                **timing,
            })

        if args.master == 'none':
            run([dermixen, 'render', str(document), str(output)])
        else:
            with tempfile.TemporaryDirectory(prefix='.dermixen-master-', dir=output.parent) as temp:
                wav = Path(temp) / 'premaster.f32.wav'
                run([dermixen, 'render', str(document), str(wav)])
                master_uniform(wav, output, args.peak_db)

        cues = transition_cues(layout, entries, output)
        manifest.write_text(json.dumps({'preset': args.preset, 'bars': args.bars,
                                        'seed': args.seed, 'master': args.master,
                                        'track_gain_db': args.track_gain_db,
                                        'tracks': playlist, 'transitions': cues}, indent=2) + '\n',
                            encoding='utf-8')
        print(f'Finished: {output}\nEditable mix: {document}\nTracklist: {manifest}', flush=True)
        return 0
    except (OSError, subprocess.CalledProcessError, RuntimeError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
