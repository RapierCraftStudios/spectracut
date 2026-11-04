# Audio tool check (issue #48)

The sound pipeline in [Technical spike § Sound design and mix](technical-spike.md#sound-design-and-mix)
depends on open-source tools that run headless and render deterministically. This
report checks that before anything builds on them: Ardour session load and export
from its Lua tool, SuperCollider non-real-time (NRT) rendering, Pedalboard hosting
VST3, and the plugin set (LSP, x42, Airwindows, Dragonfly, Surge XT, IEM, EBU ADM
tools).

- Image: `host/docker/audio.Dockerfile` (pinned Debian trixie base, pinned packages and release artifacts).
- Check: `scripts/audio_tool_check.sh` (writes `results.json`, prints a markdown table, exits non-zero if the Ardour, SuperCollider or Pedalboard check fails).
- CI: `.github/workflows/audio-tool-check.yml` ("Audio tool check": manual dispatch, and PRs that touch the three files above).

## How to build and run

In Docker (what CI runs):

```bash
docker build -f host/docker/audio.Dockerfile -t spectracut-audio-check host/docker
out="$(mktemp -d)"
docker run --rm -v "$PWD/scripts:/opt/check:ro" -v "$out:/out" \
  spectracut-audio-check bash /opt/check/audio_tool_check.sh --out /out
cat "$out/results.json"
```

The build context is `host/docker` and the image copies nothing from the repo; the
script is mounted at run time, so the same script runs unchanged on a host.

Directly on a Linux render host (with the tools from the install steps below
installed; `PYTHON` selects the interpreter that has pedalboard and ear):

```bash
PYTHON=/opt/venv/bin/python bash scripts/audio_tool_check.sh --out /tmp/audio-check
```

Outputs in the out dir: `results.json` (one object per tool: `tool`, `version`,
`licence`, `path`, `result`, `seconds`, `detail`), both Ardour exports, both
SuperCollider renders, the Pedalboard output and `logs/`.

## Install steps

Each step is one commented `RUN` in `host/docker/audio.Dockerfile`.

| Step | Tools | How |
|---|---|---|
| Base | — | `FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4`; `QT_QPA_PLATFORM=offscreen` |
| 1 | ffmpeg, sox, lv2ls | apt: `ffmpeg=7:7.1.5-0+deb13u1 sox=14.4.2+git20190427-5+b3 lilv-utils=0.24.26-1` |
| 2 | Ardour | apt: `ardour=1:8.12.0+ds-1` (ships `ardour8-lua`, `ardour8-export`) |
| 3 | SuperCollider | apt: `supercollider-language=1:3.13.0+repack-3 supercollider-server=1:3.13.0+repack-3` |
| 4 | LSP, x42, Dragonfly | apt: `lsp-plugins-lv2/-vst3=1.2.21-5 x42-plugins=20240611+ds-1 dragonfly-reverb-lv2/-vst3=3.2.10-3` |
| 5 | Surge XT | upstream `surge-xt-linux-1.3.4-pluginsonly.tar.gz`, sha256 `dd431b75…c28db`, LV2 + VST3 into `/usr/lib/lv2`, `/usr/lib/vst3` |
| 6 | Airwindows | upstream Airwindows Consolidated `AirwindowsConsolidated-2026-10-31-d013e10-Linux.zip`, sha256 `98f9f377…53ef4`, VST3 into `/usr/lib/vst3` |
| 7 | IEM Plugin Suite | not installed (see "What did not work") |
| 8 | Pedalboard, ear | apt `python3=3.13.5-1 python3-venv`; `uv==0.12.5` makes a CPython 3.12 venv at `/opt/venv` with `pedalboard==0.9.26 ear==2.1.0 setuptools==80.9.0` (numpy resolved to 1.26.4) |

## Results

Run on 2026-11-04 (08:48 UTC) in the lane, image
`sha256:381448c09c774d16407435ea91e5ee343d6b8545989245dd3b641fe0a8e61dee`
(Linux x86_64, Docker). The check exited 0.

| Tool | Version | Licence | Install path | Result | Time | Fallback |
|---|---|---|---|---|---|---|
| Ardour | 1:8.12.0+ds-1 | GPL-2.0-or-later | /usr/bin/ardour8-lua, /usr/bin/ardour8-export | PASS | 46.7 s | — |
| SuperCollider | 1:3.13.0+repack-3 | GPL-3.0-or-later | /usr/bin/sclang, /usr/bin/scsynth | PASS | 1.5 s | — |
| Pedalboard | 0.9.26 | GPL-3.0-only | /opt/venv/lib/python3.12/site-packages | PASS | 35.4 s | — |
| LSP Plugins | 1.2.21-5 | LGPL-3.0-or-later | /usr/lib/lv2 (184 URIs), /usr/lib/vst3/lsp-plugins.vst3 | PASS | 14.8 s | — |
| x42 | 20240611+ds-1 | GPL-2.0-or-later | /usr/lib/lv2 (113 URIs) | PASS | 0.0 s | — |
| Airwindows | 2026-10-31-d013e10 | MIT | /usr/lib/vst3/Airwindows Consolidated.vst3 | PASS | 0.2 s | — |
| Dragonfly Reverb | 3.2.10-3 | GPL-3.0-or-later | /usr/lib/lv2 (3 URIs), /usr/lib/vst3/DragonflyRoomReverb.vst3 | PASS | 0.2 s | — |
| Surge XT | 1.3.4 | GPL-3.0-or-later | /usr/lib/lv2 (2 URIs), /usr/lib/vst3/Surge XT.vst3 | PASS | 0.3 s | — |
| IEM Plugin Suite | not installed (target 1.16.0) | GPL-3.0-or-later | not installed | FAIL | 0.0 s | Build IEM 1.16.0 from source (CMake + JUCE, VST3/LV2 targets) in a separate pinned builder stage, or take the binaries from a Debian release whose glibc matches the image; until then, do ambisonic encode/decode with the IEM-equivalent ambisonic tools in SuperCollider (ATK) or render immersive beds through ear |
| EBU ADM Renderer (ear) | 2.1.0 | BSD-3-Clause-Clear | /opt/venv/bin/ear-render | PASS | 3.6 s | — |

What each check does:

- **Ardour (R5):** `ardour8-lua` builds a session with 2 mono tracks routed to 1
  stereo bus (not to master), gain automation on each track (Play mode), and the
  LV2 plugin ACE EQ (`urn:ardour:a-eq`, +6 dB band) on the bus; it saves and
  closes. A second `ardour8-lua` process reloads the session and checks the
  routes and the bus plugin. `ardour8-export -b 24 -s 48000` renders the master
  bus over the session range to WAV. This runs twice in fresh dirs; both full
  files had sha256 `06a8d89610d05b8c…`.
- **SuperCollider (R6):** `sclang` builds a `Score` for a riser parameterised by
  duration, start/end frequency, brightness and seed (`RandSeed`), and renders it
  with `Score.recordNRT` (scsynth NRT, int24 WAV). Two runs, both sha256
  `933e6db7892134a3…`.
- **Pedalboard (R7):** Python loads LSP "Compressor Stereo" from
  `lsp-plugins.vst3` with `pedalboard.load_plugin`, processes a generated 2 s
  clip, and asserts the output is finite, non-silent and differs from the input.
- **Inventory:** LV2 bundles are smoke-loaded through `lv2ls` (URI present);
  VST3 bundles through `pedalboard.load_plugin`; ear through `import ear.core`
  and `ear-render --help`.

## Determinism notes

- Ardour exports are byte-identical only with: no dither (`ardour8-export` uses
  none), no normalise, no BWF/bext chunk (`-B` not passed), and a fixed export
  rate. The output is a plain 44-byte-header RIFF/WAVE.
- The gain automation must start at the fader's static value. With the curve
  starting at 0.0 while the fader sat at unity, the same session exported to one
  of two different files (seen 2 times in about 20 exports): the first block's
  gain ramp depended on whether a stopped-transport cycle ran before the
  freewheel export. Starting the curve at unity removed it (12/12 identical).
- SuperCollider NRT is deterministic with a fixed `RandSeed` and no audio
  device; `sclang` still needs `QT_QPA_PLATFORM=offscreen`.
- Pedalboard: two runs of the same plugin on the same clip were not
  bit-identical (`repeat_identical=False` for LSP Compressor and for Dragonfly
  Room Reverb). The check records this but does not gate on it; anything that
  needs bit-exact output should render through Ardour or SuperCollider, or pin
  and compare with a tolerance.

## What did not work

- **IEM Plugin Suite:** upstream ships no Linux binaries, and Debian's only build
  (sid, 1.16.0) needs glibc ≥ 2.43, newer than trixie's. Reported FAIL; see the
  fallback in the table.
- **Ardour Lua file import:** headless `ardour8-lua` exposes neither
  `ARDOUR.SourceFactory` nor `Editor:do_import` (GUI only). The source audio is
  generated in the session instead: Ardour's bundled `SinGen` Lua DSP is bounced
  (`Track:bounce_range`) into a region on each track, then removed.
  Fallback for importing real media (not yet tested): place the files in
  `interchange/<name>/audiofiles` and write the matching `Source`/`Region` entries
  into the session XML before loading it.
- **Ardour Lua export:** `Session:simple_export():run_export()` hangs headless
  with the "None (Dummy)" backend. Fallback used: `ardour8-export`.
- **Ardour reload in the same process:** `load_session()` after `close_session()`
  in one `ardour8-lua` process hangs on the second close. Fallback used: reload in
  a fresh process.
- **`ARDOUR.Location(...)`** has no Lua constructor; the session range is set with
  `Session:set_session_extents`.
- **sclang as root:** sclang links QtWebEngine, which aborts as root with its
  sandbox on (containers run as root). Fallback used:
  `QTWEBENGINE_DISABLE_SANDBOX=1 QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox` for
  sclang only, or run as a non-root user.
- **ear 2.1.0:** needs numpy 1.x (no wheel for trixie's Python 3.13), so the venv
  uses CPython 3.12; and it imports `pkg_resources`, removed in setuptools 81, so
  setuptools is pinned to 80.9.0.
- **SuperCollider version:** trixie carries 1:3.13.0, not 3.14.1; 3.13.0 is what is
  pinned and tested.
- **Airwindows:** upstream publishes only a rolling `DAWPlugin` release; the image
  pins one dated build by sha256. If upstream replaces the asset, the build still
  succeeds, Airwindows reports FAIL, and the fallback is to mirror the pinned zip
  in our own release storage.

## Render hosts

Pending (R3). No Linux render host is registered yet (#5, #15). Once one is, run
`scripts/audio_tool_check.sh` on it directly (see "How to build and run") and
append its results table here with the host, date and OS.
