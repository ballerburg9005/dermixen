"""Black-box tests for scripts/mix-random.py; no Rust build required."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / 'mix-random.py'


class RandomMixScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.music = root / 'music'
        self.music.mkdir()
        self.binary = root / 'dermixen'
        self.binary.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['mix', 'new']:
    Path(args[2]).write_text('mix\\n')
elif args[:2] == ['mix', 'add']:
    assert Path(args[3]).is_file()
    with Path(args[2]).open('a') as out:
        out.write(args[3] + '\\n')
elif args[:1] == ['render']:
    Path(args[2]).write_bytes(b'MOCK-MIX')
else:
    sys.exit(17)
''')
        self.binary.chmod(0o755)
        self.out = self.music / 'random.mp3'

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.music),
                               '-o', str(self.out), '--dermixen', str(self.binary),
                               *args], capture_output=True, text=True)

    def test_recursive_random_and_repeat_no_self_inclusion(self):
        subdir = self.music / 'album'
        subdir.mkdir()
        for name in ('a.mp3', 'b.FLAC'):
            (self.music / name).write_bytes(b'fixture')
        (subdir / 'c.MP3').write_bytes(b'fixture')
        first = self.invoke('--seed', '123')
        self.assertEqual(first.returncode, 0, first.stderr)
        playlist = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        tracks = [x['source'] for x in playlist['tracks']]
        self.assertEqual(len(tracks), 3)
        self.assertTrue(self.out.with_suffix('.dmx').exists())
        self.assertEqual(self.out.read_bytes(), b'MOCK-MIX')
        refused = self.invoke('--seed', '123')
        self.assertNotEqual(refused.returncode, 0)
        second = self.invoke('--seed', '123', '--force')
        self.assertEqual(second.returncode, 0, second.stderr)
        playlist2 = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertEqual(tracks, [x['source'] for x in playlist2['tracks']])
        self.assertNotIn(str(self.out), tracks)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_ogg_conversion_is_persistent_and_not_rediscovered(self):
        ogg = self.music / 'test.ogg'
        source = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                 '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                                 '-c:a', 'libvorbis', str(ogg)], capture_output=True, text=True)
        if source.returncode:
            self.skipTest(f'ffmpeg OGG encoder unavailable: {source.stderr}')
        (self.music / 'second.mp3').write_bytes(b'mock native track')
        result = self.invoke('--seed', '12')
        self.assertEqual(result.returncode, 0, result.stderr)
        playlist = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        converted = next(x for x in playlist['tracks'] if x['source'] == str(ogg))
        wav = Path(converted['analyzed_file'])
        self.assertTrue(wav.is_file())
        self.assertEqual(wav.read_bytes()[:4], b'RIFF')
        self.assertIn(str(wav), self.out.with_suffix('.dmx').read_text())
        again = self.invoke('--seed', '12', '--force')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn('ffmpeg', again.stdout)


if __name__ == '__main__':
    unittest.main()
