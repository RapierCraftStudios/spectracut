# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the AI provider layer, using fake ``codex``/``claude`` CLIs."""

from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spectracut.providers import base, config
from spectracut.providers.api_key import AnthropicKeyVisionJudge, OpenAIKeyImageProvider
from spectracut.providers.base import (
    ImageProvider,
    ProviderAuthError,
    ProviderError,
    VisionJudge,
    redact,
    select_image_provider,
    select_vision_judge,
)
from spectracut.providers.claude_code import (
    JUDGE_SYSTEM_PROMPT,
    ClaudeCodeTextProvider,
    ClaudeCodeVisionJudge,
    claude_judge_argv,
    claude_status,
    claude_text_argv,
    parse_judge_output,
    parse_text_output,
)
from spectracut.providers.codex_cli import CodexCliImageProvider, codex_status
from spectracut.providers.mock_image import MockImageProvider
from spectracut.providers.mock_vision import MockVisionJudge

PROVIDERS_DIR = Path(base.__file__).resolve().parent


def _frame(tmp_path: Path) -> Path:
    return MockImageProvider().generate("frame", tmp_path / "in" / "frame.png", size="8x6")


# --- Claude command configuration (R10, R11, R12) ---------------------------


def test_claude_bin_defaults_to_claude_on_path(isolated_env: Path) -> None:
    assert config.configured_claude_bin() == "claude"
    assert config.resolve_claude_bin() is None  # PATH is empty in tests


def test_claude_bin_from_env(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, make_fake_claude: Callable[[str], Path]
) -> None:
    wrapper = make_fake_claude("tokenbaton-exec")
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", str(wrapper))
    assert config.resolve_claude_bin() == str(wrapper)


def test_claude_bin_from_local_config(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, make_fake_claude: Callable[[str], Path]
) -> None:
    wrapper = make_fake_claude("tokenbaton-exec")
    monkeypatch.setenv("PATH", str(wrapper.parent))
    cfg = isolated_env / "xdg" / "spectracut" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[ai]\nclaude_bin = "tokenbaton-exec"\n')
    assert config.configured_claude_bin() == "tokenbaton-exec"
    assert config.resolve_claude_bin() == str(wrapper)


def test_env_beats_local_config(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = isolated_env / "custom.toml"
    cfg.write_text('[ai]\nclaude_bin = "from-config"\n')
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(cfg))
    assert config.configured_claude_bin() == "from-config"
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", "from-env")
    assert config.configured_claude_bin() == "from-env"


