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
if args[0] == 'analyze':
    path = Path(args[1])
    name = path.name.lower()
    if 'short' in name:
        duration, bpm = 138, 125.0
    elif 'long' in name:
        duration, bpm = 350, 110.0
    else:
        duration, bpm = 180, 100.0
    print(json.dumps({'length_samples': duration * 44100,
        'grid': {'first_beat_sample': 0, 'bpm': bpm},
        'anchors': {'intro_beat': 20, 'outro_beat': 68},
        'extent': {'begins_sample': 0, 'ends_sample': (duration - 10) * 44100}}))
elif args[:2] == ['mix', 'new']:
    Path(args[2]).write_text('mix\n')
elif args[:2] == ['mix', 'add']:
    with Path(args[2]).open('a') as out:
        out.write(json.dumps(args) + '\n')
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
        self.assertGreater(short['outro_beat'], 240,
                           'short dancecore songs must play close to their actual ending')
        for entry in playlist['tracks']:
            self.assertEqual(entry['intro_beat'], 0)
            self.assertGreater(entry['outro_beat'], entry['intro_beat'])
        self.assertIn('cut', [e['outgoing_preset'] for e in playlist['tracks']])
        self.assertTrue(self.out.with_suffix('.dmx').exists())
        self.assertEqual(self.out.read_bytes(), b'MOCK-MP3')

        commands = self.out.with_suffix('.dmx').read_text().splitlines()[1:]
        self.assertEqual(len(commands), 3)
        for command in commands:
            args = json.loads(command)
            self.assertIn('--gain', args)
            self.assertEqual(args[args.index('--gain') + 1], '-3.0')
            self.assertEqual(args[args.index('--intro') + 1], '0')

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
