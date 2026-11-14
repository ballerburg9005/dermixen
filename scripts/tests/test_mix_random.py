"""Black-box tests for random folder orchestration. No Rust engine build needed."""
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

SCRIPT = Path(__file__).parents[1] / 'mix-random.py'


class RandomMixScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.music = root / 'music'
        self.music.mkdir()
        self.binary = root / 'dermixen'
        self.binary.write_text(r'''#!/usr/bin/env python3
import json
from pathlib import Path
import struct
import sys
import wave
args = sys.argv[1:]
def facts(path):
    name = Path(path).name.lower()
    if 'short' in name:
        return 138, 250.0
    if 'long' in name:
        return 350, 220.0
    return 180, 200.0
if args[0] == 'analyze':
    duration, bpm = facts(args[1])
    print(json.dumps({'length_samples': duration * 44100,
        'grid': {'first_beat_sample': 4410, 'bpm': bpm},
        'anchors': {'intro_beat': 20, 'outro_beat': 68},
        'extent': {'begins_sample': 0, 'ends_sample': (duration - 10) * 44100}}))
elif args[:2] == ['mix', 'new']:
    Path(args[2]).write_text('mix\n')
elif args[:2] == ['mix', 'add']:
    with Path(args[2]).open('a') as out:
        out.write(json.dumps(args) + '\n')
elif args[:2] == ['mix', 'show']:
    commands = [json.loads(line) for line in
                Path(args[2]).read_text().splitlines()[1:]]
    tracks = []
    for command in commands:
        path = command[3]
        duration, fresh_bpm = facts(path)
        def opt(flag, default):
            return command[command.index(flag) + 1] if flag in command else default
        # Model a cached, outdated library containing half-time tempos.
        bpm = float(opt('--bpm', fresh_bpm / 2))
        phase = int(round(float(opt('--first-beat', '0')) * 44100))
        start = (0.0 if not tracks else
                 tracks[-1]['start_seconds'] +
                 tracks[-1]['anchors']['outro_beat'] * 60.0 /
                 tracks[-1]['grid']['bpm'])
        tracks.append({'grid': {'bpm': bpm, 'first_beat_sample': phase},
                       'anchors': {'outro_beat': int(opt('--outro', '68'))},
                       'length_samples': duration * 44100,
                       'start_seconds': start, 'end_seconds': start + duration})
    print(json.dumps({'tracks': tracks}))
elif args[:1] == ['render']:
    output = Path(args[2])
    if output.suffix == '.wav':
        with wave.open(str(output), 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(struct.pack('<h', 8192) * (44100 * 2))
    else:
        output.write_bytes(b'MOCK-MP3')
else:
    sys.exit(17)
''')
        self.binary.chmod(0o755)
        self.out = root / 'random.mp3'

    def invoke(self, *args, output=None):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.music),
                               '-o', str(output or self.out),
                               '--dermixen', str(self.binary), *args],
                              capture_output=True, text=True)

    def prepare(self):
        for name in ('long.mp3', 'short.mp3', 'third.FLAC'):
            (self.music / name).write_bytes(b'mock-fixture')

    def test_short_track_outro_is_near_end_not_beat_68(self):
        self.prepare()
        first = self.invoke('--seed', '42', '--master', 'none')
        self.assertEqual(first.returncode, 0, first.stderr)
        playlist = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertEqual(len(playlist['tracks']), 3)
        short = next(e for e in playlist['tracks'] if Path(e['source']).name == 'short.mp3')
        self.assertEqual(short['analyzed_outro_beat'], 68)
        self.assertGreater(short['outro_beat'], 500,
                           'short dancecore songs must play close to their actual ending')
        for entry in playlist['tracks']:
            self.assertEqual(entry['intro_beat'], 0)
            self.assertGreater(entry['outro_beat'], entry['intro_beat'])
        # No tempo difference may force an abrupt jump between two songs.
        self.assertTrue(all(e['outgoing_preset'] == 'beatmix'
                            for e in playlist['tracks'][:-1]))
        self.assertEqual(playlist['tracks'][-1]['outgoing_preset'], 'cut')
        # Anchor moves back by the full transition span, not to the last note.
        self.assertLess(short['outro_beat'], 600)
        self.assertTrue(self.out.with_suffix('.dmx').exists())
        self.assertEqual(self.out.read_bytes(), b'MOCK-MP3')

        commands = self.out.with_suffix('.dmx').read_text().splitlines()[1:]
        self.assertEqual(len(commands), 3)
        for command in commands:
            args = json.loads(command)
            self.assertIn('--gain', args)
            self.assertEqual(args[args.index('--gain') + 1], '-7.0')
            self.assertEqual(args[args.index('--intro') + 1], '0')
            # The original bug came from --bpm missing here, so mix add used
            # half-time values cached by the older Rust analyzer.
            self.assertIn('--bpm', args)
            self.assertIn('--first-beat', args)
            self.assertEqual(float(args[args.index('--first-beat') + 1]), 0.1)
            source = args[3]
            expected = 250.0 if 'short' in source else (220.0 if 'long' in source else 200.0)
            self.assertEqual(float(args[args.index('--bpm') + 1]), expected)

        refused = self.invoke('--seed', '42', '--master', 'none')
        self.assertNotEqual(refused.returncode, 0)
        repeated = self.invoke('--seed', '42', '--master', 'none', '--force')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        playlist2 = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertEqual([e['source'] for e in playlist['tracks']],
                         [e['source'] for e in playlist2['tracks']])

    def test_explicit_preset_is_preserved(self):
        self.prepare()
        result = self.invoke('--seed', '42', '--master', 'none', '--preset', 'beatmix', '--bars', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertTrue(all(e['outgoing_preset'] == 'beatmix'
                            for e in manifest['tracks'][:-1]))


    def test_unexpected_cached_half_time_gap_prevents_render(self):
        self.prepare()
        # Here the mix show mock intentionally ignores the fresh grid's
        # tempo override, simulating a broken/stale mix command.
        half_time_binary = Path(self.tmp.name) / 'halftime'
        altered = self.binary.read_text().replace(
            "bpm = float(opt('--bpm', fresh_bpm / 2))",
            "bpm = fresh_bpm / 2")
        half_time_binary.write_text(altered)
        half_time_binary.chmod(0o755)
        command = [sys.executable, str(SCRIPT), str(self.music), '-o', str(self.out),
                   '--dermixen', str(half_time_binary), '--master', 'none', '--seed', '42']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('beat grid is stale', result.stderr)
        self.assertFalse(self.out.exists(), 'must never render a silent mix')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_mastering_raises_peak_to_near_full_scale(self):
        self.prepare()
        output = self.out.with_suffix('.wav')
        result = self.invoke('--seed', '42', output=output)
        self.assertEqual(result.returncode, 0, result.stderr)
        with wave.open(str(output), 'rb') as wav:
            self.assertEqual(wav.getnchannels(), 2)
            samples = wav.readframes(1000)
        max_peak = max(abs(n) for n in struct.unpack('<' + 'h' * (len(samples) // 2), samples)) / 32768
        self.assertGreater(max_peak, 0.85)
        self.assertLess(max_peak, 0.95)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_ogg_conversion_is_cached(self):
        ogg = self.music / 'test.ogg'
        produced = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                   '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                                   '-c:a', 'libvorbis', str(ogg)], capture_output=True, text=True)
        if produced.returncode:
            self.skipTest(f'ffmpeg OGG encoder unavailable: {produced.stderr}')
        (self.music / 'second.mp3').write_bytes(b'fixture')
        result = self.invoke('--seed', '12', '--master', 'none')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        prepared = Path(next(e['analyzed_file'] for e in manifest['tracks']
                             if e['source'] == str(ogg)))
        self.assertEqual(prepared.read_bytes()[:4], b'RIFF')
        rerun = self.invoke('--seed', '12', '--master', 'none', '--force')
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertNotIn('+ ffmpeg ', rerun.stdout)


if __name__ == '__main__':
    unittest.main()
