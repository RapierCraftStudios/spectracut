#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copy these scripts to a render host: ./push.sh legion|mrdubey-1
set -e
h=$1; d=$(dirname "$0")
case $h in legion) r='C:/spectracut/models';; *) r='C:/Users/artac-remote.MRDUBEY/spectracut-models';; esac
ssh $h "powershell -NoProfile -Command \"New-Item -ItemType Directory -Force '$r/model_check' | Out-Null\"" 2>&1 | grep -viE 'post-quantum|store now|openssh.com' || true
scp -q $d/*.py $d/*.ps1 $h:"$r/model_check/" 2>&1 | grep -viE 'post-quantum|store now|openssh.com' || true
