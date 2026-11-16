# <img src="docs/images/dermixen-icon.svg" alt="Dermixen icon" width="44" align="top"> Dermixen

Dermixen is a desktop app for macOS and Linux for building a continuous DJ mix in the studio. You scan your music once, put analyzed tracks on a timeline, get a beatmatched transition between each pair, adjust the transitions by ear, and render the whole mix to one WAV or MP3 file. It is inspired by MixMeister and is not a live performance tool.

The app has two front doors, and both drive the same engine:

- `dermixen` is the command. It scans folders into [the library](docs/library.md), which is everything Dermixen has learned about your tracks and is kept in one file in your data folder. It queries the library, builds a mix document, renders the mix, and plays it. Every command prints JSON behind `--json`, so a script or an agent can drive the app without a window.
- `dermixen-app` is the window. It opens a mix document, or an untitled empty mix when you name no document, as a timeline with the waveforms, the mix tempo curve, the anchors that place each transition, and the volume and EQ curves of every track, with the library in a panel beside them.

What you hear in the window is what a render writes, because preview and render are one code path.

![The Dermixen window: the timeline of a 91-minute mix on the left and the library panel on the right, a table of 2,274 tracks with artist, title, year, tempo, Camelot key, label, catalog number, release, track number, and duration, and a search field above it.](docs/images/window-library.png)

## Prerequisites

- `rust-toolchain.toml` names the toolchain the workspace builds with, and `rustup` installs it on the first cargo command. The window needs at least Rust 1.95, because it is built on `eframe` 0.36.
- A C and C++ compiler, and `make`. The build compiles three vendored libraries (the Signalsmith stretcher, the aubio beat tracker, and libkeyfinder) from source, and builds the LAME MP3 encoder with its own configure script and `make`. On macOS, the Xcode command line tools provide all of them.
- On Linux, the ALSA development headers, which the build needs for audio output: the `libasound2-dev` package on Debian and Ubuntu.

## Build

```
cargo build --release --workspace
```

The two executables land in `target/release/`: `dermixen` and `dermixen-app`. `dermixen open` starts the window from the folder that contains the command, so keep the two together. The window is slow in a debug build, so build it with `--release`.

`scripts/check.sh` runs the formatting check, the lints, and every test in the workspace. Continuous integration runs those three on Linux and macOS, and `cargo deny check` against `deny.toml`, for every push to `main` and every pull request. That check covers licenses, security advisories, dependency bans, and dependency sources.

## The starter library