@pytest.mark.parametrize("value", ["claude-go", "/opt/tools/claude-go", "tokenbaton"])
def test_interactive_launcher_is_rejected(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", value)
    with pytest.raises(ProviderError, match="keypress"):
        config.configured_claude_bin()


def test_interactive_launcher_rejected_from_config(isolated_env: Path) -> None:
    cfg = isolated_env / "xdg" / "spectracut" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[ai]\nclaude_bin = "claude-go"\n')
    with pytest.raises(ProviderError, match="claude-go"):
        config.resolve_claude_bin()


def test_headless_wrapper_runs_the_judge(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_fake_claude: Callable[[str], Path],
    fakes: Any,
) -> None:
    wrapper = make_fake_claude("tokenbaton-exec")
    record = isolated_env / "calls.jsonl"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", str(wrapper))
    verdict = ClaudeCodeVisionJudge().score(_frame(isolated_env), "legible UI")
    assert verdict.score == pytest.approx(0.8)
    assert fakes.calls(record)[0]["argv"][0] == "-p"


# --- Isolation (R13, R14) ----------------------------------------------------


def test_claude_call_is_isolated_and_small() -> None:
    argv = claude_judge_argv("claude", "score it")
    assert "--bare" not in argv  # --bare disables OAuth sign-in
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""  # no user plugins or hooks
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == JUDGE_SYSTEM_PROMPT
    assert len(JUDGE_SYSTEM_PROMPT) < 600


def test_judge_runs_in_scratch_dir_without_api_keys(
    fake_bins: dict[str, Path], api_keys: dict[str, str], fakes: Any
) -> None:
    frame = _frame(fake_bins["dir"].parent)
    ClaudeCodeVisionJudge().score(frame, "legible")
    call = fakes.calls(fake_bins["record"])[0]
    assert call["keys"] == []  # subscription path: keys never reach the CLI
    assert Path(call["cwd"]) != frame.parent
    assert "--setting-sources" in call["argv"]


def test_codex_call_is_isolated(
    fake_bins: dict[str, Path], api_keys: dict[str, str], fakes: Any
) -> None:
    out = fake_bins["dir"].parent / "boards" / "b1.png"
    CodexCliImageProvider().generate("a desk", out)
    argv = fakes.calls(fake_bins["record"])[0]["argv"]
    assert argv[0] == "exec"
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert fakes.calls(fake_bins["record"])[0]["keys"] == []


# --- Providers through the fake CLIs (R2, R3, R7, R8) ------------------------


def test_codex_generates_board_frame(fake_bins: dict[str, Path]) -> None:
    out = fake_bins["dir"].parent / "boards" / "b1.png"
    assert CodexCliImageProvider().generate("a desk", out) == out
    assert out.read_bytes().startswith(b"\x89PNG")


def test_claude_scores_frame(fake_bins: dict[str, Path]) -> None:
    judge = ClaudeCodeVisionJudge()
    verdict = judge.score(_frame(fake_bins["dir"].parent), "legible")
    assert verdict.score == pytest.approx(0.8)
    assert verdict.notes == "clear and legible"
    assert judge.last_usage["cache_read_input_tokens"] == 3000


def test_parse_judge_output_falls_back_to_result_text() -> None:
    out = json.dumps({"is_error": False, "result": '```json\n{"score": 1.4, "notes": "x"}\n```'})
    verdict, _ = parse_judge_output(out)
    assert verdict.score == 1.0


# --- Failed or expired sign-in (R15) ----------------------------------------


def test_expired_claude_sign_in_is_a_named_error(
    fake_bins: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fakes: Any,
) -> None:
    monkeypatch.setenv("FAKE_MODE", "expired")
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderAuthError) as info:
        ClaudeCodeVisionJudge().score(_frame(fake_bins["dir"].parent), "legible")
    assert info.value.cli == "claude"
    assert str(info.value).startswith("claude:")
    assert "claude auth login" in str(info.value)
    assert fakes.no_secret(str(info.value)) and fakes.no_secret(caplog.text)


def test_claude_is_error_with_zero_exit_is_auth_error(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "is-error-ok-exit")
    with pytest.raises(ProviderAuthError, match="claude"):
        ClaudeCodeVisionJudge().score(_frame(fake_bins["dir"].parent), "legible")


def test_empty_claude_output_is_an_error_not_an_empty_result(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "empty")
    with pytest.raises(ProviderError, match="^claude: .*empty"):
        ClaudeCodeVisionJudge().score(_frame(fake_bins["dir"].parent), "legible")


def test_expired_codex_sign_in_is_a_named_error(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch, fakes: Any
) -> None:
    monkeypatch.setenv("FAKE_MODE", "expired")
    with pytest.raises(ProviderAuthError) as info:
        CodexCliImageProvider().generate("x", fake_bins["dir"].parent / "o.png")
    assert info.value.cli == "codex"
    assert "codex login" in str(info.value)
    assert fakes.no_secret(str(info.value))


def test_codex_without_image_is_an_error(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "no-image")
    out = fake_bins["dir"].parent / "o.png"
    with pytest.raises(ProviderError, match="^codex: .*no PNG"):
        CodexCliImageProvider().generate("x", out)
    assert not out.exists()


