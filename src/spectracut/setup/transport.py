# SPDX-License-Identifier: AGPL-3.0-or-later
"""Framed stdin for PowerShell steps: the remote never waits for EOF.

Windows OpenSSH intermittently loses stdin EOF, so ``[Console]::In.ReadToEnd()``
can hang forever. A PowerShell step's stdin is sent as a fixed 74-char header (the
payload length as 10 digits, then its sha256 hex) followed by the base64 of the
UTF-8 text. The remote reads exactly that many chars, checks the sha256 and sets
``$StepInput``. Length and hash travel in-band, never on the command line, so no
hash of a secret shows up in a process list. Base64 keeps the frame ASCII, so the
console input code page cannot change its char count.
"""

from __future__ import annotations

import base64
import hashlib

STEP_INPUT = "$StepInput"
LENGTH_DIGITS = 10
HEADER_CHARS = LENGTH_DIGITS + 64
SHORT_READ_EXIT = 3
MISMATCH_EXIT = 4


def frame_input(text: str) -> str:
    """``text`` framed for :func:`windows_input_preamble`."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return f"{len(payload):0{LENGTH_DIGITS}d}{digest}{payload}"


def windows_input_preamble() -> str:
    """PowerShell that reads one frame from stdin into ``$StepInput`` (exact counts only)."""
    return (
        "function Read-StepChars([int]$n) { "
        "$b = New-Object char[] $n; $o = 0; "
        "while ($o -lt $n) { $k = [Console]::In.Read($b, $o, $n - $o); "
        "if ($k -le 0) { [Console]::Error.WriteLine("
        "'step input: short read ' + $o + ' of ' + $n + ' chars'); "
        f"exit {SHORT_READ_EXIT} }}; $o += $k }}; "
        "return [string]::new($b, 0, $n) }; "
        f"$h = Read-StepChars {HEADER_CHARS}; "
        f"$n = [int]$h.Substring(0, {LENGTH_DIGITS}); $want = $h.Substring({LENGTH_DIGITS}); "
        "$pl = Read-StepChars $n; "
        "$sha = [Security.Cryptography.SHA256]::Create(); "
        "$got = -join ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($pl)) | "
        "ForEach-Object { $_.ToString('x2') }); "
        "if ($got -ne $want) { [Console]::Error.WriteLine('step input sha256 mismatch'); "
        f"exit {MISMATCH_EXIT} }}; "
        f"{STEP_INPUT} = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($pl)); "
        "Remove-Variable h, n, want, pl, sha, got; "
    )