The window builds its first library from `~/Music/Undefunktis` when it opens and finds no library file. That folder is empty until `scripts/install-starter-tracks.sh` puts five tracks by Undefunktis there, about 70 megabytes in all. The script checks each download against a SHA-256 digest recorded in the script itself and deletes and reports a download that does not match. The tracks are by [Undefunktis](https://undefunktis.bandcamp.com/) and are licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 license](https://creativecommons.org/licenses/by-nc-sa/4.0/), which is their license and not the app's. The folder is the `music_folder` setting, so it can name any folder instead.

```
scripts/install-starter-tracks.sh
```

## Opening a mix from the desktop

On macOS, `scripts/bundle-macos.sh` makes `target/Dermixen.app`, and you can drag that to your Applications folder. On Linux, `scripts/install-linux-desktop.sh` installs the desktop entry, the MIME type, and the app icon for you. On macOS and on Linux, double-clicking a `.dmx` file opens the window on that mix. On macOS, dropping a `.dmx` file on the Dermixen icon does the same, and a file you open while the window is up replaces the mix in that window. The window asks what should become of unsaved changes before it takes the new mix, when the mix it holds has any.

## Randomized folder mix (Dancecore.party test)

This fork extends the beat analyzer to 60-360 BPM and centers its prior at
215 BPM, so it can detect typical 180-300 BPM dancecore instead of limiting
all detections to 200 BPM. The analyzer still can make half-time or
double-time errors, so listen to any suspicious beat grids.

Build the CLI after pulling the fork, then render three songs:

```sh
cargo build --release --locked -p dermixen-cli
python3 scripts/mix-random.py ~/Music/my-tracks -o ~/test-mix.mp3 --limit 3 --seed 42
```

The script randomly selects tracks from the folder, scans MP3/WAV/FLAC/M4A/MP4
natively, and converts OGG/Opus/WebM/AAC/WMA/AIFF with FFmpeg. The fresh
analyzed BPM and beat phase are passed into the `mix add` command, bypassing
stale half-time beat grids stored in Dermixen's persistent library. Before
rendering, the script verifies that the actual mix uses those BPMs and has
no unintended timeline gaps.

**Transitions** use eight-bar beatmatched fades by default, with
their start and completion shifted **four bars earlier** than the old end-of-song
placement. At 250 BPM, four bars are about 3.84 seconds. This fades the
outgoing song sooner; use `--advance-bars 0` if its last notes should remain
audible until the end. Trailing silence is still detected, and automatic
hard cuts are disabled. To move only the second handover two bars earlier
than the original timing, while keeping the first handover unadvanced:

```sh
python3 scripts/mix-random.py ~/Music/my-tracks -o ~/test-earlier.mp3 --limit 3 --seed 42 --advance-bars 0 --advance-transition 2:2
```

`--advance-bars 4` is now the default; `--advance-bars 1` moves each
handover only one bar earlier. Repeating
`--advance-transition N:BARS` overrides any individual handover. The option
never moves the last track's end, because that track has no successor.

After rendering, the program prints exact timeline seek cues, including
when the incoming track starts and the outgoing source finishes. It writes
`test-earlier.transitions.txt` and embeds the cues inside
`test-earlier.tracklist.json`. The editable `.dmx` file is retained.

**Original mastering and shared gain:** By default, every track is mixed
at **0 dB gain**. The mixer does not normalize peaks, alter relative track
loudness, or compress the recordings. Beatmatching and crossfade envelopes
are the only changes made during the transition.

The output defaults to `--master original`: Dermixen renders a floating-point
`*.f32.wav`, then FFmpeg encodes the complete mix to 320 kbps MP3 without
a limiter, normalization, or blanket gain reduction. This preserves original
levels, even if the floating-point audio exceeds ±1.0. An MP3 can decode to
peaks beyond 1.0 (0 dBFS) without digital hard clipping; the codec's lossy
reconstruction means peaks need not be numerically identical to the inputs.
Audio above 0 dBFS can still clip some players/DACs at full digital volume.

For a conservative delivery master, opt in to
`--master safe --peak-db -1`. That option measures the whole floating-point
mix, then applies one shared attenuation if the highest peak is above -1 dBFS.
It changes all tracks' levels equally; this was the reason previous exports
sometimes looked ~0.6 high in Audacity. For comparison, `--master none`
renders directly through Dermixen's encoder, which clamps over-range
samples and should **not** be used when preserving the originals matters.
`--track-gain-db -3` deliberately lowers every track by the same 3 dB.

A quieter master remains quieter than a louder master. Making all songs
equally loud while keeping their exact original loudness is impossible.
The default prioritizes preserving the recordings' mastering. Dermixen's flat three-band EQ now
passes samples through exactly when no EQ is requested; this avoids subtle
transient changes to already heavily distorted masters. Tempo changes and
MP3 transcoding may still alter audio, especially during handovers.

Use `--bars 4` or `--bars 12` to compare fade lengths. The current script
is not a creative Auto-DJ: phrase-aware planning, vocal separation,
and drop selection still require work. It runs separately from WordPress.

## Standalone Docker worker for Dancecore.party

The user builds the headless Docker image on their workstation and pushes
three architectures: AMD64, ARM64 (used by Syburg), and ARMv7/armhf.
The storage server pulls the ready-made image, not the Rust source:

```sh
cd ~/dermixen
git pull
docker buildx build --platform linux/arm64,linux/amd64,linux/arm/v7 --push -t ballerburg9005/dermixen:latest .
```

The image includes Dermixen's Rust DJ engine and FFmpeg. It runs as an
unprivileged standalone queue worker; WordPress does not render audio.
Container mounts are read-only `/media`, scratch `/work` and output
`/output`. Rendered MP3s are atomically published to Syburg and served
at `/dc-media/generated-mixes/<job-uuid>.mp3`.

A separate WordPress plugin is in
`wordpress/dancecore-dermixen-bridge/dancecore-dermixen-bridge.php`.
It does not modify the existing Dancecore Community plugin, voting
system or Kadence theme.

**User workflow:** On the existing **Mixes & Playlists** archive,
a logged-in member presses **Generate mix** beside a public playlist.
The bridge selects eligible local CC0 recordings, respecting source
order; ineligible external or unlicensed tracks are skipped.
The job is queued asynchronously, the storage worker mixes the sources,
and WordPress publishes a finished `mix` post attributed to the
**member who pressed Generate**, not the original playlist curator.
The result has a playable native MP3, download link, automatic-mixing
disclosure, source tracklist and playlist provenance. It appears in
Finished mixes and on the generating member's profile.

Worker API:
- `POST /wp-json/dcmix/v1/worker/claim` — authenticated job claim
- `POST /wp-json/dcmix/v1/worker/report` — result/failure receipt
- `GET/POST /wp-json/dcmix/v1/jobs` — members view/submit their jobs
- `POST /wp-json/dcmix/v1/worker/configure` — admin-only SHA256 pairing
- `POST /wp-json/dcmix/v1/worker/reconcile` — admin-only recovery

**Secrets:** A randomly generated bearer secret stays on Syburg in a
mode-0600 `worker.env`. WordPress stores only its SHA-256 verifier.
Do not paste or commit a shared token. The worker uses outbound HTTPS;
no inbound Docker port is required.

Current Syburg runtime:
`/root/dancecore-dermixen-runtime/compose.yml`. Operator commands:

```sh
cd /root/dancecore-dermixen-runtime
docker compose --profile manual ps
docker compose --profile manual logs -f dermixen
docker compose --profile manual pull dermixen
docker compose --profile manual up -d dermixen
```

The ARM64 container completed a real two-track render and the WordPress
bridge published the corresponding member-authored Mix post on
2026-11-16. The Docker amd64 build, protocol tests, and PHP lint also
passed. Follow-up work includes browser testing of the logged-in
Generate button, worker crash/stale-job recovery, and audio-quality
tuning for musical transitions.

## Make a mix

Scan a folder of tracks, put two of them in a mix, and render it. Substitute your own folder and two tracks of your own at a similar tempo.

```
cd ~/Music/goa
dermixen library scan comp
dermixen mix new set.dmx
dermixen mix add set.dmx "comp/VA - Orbital Gardens [ORBCD02]/03 Helix Nine - Lantern Fish.mp3"
dermixen mix add set.dmx "comp/VA - Signal Bloom [SGNL011]/07 Marrow Circuit - Tidewater.mp3"
dermixen render set.dmx set.mp3
dermixen open set.dmx
```

The scan analyzes each track once, at about three seconds per track, and writes the results to the library file in your data folder. `mix add` reads each track's tempo, beat grid, anchors, and loudness from the library, joins the track to the one before it with an eight-bar beatmatched transition, and prints the timeline. `render` writes the mix to a 320 kbps MP3 when the name ends in `.mp3`, and to a 16-bit WAV (44.1 kHz, stereo) with any other name. `open` shows the mix in the window. [Your first mix](docs/tutorials/your-first-mix.md) walks through the same steps with the output of each.

## Documentation

The documentation is in `docs/`, arranged by what you're trying to do, and published at [mmacy.github.io/dermixen](https://mmacy.github.io/dermixen/). [The documentation home page](docs/README.md) lists every page.

- Tutorials, to learn by doing: [Your first mix](docs/tutorials/your-first-mix.md) and [Editing a mix in the window](docs/tutorials/editing-a-mix-in-the-window.md).
- How-to guides, one task each: scanning your music, finding tracks, building a mix from a tracklisting, checking and changing a transition, fixing a beat grid, leveling the tracks, relinking moved files, driving the app from a script, and building from source. They're listed on [the documentation home page](docs/README.md#how-to-guides).
- Reference, the facts about each part: [the `dermixen` command](docs/cli.md), [the window](docs/window.md), [the library](docs/library.md), [the project file](docs/project-file.md), and [the scoreboard's ground truth](docs/ground-truth.md).
- Explanation, how and why the app works the way it does: [How a mix fits together](docs/explanation/how-a-mix-fits-together.md), [What you hear is what renders](docs/explanation/what-you-hear-is-what-renders.md), [How analysis is trusted](docs/explanation/how-analysis-is-trusted.md), and [The command and agents](docs/explanation/the-command-and-agents.md).

`DESIGN.md` is the specification the app is built to, including what it deliberately leaves out. `PLAN.md` is the working method, and `CLAUDE.md` is the guidance for the agents that build the app.

## Checking the work

Correctness is shown by things that run, not only by reading the code. `cargo test --workspace` runs the acceptance tests, including golden renders that compare the engine's output against stored audio. `dermixen play --capture` writes the frames the audio device would play, and a render of the same span writes identical samples, which is how the rule that preview equals render is checked from the command line. `dermixen scoreboard` measures every analyzer against annotated tracks and prints a table, so analyzer quality is a number rather than an opinion. [How analysis is trusted](docs/explanation/how-analysis-is-trusted.md) describes the scoreboard and the correction loop behind it.

## License

Dermixen is licensed under the GNU General Public License, version 3 or any later version. `LICENSE.md` holds the license text.

Copyright (C) 2026 Marsh Macy

The starter tracks are not part of the app and have their own license, named under "The starter library" above.
