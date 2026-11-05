#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Audio tool check (issue #48, spike § Sound design and mix).
#
# Verifies, headless, the open-source audio stack SpectraCut drives from code:
#   a) Ardour renders a generated session (2 tracks -> bus, gain automation, one
#      LV2 plugin) to WAV from its Lua tool, twice, byte-identical;
#   b) SuperCollider NRT renders a parameterised riser identically on two runs;
#   c) Pedalboard processes a clip through a VST3 plugin from Python;
#   d) inventory of the plugin set (LSP, x42, Airwindows, Dragonfly, Surge XT,
#      IEM, EBU ADM renderer): version, licence, install path, smoke load.
#
# Usage: audio_tool_check.sh [--out DIR]
# Runs inside host/docker/audio.Dockerfile, or directly on a Linux render host
# with the same tools installed. Writes DIR/results.json (one object per tool:
# tool, version, licence, path, result, seconds, detail), the rendered audio, and
# a markdown table on stdout. Exits non-zero when check a, b or c fails.

set -euo pipefail

OUT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n '4,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
OUT="${OUT:-$(mktemp -d)}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
LOGS="$OUT/logs"
mkdir -p "$LOGS"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

RATE=48000
VST3_DIRS=(/usr/lib/vst3 /usr/local/lib/vst3 "$HOME/.vst3")
PY="${PYTHON:-python3}"
ROWS="$WORK/rows.tsv"
: > "$ROWS"

now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.1f", b - a }'; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
deb_version() { dpkg-query -W -f='${Version}' "$1" 2>/dev/null || echo "not installed"; }
clean() { tr '\t\n' '  ' <<<"$1"; }

# record TOOL VERSION LICENCE PATH RESULT SECONDS DETAIL
record() {
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$(clean "$2")" "$3" "$(clean "$4")" "$5" "$6" \
    "$(clean "$7")" >> "$ROWS"
  printf '[%s] %s: %s\n' "$5" "$1" "$7" >&2
}

