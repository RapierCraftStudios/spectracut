# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Audio tool check image (issue #48, spike § Sound design and mix).
#
# Headless Linux image with the open-source audio stack SpectraCut drives from
# code: Ardour (+ its Lua session tool), SuperCollider (NRT), Pedalboard, and the
# plugin set (LSP, x42, Airwindows, Dragonfly, Surge XT, IEM, EBU ADM renderer).
# Every version is pinned. The check script is not copied in; mount it:
#
#   docker build -f host/docker/audio.Dockerfile -t spectracut-audio-check host/docker
#   docker run --rm -v "$PWD/scripts:/opt/check:ro" -v "$out:/out" \
#     spectracut-audio-check bash /opt/check/audio_tool_check.sh --out /out
#
# Results: docs/engineering/audio-tool-check.md.

FROM debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    QT_QPA_PLATFORM=offscreen \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Step 1: base tools (download, unpack, generate and inspect test audio).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip xz-utils jq \
      ffmpeg=7:7.1.5-0+deb13u1 sox=14.4.2+git20190427-5+b3 \
      lilv-utils=0.24.26-1 \
 && rm -rf /var/lib/apt/lists/*

# Step 2: Ardour 8 with its headless Lua session tool (ardour8-lua / luasession).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ardour=1:8.12.0+ds-1 \
 && rm -rf /var/lib/apt/lists/*

# Step 3: SuperCollider (sclang + scsynth) for non-real-time rendering.
# sclang needs a Qt platform; QT_QPA_PLATFORM=offscreen keeps it headless.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      supercollider-language=1:3.13.0+repack-3 supercollider-server=1:3.13.0+repack-3 \
 && rm -rf /var/lib/apt/lists/*

# Step 4: Debian-packaged plugins: LSP (LV2 + VST3), x42 (LV2), Dragonfly Reverb (LV2 + VST3).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      lsp-plugins-lv2=1.2.21-5 lsp-plugins-vst3=1.2.21-5 \
      x42-plugins=20240611+ds-1 \
      dragonfly-reverb-lv2=3.2.10-3 dragonfly-reverb-vst3=3.2.10-3 \
 && rm -rf /var/lib/apt/lists/*

# Step 5: Surge XT 1.3.4 plugins (not packaged by Debian), from the upstream release.
ARG SURGE_URL=https://github.com/surge-synthesizer/releases-xt/releases/download/1.3.4/surge-xt-linux-1.3.4-pluginsonly.tar.gz
ARG SURGE_SHA256=dd431b75f5fa197c4bffa6ca27ca46970f0a94c834119bb1db7decdeec4c28db
RUN curl -fsSL --ipv4 --retry 5 --retry-all-errors --connect-timeout 30 --max-time 1200 -o /tmp/surge.tgz "$SURGE_URL" \
 && echo "$SURGE_SHA256  /tmp/surge.tgz" | sha256sum -c - \
 && mkdir -p /usr/lib/lv2 /usr/lib/vst3 /tmp/surge \
 && tar xzf /tmp/surge.tgz -C /tmp/surge \
 && cp -r "/tmp/surge/Surge XT.lv2" "/tmp/surge/Surge XT Effects.lv2" /usr/lib/lv2/ \
 && cp -r "/tmp/surge/Surge XT.vst3" "/tmp/surge/Surge XT Effects.vst3" /usr/lib/vst3/ \
 && echo "1.3.4" > /usr/lib/vst3/surge-xt.version \
 && rm -rf /tmp/surge /tmp/surge.tgz

# Step 6: Airwindows Consolidated (not packaged by Debian). Upstream publishes only a
# rolling "DAWPlugin" release; this pins one dated build by sha256. If the asset has
# been replaced upstream the step records the failure instead of breaking the image,
# and the check reports Airwindows as FAIL with its fallback.
ARG AIRWINDOWS_URL=https://github.com/baconpaul/airwin2rack/releases/download/DAWPlugin/AirwindowsConsolidated-2026-10-31-d013e10-Linux.zip
ARG AIRWINDOWS_SHA256=98f9f37788a14cfff97dad73332e7f7621e29bd2685b4ef5f0671273a1253ef4
RUN mkdir -p /usr/lib/vst3 /opt/airwindows \
 && if curl -fsSL --ipv4 --retry 5 --retry-all-errors --connect-timeout 30 --max-time 1200 -o /tmp/aw.zip "$AIRWINDOWS_URL" \
      && echo "$AIRWINDOWS_SHA256  /tmp/aw.zip" | sha256sum -c - ; then \
      unzip -q /tmp/aw.zip -d /opt/airwindows \
      && find /opt/airwindows -maxdepth 4 -name '*.vst3' -type d -exec cp -r {} /usr/lib/vst3/ \; \
      && echo "2026-10-31-d013e10" > /usr/lib/vst3/airwindows.version ; \
    else echo "airwindows download or checksum failed" > /opt/airwindows/INSTALL_FAILED ; fi \
 && rm -f /tmp/aw.zip

# Step 7: IEM Plugin Suite. Upstream ships no Linux binaries, and Debian's only build
# (sid, 1.16.0) needs glibc >= 2.43, newer than trixie's. Nothing is installed; the
# check reports IEM as FAIL with its fallback (see the report).

# Step 8: Python venv with Pedalboard (VST3 host) and the EBU ADM Renderer (ear).
# ear 2.1.0 needs numpy 1.x, which has no wheel for trixie's Python 3.13, so the venv
# uses a uv-managed CPython 3.12 (the repo's Python, pyproject.toml) instead.
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_NO_CACHE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3=3.13.5-1 python3-venv=3.13.5-1 \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/uv \
 && /opt/uv/bin/pip install uv==0.12.5 \
 && /opt/uv/bin/uv venv --python 3.12 /opt/venv \
 && /opt/uv/bin/uv pip install --python /opt/venv/bin/python pedalboard==0.9.26 ear==2.1.0 \
 && /opt/uv/bin/uv pip freeze --python /opt/venv/bin/python > /opt/venv/pip-freeze.txt
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /work
CMD ["bash", "/opt/check/audio_tool_check.sh", "--out", "/out"]