def test_missing_cli_is_a_named_error(isolated_env: Path) -> None:
    with pytest.raises(ProviderError, match="^claude: .*not found"):
        ClaudeCodeVisionJudge("definitely-missing-claude").score(_frame(isolated_env), "x")


# --- Sign-in state and tokens (R6, R9) ---------------------------------------


def test_status_reports_sign_in_without_account_or_tokens(
    fake_bins: dict[str, Path], fakes: Any
) -> None:
    claude = claude_status()
    assert claude.signed_in and claude.method == "claude.ai, max"
    assert fakes.email not in repr(claude) and fakes.no_secret(repr(claude))
    codex = codex_status()
    assert codex.signed_in and codex.method == "ChatGPT"


def test_status_reports_signed_out(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "signed-out")
    assert not claude_status().signed_in and claude_status().installed
    assert not codex_status().signed_in and codex_status().installed


def test_status_when_cli_missing(isolated_env: Path) -> None:
    assert not claude_status().installed
    assert not codex_status().installed


def test_redact_masks_token_shapes(fakes: Any) -> None:
    text = f"token {fakes.token} bearer: Bearer abc.def-ghi and sk-proj-{'a' * 30}"
    masked = redact(text)
    assert fakes.token not in masked and "abc.def-ghi" not in masked and "sk-proj" not in masked
    assert redact("/usr/bin/claude exited 1") == "/usr/bin/claude exited 1"


def test_provider_code_never_touches_cli_token_stores() -> None:
    forbidden = ("auth.json", ".credentials", "CLAUDE_CONFIG_DIR", "keychain", "accessToken")
    for path in PROVIDERS_DIR.glob("*.py"):
        source = path.read_text()
        for needle in forbidden:
            assert needle not in source, f"{path.name} references {needle}"


def test_provider_error_redacts_its_message(fakes: Any) -> None:
    err = ProviderError("claude", f"boom {fakes.token}")
    assert fakes.no_secret(str(err))


# --- API-key fallback (R4) ---------------------------------------------------


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_openai_key_fallback(isolated_env: Path, api_keys: dict[str, str]) -> None:
    import base64

    seen: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, timeout: float) -> _Resp:
        seen.append(request)
        return _Resp({"data": [{"b64_json": base64.b64encode(b"\x89PNGdata").decode()}]})

    out = OpenAIKeyImageProvider(opener=opener).generate("x", isolated_env / "o.png")
    assert out.read_bytes() == b"\x89PNGdata"
    assert seen[0].get_header("Authorization") == f"Bearer {api_keys['OPENAI_API_KEY']}"


def test_anthropic_key_fallback(isolated_env: Path, api_keys: dict[str, str]) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Resp:
        assert request.get_header("X-api-key") == api_keys["ANTHROPIC_API_KEY"]
        return _Resp({"content": [{"type": "text", "text": '{"score": 0.5, "notes": "ok"}'}]})

    verdict = AnthropicKeyVisionJudge(opener=opener).score(_frame(isolated_env), "legible")
    assert verdict.score == 0.5


def test_rejected_key_is_auth_error_without_the_key(
    isolated_env: Path, api_keys: dict[str, str]
) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "no",
            email.message.Message(),
            io.BytesIO(api_keys["OPENAI_API_KEY"].encode()),
        )

    with pytest.raises(ProviderAuthError) as info:
        OpenAIKeyImageProvider(opener=opener).generate("x", isolated_env / "o.png")
    assert api_keys["OPENAI_API_KEY"] not in str(info.value)
    assert "OPENAI_API_KEY" in str(info.value)