find_vst3() { # find_vst3 GLOB -> first matching bundle dir
  local d
  for d in "${VST3_DIRS[@]}"; do
    compgen -G "$d/$1" | head -n1 && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# a) Ardour: generated session -> WAV, headless, twice, byte-identical
# ---------------------------------------------------------------------------
ardour_lua_bin() {
  local b
  for b in ardour9-lua ardour8-lua; do
    command -v "$b" && return 0
  done
  return 1
}

# The Lua programs run by ardourN-lua: FILE builds the session, saves and closes
# it; FILE_reload.lua reloads it, checks the routing and plugin survived, and
# exports the master bus with Session:simple_export() and the preset below.
# ardourN-export is not used: about 1 export in 10-15 of the same session came out
# different (whole mix shifted by 16 samples, or the automation ramp off by a few
# LSB), a race at its export start; the Lua export, run after the session settles,
# gave 40/40 identical files.
write_ardour_lua() { # write_ardour_lua FILE SESSION_DIR EXPORT_DIR
  cat > "$1" <<EOF
local dir, name = "$2", "check"
local rate = $RATE
local len = 4 * rate

AudioEngine:set_backend("None (Dummy)", "", "")
local s = create_session(dir, name, rate)
assert(s, "create_session failed")

-- two mono tracks with the same generated source
local tracks = s:new_audio_track(1, 2, nil, 2, "Src", ARDOUR.PresentationInfo.max_order,
  ARDOUR.TrackMode.Normal, true, false)
assert(tracks:size() == 2, "expected 2 tracks")

-- one stereo bus; both tracks feed it instead of the master
local buses = s:new_audio_route(2, 2, nil, 1, "FX Bus", ARDOUR.PresentationInfo.Flag.AudioBus,
  ARDOUR.PresentationInfo.max_order)
local bus = buses:front()

-- place a region of the generated source on each track, offset by 0.5 s
local i = 0
for t in tracks:iter() do
  local tr = t:to_track()
  -- route: disconnect from master, connect to the bus input
  tr:output():disconnect_all(nil)
  for c = 0, 1 do
    tr:output():audio(c):connect(bus:input():audio(c):name())
  end
  -- source audio: bounce a sine from Ardour's bundled SinGen Lua DSP into a new
  -- region. The headless Lua API has no file import (SourceFactory and
  -- Editor:do_import are GUI-only), so the source is generated inside the session.
  local gen = ARDOUR.LuaAPI.new_luaproc(s, "SinGen")
  assert(not gen:isnil(), "SinGen Lua DSP not found")
  assert(tr:add_processor_by_index(gen, 0, nil, true), "add SinGen failed")
  ARDOUR.LuaAPI.set_processor_param(gen, 0, 220 * (i + 1))
  ARDOUR.LuaAPI.set_processor_param(gen, 1, -12)
  local r = tr:bounce_range(0, len, ARDOUR.InterThreadInfo(), gen, true, "src" .. i, false)
  assert(not r:isnil(), "bounce of the source failed")
  tr:remove_processor(gen, nil, true)
  tr:playlist():add_region(r, Temporal.timepos_t(i * rate / 2), 1, false, 0, 0, false)
  -- gain automation: hold, dip, recover, fade out. The curve starts at the
  -- fader's static value (unity): starting elsewhere makes the first block's gain
  -- ramp depend on whether a stopped-transport cycle ran before the export
  -- started, which gave two different exports of the same session.
  local ac = tr:amp():gain_control()
  ac:set_value(1.0, PBD.GroupControlDisposition.NoGroup)
  local al = ac:alist()
  al:clear_list()
  al:add(Temporal.timepos_t(0), 1.0, false, true)
  al:add(Temporal.timepos_t(rate), 1.0, false, true)
  al:add(Temporal.timepos_t(2 * rate), 0.5, false, true)
  al:add(Temporal.timepos_t(len), 0.0, false, true)
  ac:set_automation_state(ARDOUR.AutoState.Play)
  i = i + 1
end

-- one LV2 plugin on the bus (ACE EQ, shipped with Ardour)
local eq = ARDOUR.LuaAPI.new_plugin(s, "urn:ardour:a-eq", ARDOUR.PluginType.LV2, "")
assert(not eq:isnil(), "a-eq LV2 plugin not found")
assert(bus:add_processor_by_index(eq, 0, nil, true), "add a-eq failed")
ARDOUR.LuaAPI.set_processor_param(eq, 1, 6.0)

-- session range = the export range (Location has no Lua constructor)
s:set_session_extents(Temporal.timepos_t(0), Temporal.timepos_t(len))
s:save_state("", false, false, false, false, false)
close_session()
print("ARDOUR_BUILD_OK")
EOF
  # Reload runs in a fresh ardourN-lua process: load_session() after close_session()
  # in the same process hangs on close in Ardour 8.12.
  cat > "${1%.lua}_reload.lua" <<EOF
AudioEngine:set_backend("None (Dummy)", "", "")
local dir, name = "$2", "check"
-- reload: the saved session must load headless with the same routing and plugin
local s = load_session(dir, name)
assert(s, "load_session failed")
local n = 0
for _ in s:get_routes():iter() do n = n + 1 end
assert(n == 4, "expected master + 2 tracks + bus after reload, got " .. n)
local rb = s:route_by_name("FX Bus")
assert(not rb:isnil(), "bus missing after reload")
assert(not rb:nth_plugin(0):isnil(), "bus plugin missing after reload")
print("ARDOUR_RELOAD_OK")
-- export: let the freshly loaded session settle, then render the session range
-- of the master bus with the check preset (WAV int24, session rate, no dither,
-- no normalise, no BWF/bext, no tags)
ARDOUR.LuaAPI.usleep(500000)
local e = s:simple_export()
e:set_folder("$3")
e:set_name("mix")
e:set_range(0, 4 * $RATE)
assert(e:set_preset("$ARDOUR_PRESET_ID"), "export preset not found")
assert(e:check_outputs(), "master bus has no outputs to export")
assert(e:run_export(), "export failed")
close_session()
print("ARDOUR_EXPORT_OK")
EOF
}

# Export format + preset for Session:simple_export(), read from a private
# XDG_CONFIG_HOME so nothing is written to the user's Ardour config on a host.
ARDOUR_FORMAT_ID=5c1e0d7a-48a0-4b0e-9c3e-2a7d0c0f4801
ARDOUR_PRESET_ID=5c1e0d7a-48a0-4b0e-9c3e-2a7d0c0f4802
write_ardour_export_preset() { # write_ardour_export_preset DIR
  mkdir -p "$1"
  cat > "$1/check-wav24.format" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<ExportFormatSpecification name="Check WAV 24" id="$ARDOUR_FORMAT_ID" analyse="0">
  <Encoding id="F_WAV" type="T_Sndfile" extension="wav" name="WAV" has-sample-format="true" channel-limit="256"/>
  <SampleRate rate="SR_Session"/>
  <SRCQuality quality="SRC_SincBest"/>
  <EncodingOptions>
    <Option name="sample-format" value="SF_24"/>
    <Option name="dithering" value="D_None"/>
    <Option name="tag-metadata" value="false"/>
    <Option name="tag-support" value="false"/>
    <Option name="broadcast-info" value="false"/>
  </EncodingOptions>
  <Processing>
    <Normalize enabled="false" loudness="no" dbfs="0" lufs="-23" dbtp="-1"/>
    <Silence>
      <Start><Trim enabled="false"/><Add enabled="false"><Duration format="Timecode" hours="0" minutes="0" seconds="0" frames="0"/></Add></Start>
      <End><Trim enabled="false"/><Add enabled="false"><Duration format="Timecode" hours="0" minutes="0" seconds="0" frames="0"/></Add></End>
    </Silence>
  </Processing>
</ExportFormatSpecification>
EOF
  cat > "$1/check-wav24.preset" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<ExportPreset id="$ARDOUR_PRESET_ID" name="Check WAV 24">
  <ExportFormat id="$ARDOUR_FORMAT_ID"/>
  <ExportFilename>
    <Folder relative="1" path="export"/>
    <Field name="label" enabled="1"/>
    <Field name="session" enabled="0"/>
    <Field name="snapshot" enabled="0"/>
    <Field name="timespan" enabled="0"/>
    <Field name="revision" enabled="0"/>
    <Field name="time" enabled="0" value="T_None"/>
    <Field name="date" enabled="0" value="D_None"/>
  </ExportFilename>
</ExportPreset>
EOF
}

check_ardour() {
  local t0 bin ver run lua out hashes=() files=() detail
  t0="$(now)"
  if ! bin="$(ardour_lua_bin)"; then
    record Ardour "not installed" GPL-2.0-or-later "-" FAIL "$(elapsed "$t0")" \
      "no ardour9-lua or ardour8-lua on PATH"
    return 1
  fi
  ver="$(deb_version ardour)"
  # ardourN reads export presets from $XDG_CONFIG_HOME/ardourN/export
  write_ardour_export_preset "$WORK/xdg/$(basename "${bin%-lua}")/export"
  for run in 1 2; do
    lua="$WORK/ardour_$run.lua"
    out="$OUT/ardour_run$run"
    rm -rf "$WORK/ardour_session_$run" "$out"
    mkdir -p "$out"
    write_ardour_lua "$lua" "$WORK/ardour_session_$run" "$out"
    if ! XDG_CONFIG_HOME="$WORK/xdg" timeout 600 "$bin" "$lua" > "$LOGS/ardour_run$run.log" 2>&1 \
       || ! grep -q ARDOUR_BUILD_OK "$LOGS/ardour_run$run.log" \
       || ! XDG_CONFIG_HOME="$WORK/xdg" timeout 600 "$bin" "${lua%.lua}_reload.lua" \
            >> "$LOGS/ardour_run$run.log" 2>&1 \
       || ! grep -q ARDOUR_RELOAD_OK "$LOGS/ardour_run$run.log" \
       || ! grep -q ARDOUR_EXPORT_OK "$LOGS/ardour_run$run.log"; then
      record Ardour "$ver" GPL-2.0-or-later "$bin" FAIL "$(elapsed "$t0")" \
        "run $run failed: $(grep -v '^$' "$LOGS/ardour_run$run.log" | grep -v 'encoder is not available' | tail -n 3)"
      return 1
    fi
    files+=("$(find "$out" -name '*.wav' | head -n1)")
    if [[ -z "${files[-1]}" ]]; then
      record Ardour "$ver" GPL-2.0-or-later "$bin" FAIL "$(elapsed "$t0")" "run $run wrote no WAV"
      return 1
    fi
    hashes+=("$(sha "${files[-1]}")")
  done
  detail="2 tracks -> bus, gain automation, LV2 urn:ardour:a-eq; sha256 ${hashes[0]:0:16}"
  if [[ "${hashes[0]}" == "${hashes[1]}" ]]; then
    record Ardour "$ver" GPL-2.0-or-later "$bin" PASS "$(elapsed "$t0")" "$detail; byte-identical"
    return 0
  fi
  local d1 d2
  d1="$(ffmpeg -nostdin -v error -i "${files[0]}" -f s32le - | sha256sum | cut -c1-16)"
  d2="$(ffmpeg -nostdin -v error -i "${files[1]}" -f s32le - | sha256sum | cut -c1-16)"
  record Ardour "$ver" GPL-2.0-or-later "$bin" FAIL "$(elapsed "$t0")" \
    "files differ (${hashes[0]:0:16} vs ${hashes[1]:0:16}); audio data $([[ "$d1" == "$d2" ]] && echo equal || echo differs)"
  return 1
}

# ---------------------------------------------------------------------------
# b) SuperCollider NRT: parameterised riser, two runs, identical
# ---------------------------------------------------------------------------
write_riser_scd() { # write_riser_scd FILE OUT_WAV
  cat > "$1" <<EOF
// Parameterised riser rendered in non-real-time mode.
(
var params = (dur: 3.0, f0: 110, f1: 1760, bright: 0.6, seed: 4242, amp: 0.25);
var def = SynthDef(\\riser, { |out = 0, dur = 3, f0 = 110, f1 = 1760, bright = 0.6, seed = 0, amp = 0.25|
  var env, freq, body, air, sig;
  RandSeed.ir(1, seed);
  env = EnvGen.kr(Env([0, 1, 0], [dur * 0.92, dur * 0.08], [3, -6]), doneAction: 2);
  freq = XLine.kr(f0, f1, dur);
  body = Saw.ar(freq * [1, 1.006]) + (SinOsc.ar(freq * 0.5) * 0.5);
  air = HPF.ar(WhiteNoise.ar(0.3 ! 2), freq * 2) * Line.kr(0, bright, dur);
  sig = RLPF.ar(body, (freq * 6).min(18000), 0.4) + air;
  Out.ar(out, (sig * env * amp).tanh);
});
var score = Score([
  [0.0, ['/d_recv', def.asBytes]],
  [0.0, Synth.basicNew(\\riser, Server.default, 1000).newMsg(nil,
    [\\dur, params.dur, \\f0, params.f0, \\f1, params.f1, \\bright, params.bright,
     \\seed, params.seed, \\amp, params.amp])],
  [params.dur + 0.1, [\\c_set, 0, 0]]
]);
var opts = ServerOptions.new.numOutputBusChannels_(2).numInputBusChannels_(0)
  .sampleRate_($RATE).verbosity_(-1);
score.recordNRT(nil, "$2", nil, $RATE, "WAV", "int24", opts, "", params.dur + 0.1, {
  "SC_NRT_DONE".postln;
  0.exit;
});
)
EOF
}

check_supercollider() {
  local t0 ver run scd wav hashes=() bin
  t0="$(now)"
  if ! bin="$(command -v sclang)" || ! command -v scsynth > /dev/null; then
    record SuperCollider "not installed" GPL-3.0-or-later "-" FAIL "$(elapsed "$t0")" \
      "sclang/scsynth not on PATH"
    return 1
  fi
  ver="$(deb_version supercollider-language)"
  for run in 1 2; do
    scd="$WORK/riser_$run.scd"
    wav="$OUT/riser_run$run.wav"
    rm -f "$wav"
    write_riser_scd "$scd" "$wav"
    # sclang links QtWebEngine (Chromium), which refuses to start as root with its
    # sandbox on; containers run as root, so the sandbox is disabled for sclang only.
    if ! QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}" QTWEBENGINE_DISABLE_SANDBOX=1 \
         QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox" timeout 300 "$bin" "$scd" \
         > "$LOGS/sc_run$run.log" 2>&1 || [[ ! -s "$wav" ]]; then
      record SuperCollider "$ver" GPL-3.0-or-later "$bin" FAIL "$(elapsed "$t0")" \
        "run $run failed: $(grep -v '^$' "$LOGS/sc_run$run.log" | tail -n 3)"
      return 1
    fi
    hashes+=("$(sha "$wav")")
  done
  if [[ "${hashes[0]}" == "${hashes[1]}" ]]; then
    record SuperCollider "$ver" GPL-3.0-or-later "$bin" PASS "$(elapsed "$t0")" \
      "riser dur=3 f0=110 f1=1760 seed=4242, NRT int24; sha256 ${hashes[0]:0:16}; identical"
    return 0
  fi
  record SuperCollider "$ver" GPL-3.0-or-later "$bin" FAIL "$(elapsed "$t0")" \
    "runs differ: ${hashes[0]:0:16} vs ${hashes[1]:0:16}"
  return 1
}

