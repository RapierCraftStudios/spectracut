# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every download and checksum URL in the manifest resolves, and Flamenco picks its checksum line.

Flamenco publishes one combined ``flamenco-{version}.sha256`` (a line per artifact) and no
per-file ``.sha256`` sidecar. The offline tests run the real Linux snippets against a fake
``curl``; the network test (``SPECTRACUT_NETWORK=1``, the CI ``network`` job) requests every URL.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import subprocess
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spectracut.setup.manifest import Os, load_manifest

FLAMENCO = ("flamenco-manager", "flamenco-worker")
LINUX_ARTIFACT = "flamenco-{v}-linux-amd64.tar.gz"
WINDOWS_ARTIFACT = "flamenco-{v}-windows-amd64.zip"


def _active() -> dict[str, str]:
    return load_manifest().sets[0].versions


def _render(name: str, os_name: Os) -> str:
    cmd = load_manifest().components[name].install[os_name]
    return cmd.format(version=_active()[name])


def active_urls() -> list[tuple[str, str, str]]:
    """(component, os, url) for every https URL in the active set's rendered install commands."""
    m = load_manifest()
    found: list[tuple[str, str, str]] = []
    for name, comp in m.components.items():
        if name not in _active():
            continue
        for os_name in comp.install:
            if "{version}" not in comp.install[os_name] and "https://" not in comp.install[os_name]:
                continue
            try:
                cmd = _render(name, os_name)
            except (KeyError, IndexError):  # templates with other placeholders: no URLs
                continue
            base = re.search(r"\bu\s*=\s*'?(https://[^\s'\";]+)", cmd)
            if base:
                cmd = cmd.replace("$u", base.group(1))
            for url in re.findall(r"https://[^\s'\"`;)]+", cmd):
                if base and url == base.group(1) and f"{url}/" in cmd:
                    continue  # a directory prefix (uv's release dir), not a download
                found.append((name, os_name, url))
    return found


def test_url_extraction_covers_every_download_and_checksum() -> None:
    urls = {u for _, _, u in active_urls()}
    v = _active()
    assert f"https://flamenco.blender.org/downloads/flamenco-{v['flamenco-worker']}.sha256" in urls
    assert any(u.endswith("windows-x64.zip") for u in urls)
    assert any("uv-x86_64-unknown-linux-gnu.tar.gz.sha256" in u for u in urls)
    assert not any("$" in u or "{" in u for u in urls)


def test_flamenco_verifies_against_the_combined_checksum_file() -> None:
    v = _active()["flamenco-worker"]
    combined = f"https://flamenco.blender.org/downloads/flamenco-{v}.sha256"
    for name in FLAMENCO:
        for os_name in load_manifest().components[name].install:
            urls = [u for n, o, u in active_urls() if (n, o) == (name, os_name)]
            assert combined in urls, (name, os_name)
            assert not [u for u in urls if re.search(r"\.(tar\.gz|zip)\.sha256$", u)], (
                name,
                os_name,
            )
    win = _render("flamenco-worker", "windows")
    assert f"-SimpleMatch '{WINDOWS_ARTIFACT.format(v=v)}'" in win
    for name in FLAMENCO:
        assert f"grep '{LINUX_ARTIFACT.format(v=v)}$'" in _render(name, "linux")


def _fake_env(tmp_path: Path, linux_hash: str | None) -> tuple[dict[str, str], Path]:
    """A HOME and a PATH whose `curl` serves a combined checksum file and a real tarball."""
    v = _active()["flamenco-worker"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("flamenco-top/flamenco-worker")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tarball = tmp_path / "artifact.tar.gz"
    tarball.write_bytes(buf.getvalue())
    good = hashlib.sha256(buf.getvalue()).hexdigest()
    lines = [
        f"{'1' * 64}  {WINDOWS_ARTIFACT.format(v=v)}",  # before the linux line: must be skipped
        f"{linux_hash or good}  {LINUX_ARTIFACT.format(v=v)}",
        f"{'2' * 64}  flamenco-{v}-macos-arm64.tar.gz",
    ]
    (tmp_path / "combined.sha256").write_text("\n".join(lines) + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; -*) ;; *) url="$1";; esac;'
        " shift; done\n"
        f'case "$url" in\n  *.sha256) cat "{tmp_path}/combined.sha256";;\n'
        f'  *.tar.gz) cp "{tarball}" "$out";;\n'
        '  *) echo "unexpected url $url" >&2; exit 22;;\nesac\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)}
    return env, home


@pytest.mark.parametrize("name", FLAMENCO)
def test_linux_install_picks_its_line_from_the_combined_file(name: str, tmp_path: Path) -> None:
    env, home = _fake_env(tmp_path, None)
    proc = subprocess.run(
        ["sh", "-c", _render(name, "linux")], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert (home / ".local/opt/flamenco/flamenco-worker").is_file()


@pytest.mark.parametrize("name", FLAMENCO)
def test_linux_install_fails_when_its_line_has_the_wrong_hash(name: str, tmp_path: Path) -> None:
    env, home = _fake_env(tmp_path, "0" * 64)
    proc = subprocess.run(
        ["sh", "-c", _render(name, "linux")], env=env, capture_output=True, text=True
    )
    assert proc.returncode != 0
    assert not (home / ".local/opt/flamenco/flamenco-worker").exists()


def _status(url: str, method: str) -> int:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "spectracut-ci"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # follows redirects
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return err.code


@pytest.mark.skipif(
    os.environ.get("SPECTRACUT_NETWORK") != "1", reason="set SPECTRACUT_NETWORK=1 (CI network job)"
)
def test_every_url_in_the_active_set_resolves() -> None:
    bad = []
    for name, os_name, url in sorted(set(active_urls())):
        code = _status(url, "HEAD")
        if code in (403, 405, 501):  # some hosts refuse HEAD
            code = _status(url, "GET")
        if code != 200:
            bad.append(f"{code} {name}/{os_name} {url}")
    assert not bad, "non-200 URLs:\n" + "\n".join(bad)