def test_key_provider_without_key_is_auth_error(isolated_env: Path) -> None:
    with pytest.raises(ProviderAuthError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicKeyVisionJudge().score(_frame(isolated_env), "x")


# --- Selection and mocks (R1, R3, R4, R5) ------------------------------------


def test_selection_prefers_subscription_cli(
    fake_bins: dict[str, Path], api_keys: dict[str, str]
) -> None:
    assert isinstance(select_image_provider(), CodexCliImageProvider)
    assert isinstance(select_vision_judge(), ClaudeCodeVisionJudge)


def test_selection_falls_back_to_api_keys(isolated_env: Path, api_keys: dict[str, str]) -> None:
    assert isinstance(select_image_provider(), OpenAIKeyImageProvider)
    assert isinstance(select_vision_judge(), AnthropicKeyVisionJudge)


def test_selection_without_cli_or_key_explains_both(isolated_env: Path) -> None:
    with pytest.raises(ProviderAuthError, match="OPENAI_API_KEY"):
        select_image_provider()
    with pytest.raises(ProviderAuthError, match="ANTHROPIC_API_KEY"):
        select_vision_judge()


def test_mock_mode(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    image, judge = select_image_provider(), select_vision_judge()
    assert isinstance(image, MockImageProvider) and isinstance(judge, MockVisionJudge)
    assert isinstance(image, ImageProvider) and isinstance(judge, VisionJudge)
    a = image.generate("same", isolated_env / "a.png", size="4x3")
    b = image.generate("same", isolated_env / "b.png", size="4x3")
    assert a.read_bytes() == b.read_bytes() and a.read_bytes().startswith(b"\x89PNG")
    first, second = judge.score(a, "r"), judge.score(b, "r")
    assert first == second and 0.0 <= first.score <= 1.0


def test_mock_png_is_valid() -> None:
    import struct
    import zlib

    from spectracut.providers.mock_image import solid_png

    data = solid_png(3, 2, (1, 2, 3))
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (3, 2)
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41 : 41 + idat_len])
    assert raw == (b"\x00" + bytes((1, 2, 3)) * 3) * 2


def test_mock_judge_missing_frame(isolated_env: Path) -> None:
    with pytest.raises(ProviderError):
        MockVisionJudge().score(isolated_env / "nope.png", "x")


def test_token_constant_is_token_shaped(fakes: Any) -> None:
    assert redact(fakes.token) != fakes.token


# --- Claude Code text provider (#24) -----------------------------------------


def test_claude_text_call_is_isolated_with_no_tools() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    argv = claude_text_argv("claude", "SYSTEM", "PROMPT", schema)
    assert "--bare" not in argv
    assert argv[argv.index("--json-schema") + 1] == json.dumps(schema, separators=(",", ":"))
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM"
    assert argv[-1] == "PROMPT"


def test_claude_text_provider_completes_through_sign_in(
    fake_bins: dict[str, Path], api_keys: dict[str, str], fakes: Any
) -> None:
    provider = ClaudeCodeTextProvider()
    assert isinstance(provider, base.TextProvider)
    out = provider.complete("sys", "say it", schema={"type": "object"})
    assert out.output == {"score": 0.8, "notes": "clear and legible"}
    assert out.usage == {
        "claude_requests": 1,
        "claude_input_tokens": 3010,
        "claude_output_tokens": 0,
    }
    call = fakes.calls(fake_bins["record"])[0]
    assert call["keys"] == []
    assert "--json-schema" in call["argv"]


def test_claude_text_provider_expired_sign_in_is_auth_error(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "expired")
    with pytest.raises(ProviderAuthError, match="claude"):
        ClaudeCodeTextProvider().complete("sys", "say it", schema={"type": "object"})


def test_parse_text_output_falls_back_and_rejects_non_objects() -> None:
    out, _ = parse_text_output(json.dumps({"is_error": False, "result": '```json\n{"a": 1}\n```'}))
    assert out == {"a": 1}
    with pytest.raises(ProviderError, match="empty"):
        parse_text_output(json.dumps({"is_error": False, "result": ""}))
    with pytest.raises(ProviderError, match="JSON object"):
        parse_text_output(json.dumps({"is_error": False, "result": "no json here"}))