# ---------------------------------------------------------------------------
# c) Pedalboard: process a clip through a VST3 plugin from Python
# ---------------------------------------------------------------------------
check_pedalboard() {
  local t0 ver vst3 log rc
  t0="$(now)"
  ver="$("$PY" -c 'import pedalboard; print(pedalboard.__version__)' 2>/dev/null || echo "not installed")"
  if [[ "$ver" == "not installed" ]]; then
    record Pedalboard "$ver" GPL-3.0-only "-" FAIL "$(elapsed "$t0")" "python cannot import pedalboard"
    return 1
  fi
  # LSP (a deterministic compressor) first; Surge XT Effects, then Dragonfly as fallbacks
  if ! vst3="$(find_vst3 'lsp-plugins.vst3')" && ! vst3="$(find_vst3 'Surge XT Effects.vst3')" \
     && ! vst3="$(find_vst3 'DragonflyRoomReverb.vst3')"; then
    record Pedalboard "$ver" GPL-3.0-only "-" FAIL "$(elapsed "$t0")" "no VST3 plugin found"
    return 1
  fi
  log="$LOGS/pedalboard.log"
  set +e
  "$PY" - "$vst3" "$OUT/pedalboard_out.wav" > "$log" 2>&1 <<'EOF'
import hashlib
import sys

import numpy as np
import pedalboard
from pedalboard.io import AudioFile

path, out = sys.argv[1], sys.argv[2]
names = pedalboard.VST3Plugin.get_plugin_names_for_file(path)
preferred = [n for n in names if n == "Compressor Stereo"]
name = (preferred or names)[0]
rate = 48000
t = np.arange(int(rate * 2.0)) / rate
clip = (0.3 * np.sin(2 * np.pi * 440 * t) * (t < 0.5)).astype(np.float32)
clip = np.stack([clip, clip])


def run() -> np.ndarray:
    plugin = pedalboard.load_plugin(path, plugin_name=name)
    for key in plugin.parameters:
        if "mix" in key or "wet" in key:
            try:
                setattr(plugin, key, plugin.parameters[key].max_value)
            except Exception:
                pass
    return plugin.process(clip, rate, reset=True)


a, b = run(), run()
assert a.shape == clip.shape, a.shape
assert np.isfinite(a).all(), "non-finite output"
assert float(np.abs(a).max()) > 1e-4, "silent output"
assert not np.allclose(a, clip, atol=1e-6), "output equals input"
tail = float(np.abs(a[:, int(rate * 0.6):]).max())
with AudioFile(out, "w", rate, a.shape[0]) as f:
    f.write(a)
same = hashlib.sha256(a.tobytes()).hexdigest() == hashlib.sha256(b.tobytes()).hexdigest()
print(f"PEDALBOARD_OK plugin={name!r} peak={float(np.abs(a).max()):.3f} tail={tail:.4f} "
      f"repeat_identical={same}")
EOF
  rc=$?
  set -e
  if [[ $rc -eq 0 ]] && grep -q PEDALBOARD_OK "$log"; then
    record Pedalboard "$ver" GPL-3.0-only "$(dirname "$(dirname "$("$PY" -c 'import pedalboard; print(pedalboard.__file__)')")")" PASS \
      "$(elapsed "$t0")" "VST3 $(basename "$vst3"): $(grep PEDALBOARD_OK "$log" | cut -d' ' -f2-)"
    return 0
  fi
  record Pedalboard "$ver" GPL-3.0-only "$vst3" FAIL "$(elapsed "$t0")" "$(tail -n 3 "$log")"
  return 1
}

