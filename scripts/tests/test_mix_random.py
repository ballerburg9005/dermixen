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
        'loudness': {'true_peak_db': (-4.0 if 'short' in str(args[1])
                   else (0.25 if 'long' in str(args[1]) else -2.0)),
                     'integrated_lufs': (-11.0 if 'short' in str(args[1])
                     else (-7.0 if 'long' in str(args[1]) else -8.5))},
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
    if output.name.endswith('.f32.wav'):
        frames = struct.pack('<ff', 1.2, 0.45) * 44100
        wavformat = struct.pack('<HHIIHH', 3, 2, 44100, 44100 * 8, 8, 32)
        output.write_bytes(b'RIFF' + struct.pack('<I', 36 + len(frames)) +
                           b'WAVEfmt ' + struct.pack('<I', 16) + wavformat +
                           b'data' + struct.pack('<I', len(frames)) + frames)
    elif output.suffix == '.wav':
        with wave.open(str(output), 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(struct.pack('<h', 32000) * (44100 * 2))
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
        cue_file = self.out.with_suffix('.transitions.txt')
        self.assertTrue(cue_file.exists())
        self.assertIn('SEEK', cue_file.read_text())
        self.assertIn('INCOMING', cue_file.read_text())

        commands = self.out.with_suffix('.dmx').read_text().splitlines()[1:]
        self.assertEqual(len(commands), 3)
        for command in commands:
            args = json.loads(command)
            self.assertIn('--gain', args)
            # All tracks retain unity gain regardless of measured loudness.
            self.assertEqual(float(args[args.index('--gain') + 1]), 0.0)
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

    def test_second_handover_can_be_two_bars_earlier(self):
        self.prepare()
        reference = self.invoke('--seed', '42', '--master', 'none',
                                '--advance-bars', '0')
        self.assertEqual(reference.returncode, 0, reference.stderr)
        baseline = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        comparison = self.out.with_name('advanced.mp3')
        changed = self.invoke('--seed', '42', '--master', 'none',
                              '--advance-bars', '0', '--advance-transition', '2:2',
                              output=comparison)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        adjusted = json.loads(comparison.with_suffix('.tracklist.json').read_text())
        self.assertEqual(baseline['tracks'][0]['outro_beat'],
                         adjusted['tracks'][0]['outro_beat'])
        self.assertEqual(baseline['tracks'][1]['outro_beat'] - 8,
                         adjusted['tracks'][1]['outro_beat'])
        self.assertGreater(
            baseline['transitions'][1]['incoming_start_seconds'],
            adjusted['transitions'][1]['incoming_start_seconds'])

    def test_default_handover_advances_four_bars(self):
        self.prepare()
        default = self.invoke('--seed', '42', '--master', 'none')
        self.assertEqual(default.returncode, 0, default.stderr)
        default_manifest = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        baseline_output = self.out.with_name('no-advance.mp3')
        neutral = self.invoke('--seed', '42', '--master', 'none',
                              '--advance-bars', '0', output=baseline_output)
        self.assertEqual(neutral.returncode, 0, neutral.stderr)
        neutral_manifest = json.loads(baseline_output.with_suffix('.tracklist.json').read_text())
        for current, original in zip(default_manifest['tracks'][:-1],
                                     neutral_manifest['tracks'][:-1]):
            self.assertEqual(current['outro_beat'] + 16, original['outro_beat'])

    def test_uniform_track_gain_override(self):
        self.prepare()
        result = self.invoke('--seed', '42', '--master', 'none',
                             '--track-gain-db', '-3')
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertTrue(all(x['track_gain_db'] == -3 for x in manifest['tracks']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_default_mp3_preserves_original_over_range_peaks(self):
        self.prepare()
        result = self.invoke('--seed', '42')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('original levels preserved', result.stdout)
        # Real FFmpeg MP3 encoding/decoding, not the mock renderer.
        raw = subprocess.check_output([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-i', str(self.out), '-f', 'f32le', '-acodec', 'pcm_f32le', '-'])
        frame = (len(raw) // 16) * 8
        left, right = struct.unpack_from('<ff', raw, frame)
        self.assertGreater(left, 1.1)
        self.assertAlmostEqual(left, 1.2, delta=0.05)
        self.assertAlmostEqual(right, 0.45, delta=0.04)
        manifest = json.loads(self.out.with_suffix('.tracklist.json').read_text())
        self.assertEqual(manifest['master'], 'original')
        self.assertTrue(all(x['track_gain_db'] == 0 for x in manifest['tracks']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_default_float_wav_preserves_original_over_range_peaks(self):
        self.prepare()
        out = self.out.with_suffix('.wav')
        result = self.invoke('--seed', '42', output=out)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = subprocess.check_output([
            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-i', str(out), '-f', 'f32le', '-acodec', 'pcm_f32le', '-'])
        left, right = struct.unpack_from('<ff', raw, (len(raw)//16)*8)
        self.assertAlmostEqual(left, 1.2, delta=0.0001)
        self.assertAlmostEqual(right, 0.45, delta=0.0001)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_safe_master_attenuates_over_range_mix_equally(self):
        self.prepare()
        output = self.out.with_suffix('.wav')
        result = self.invoke('--seed', '42', '--master', 'safe', output=output)
        self.assertEqual(result.returncode, 0, result.stderr)
        with wave.open(str(output), 'rb') as wav:
            self.assertEqual(wav.getnchannels(), 2)
            samples = wav.readframes(1000)
        decoded = struct.unpack('<' + 'h' * (len(samples) // 2), samples)
        loud_peak = abs(decoded[0]) / 32768
        quiet_peak = abs(decoded[1]) / 32768
        self.assertGreater(loud_peak, 0.85)
        self.assertLess(loud_peak, 0.95)
        self.assertAlmostEqual(quiet_peak / loud_peak, 0.45 / 1.2, delta=0.001)
        manifest = json.loads(output.with_suffix('.tracklist.json').read_text())
        self.assertTrue(all(x['track_gain_db'] == 0.0 for x in manifest['tracks']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_master_never_boosts_a_mix_below_ceiling(self):
        self.prepare()
        # A quieter floating-point mix remains at its original volume.
        low_binary = Path(self.tmp.name) / 'low'
        low_binary.write_text(self.binary.read_text().replace(
            "struct.pack('<ff', 1.2, 0.45)",
            "struct.pack('<ff', 0.5, 0.25)"))
        low_binary.chmod(0o755)
        output = self.out.with_suffix('.wav')
        command = [sys.executable, str(SCRIPT), str(self.music),
                   '-o', str(output), '--dermixen', str(low_binary), '--seed', '42',
                   '--master', 'safe']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('global attenuation +0.00 dB', result.stdout)
        with wave.open(str(output), 'rb') as wav:
            frame = wav.readframes(1)
        left, right = struct.unpack('<hh', frame)
        self.assertAlmostEqual(left / 32768, 0.5, delta=0.0001)
        self.assertAlmostEqual(right / 32768, 0.25, delta=0.0001)

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
