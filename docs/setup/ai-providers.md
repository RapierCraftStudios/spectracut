# AI providers: sign in with ChatGPT and Claude

Every AI call in SpectraCut goes through one provider layer (`src/spectracut/providers/`): board images, the vision judge, the writer and the agents that direct the harness. By default that layer uses **your own subscriptions** through the official CLIs, so you need no API keys:

| Provider | CLI | Sign in | Used for |
|----------|-----|---------|----------|
| ChatGPT | [Codex CLI](https://github.com/openai/codex) (`codex`) | `codex login` | image generation (boards), text |
| Claude | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) | `claude auth login` | vision judge, text, agents |

## Check your sign-in

```console
$ spectracut auth status
ChatGPT (codex CLI): signed in via ChatGPT
Claude (claude CLI at /usr/local/bin/claude): signed in via claude.ai, max
OPENAI_API_KEY: not set (optional)
ANTHROPIC_API_KEY: not set (optional)
```

The command asks the local CLIs (`codex login status`, `claude auth status --json`). It shows only whether you are signed in and how. It never prints tokens, account e-mails or key values.

## Tokens stay in the official CLIs

SpectraCut never reads, copies or stores your sign-in tokens. They stay in the CLIs' own stores (for example `~/.codex/` and Claude Code's credential store or keychain). Nothing token-shaped is written to SpectraCut's database, logs, specs or renders:
- Provider errors and log lines pass through a redaction filter.
- The CLIs' config directories are never overridden.

## Optional API-key fallback

If a CLI is not installed, the provider layer falls back to an API key from the environment:

| Need | Order of preference |
|------|---------------------|
| Images | Codex CLI (if installed), then `OPENAI_API_KEY` |
| Vision | Claude Code (if installed), then `ANTHROPIC_API_KEY` |

Keys are read at call time and sent only in the request header. They are never stored or logged.

When a subscription CLI runs, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are removed from its environment. This makes the CLI use your sign-in, so it never switches silently to key billing.

## Choosing the Claude command

The Claude provider runs the first of these that is set:

1. `SPECTRACUT_CLAUDE_BIN`, for example `export SPECTRACUT_CLAUDE_BIN=tokenbaton-exec`.
2. `claude_bin` in the `[ai]` table of the local config file:
   - The file is `$XDG_CONFIG_HOME/spectracut/config.toml`, which defaults to `~/.config/spectracut/config.toml`.
   - `SPECTRACUT_CONFIG` overrides the file path.
3. `claude` on `PATH`.

```toml
# ~/.config/spectracut/config.toml
[ai]
claude_bin = "tokenbaton-exec"   # or an absolute path
# codex_bin = "codex"            # SPECTRACUT_CODEX_BIN works the same way
```

A headless multi-account wrapper such as `tokenbaton-exec`, which picks an account per call and then execs the real `claude`, works as the Claude command.

The interactive `claude-go` launcher (and plain `tokenbaton`) is **not supported**: it waits for a keypress after `claude` exits, so a headless call would hang. SpectraCut rejects it with a clear error.

## Minimal, isolated calls

A bare `claude -p` loads your whole user setup (plugins, MCP servers, hooks, CLAUDE.md, skills), which was about 28k tokens of context per call. SpectraCut runs each provider call with a minimal isolated config instead.

**Claude:**

```
claude -p --output-format json --json-schema <score schema> \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --setting-sources "" --disable-slash-commands \
  --tools Read --allowedTools Read --no-session-persistence \
  --system-prompt "<short judge prompt>" "<prompt>"
```

- Each call runs in a fresh temporary directory, so no project CLAUDE.md is picked up.
- A vision-judge call measured about 4k tokens of context.
- `--bare` is deliberately **not** used: it ignores OAuth sign-in and accepts only `ANTHROPIC_API_KEY`.

**Codex:**

```
codex exec --ephemeral --skip-git-repo-check --ignore-user-config \
  --sandbox workspace-write -C <temp dir> -o <temp dir>/last-message.txt "<prompt>"
```

- It runs with the built-in image generation tool, in a fresh temporary directory.
- `--ignore-user-config` skips `config.toml` (user MCP servers and profiles). Your sign-in in Codex's auth store still applies.

## Errors

A missing, failed or expired sign-in raises a provider error that names the CLI and tells you how to sign in again. It is never returned as an empty result. For example:

```
claude: sign-in failed: OAuth token has expired. Please run /login (sign in with `claude auth login`)
```

A CLI that exits non-zero, returns nothing, or (for Codex) writes no image is also an error that names the CLI.

## Mocks for development and CI

Set `SPECTRACUT_PROVIDERS=mock` to use the deterministic mock providers:

| Mock | Output |
|------|--------|
| `providers/mock_image.py` | a solid PNG coloured from the prompt |
| `providers/mock_vision.py` | a score hashed from the frame and the rubric |

Unit tests use fake `codex`/`claude` scripts, so CI needs no sign-in and no network.

The real end-to-end checks run against your signed-in CLIs:

```
SPECTRACUT_E2E=1 uv run pytest -m e2e tests/providers/test_providers_e2e.py
```

## Provider terms (hosted cloud)

**Status: pending.** Before subscription sign-in is offered to other users in the hosted cloud, the ChatGPT/Codex and Claude/Claude Code terms must be checked. The quoted policy text that allows (or forbids) that use must be recorded in this section.

Until that is done, the cloud does not offer subscription sign-in. **The cloud requires each workspace's own API keys** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).

Self-hosted installs use the operator's own sign-in on their own machine, as described above.