# ---------------------------------------------------------------------------
# d) plugin set inventory and smoke loads
# ---------------------------------------------------------------------------
LV2LS="$WORK/lv2ls.txt"
vst3_names() { # vst3_names BUNDLE -> plugin names via pedalboard (smoke load of the bundle)
  "$PY" -c 'import sys, pedalboard; print(len(pedalboard.VST3Plugin.get_plugin_names_for_file(sys.argv[1])))' \
    "$1" 2>> "$LOGS/vst3_scan.log"
}

inventory_lv2() { # inventory_lv2 TOOL VERSION LICENCE URI_REGEX [VST3_GLOB]
  local t0 n path vst3 vn detail
  t0="$(now)"
  n="$(grep -c -E "$4" "$LV2LS" || true)"
  path="$(grep -m1 -E "$4" "$LV2LS" || true)"
  detail="LV2 plugins: $n"
  if [[ -n "${5:-}" ]] && vst3="$(find_vst3 "$5")"; then
    vn="$(vst3_names "$vst3" || echo 0)"
    detail="$detail; VST3 $(basename "$vst3") loads $vn plugin(s)"
    path="$path $vst3"
  fi
  if [[ "$n" -gt 0 ]]; then
    record "$1" "$2" "$3" "$path" PASS "$(elapsed "$t0")" "$detail"
  else
    record "$1" "$2" "$3" "-" FAIL "$(elapsed "$t0")" "no LV2 URI matching $4"
  fi
}

inventory_vst3() { # inventory_vst3 TOOL VERSION LICENCE VST3_GLOB [FAILED_MARKER]
  local t0 vst3 vn
  t0="$(now)"
  if ! vst3="$(find_vst3 "$4")"; then
    record "$1" "$2" "$3" "-" FAIL "$(elapsed "$t0")" \
      "no VST3 bundle matching $4$([[ -n "${5:-}" && -e "${5:-}" ]] && echo "; $(cat "$5")")"
    return
  fi
  vn="$(vst3_names "$vst3" || echo 0)"
  if [[ "$vn" -gt 0 ]]; then
    record "$1" "$2" "$3" "$vst3" PASS "$(elapsed "$t0")" "VST3 loads $vn plugin(s)"
  else
    record "$1" "$2" "$3" "$vst3" FAIL "$(elapsed "$t0")" "VST3 bundle found but did not load"
  fi
}

check_inventory() {
  local t0 ver
  lv2ls > "$LV2LS" 2> "$LOGS/lv2ls.log" || : > "$LV2LS"
  inventory_lv2 "LSP Plugins" "$(deb_version lsp-plugins-lv2)" LGPL-3.0-or-later \
    '^http://lsp-plug.in/plugins/lv2/' 'lsp-plugins.vst3'
  inventory_lv2 x42 "$(deb_version x42-plugins)" GPL-2.0-or-later '^http://gareus.org/oss/lv2/'
  inventory_vst3 Airwindows "$(cat /usr/lib/vst3/airwindows.version 2>/dev/null || echo unknown)" \
    MIT 'Airwindows*.vst3' /opt/airwindows/INSTALL_FAILED
  inventory_lv2 "Dragonfly Reverb" "$(deb_version dragonfly-reverb-lv2)" GPL-3.0-or-later \
    '^urn:dragonfly:' 'DragonflyRoomReverb.vst3'
  inventory_lv2 "Surge XT" "$(cat /usr/lib/vst3/surge-xt.version 2>/dev/null || echo unknown)" \
    GPL-3.0-or-later '^https?://surge-synth' 'Surge XT.vst3'
  inventory_vst3 "IEM Plugin Suite" "$(deb_version iem-plugin-suite-vst3)" GPL-3.0-or-later \
    'IEM*.vst3'
  t0="$(now)"
  ver="$("$PY" -c 'import importlib.metadata as m; print(m.version("ear"))' 2>/dev/null || echo "not installed")"
  if [[ "$ver" != "not installed" ]] && "$PY" -c 'import ear.core' 2>> "$LOGS/ear.log" \
     && ear-render --help > /dev/null 2>> "$LOGS/ear.log"; then
    record "EBU ADM Renderer (ear)" "$ver" BSD-3-Clause-Clear "$(command -v ear-render)" PASS \
      "$(elapsed "$t0")" "import ear.core and ear-render CLI ok"
  else
    record "EBU ADM Renderer (ear)" "$ver" BSD-3-Clause-Clear "-" FAIL "$(elapsed "$t0")" \
      "$(tail -n 2 "$LOGS/ear.log" 2>/dev/null)"
  fi
}

# ---------------------------------------------------------------------------
STATUS=0
check_ardour || STATUS=1
check_supercollider || STATUS=1
check_pedalboard || STATUS=1
check_inventory

"$PY" - "$ROWS" "$OUT/results.json" <<'EOF'
import json
import sys

keys = ["tool", "version", "licence", "path", "result", "seconds", "detail"]
rows = []
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        row = dict(zip(keys, line.rstrip("\n").split("\t"), strict=True))
        row["seconds"] = float(row["seconds"])
        rows.append(row)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(rows, fh, indent=2)
    fh.write("\n")
print("| Tool | Version | Licence | Install path | Result | Time (s) | Detail |")
print("|---|---|---|---|---|---|---|")
for r in rows:
    print("| " + " | ".join(str(r[k]) for k in keys) + " |")
EOF
echo "results: $OUT/results.json" >&2
exit "$STATUS"
