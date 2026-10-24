# claude-gateway

Minimal LiteLLM gateway that presents FreeLLMAPI's `auto` router as a catalog-valid Anthropic model (`claude-3-5-sonnet-20241022`) to Claude Code.

## Architecture

```
Claude Code (port 4000) → LiteLLM Gateway → FreeLLMAPI (port 3002)
                              │
                              └──► model: hosted_vllm/auto
                                   api_base: http://localhost:3002/v1
```

No model probing, no nightly sync, no catalog caching. FreeLLMAPI's `auto` endpoint does its own smart routing (picks best available model per request). The gateway just forwards with a name the static picker recognizes.

## Installation

```bash
git clone <repo> ~/work/claude-gateway-sync
cd ~/work/claude-gateway-sync
./install.sh
```

`install.sh`:
- Deploys scripts to `~/.config/claude-gateway/`
- Installs systemd user unit `claude-gateway.service`
- Builds the Rust binary (unused at runtime, kept for `manage-providers.py` validation)
- Creates `credentials.json` template (edit with real keys)

## Configuration

### Required: `~/.config/claude-gateway/credentials.json` (mode 600)

```json
{
  "master_key": "sk-claude-gateway-local",
  "freellmapi": { "type": "api", "key": "freellmapi-...your-key..." }
}
```

### Optional: add more providers

```bash
MG=~/.config/claude-gateway/manage-providers.py
$MG add newco https://api.newco.com NEWCO_API_KEY
```

This updates all four config sites atomically:
1. `credentials.json` — the key
2. `start-gateway.sh` — exports `NEWCO_API_KEY` env var
3. `src/main.rs` — `PROVIDERS` table (for validation)
4. `gateway/sync-models.py` — `PROVIDERS` list (Python fallback)

Then rebuild: `cargo build --release` (only needed after provider changes).

## Files in this repo

| path | role |
|---|---|
| `src/main.rs` | provider definitions + validation logic for `manage-providers.py` |
| `gateway/manage-providers.py` | add/remove/list providers, deploys to `~/.config` |
| `gateway/start-gateway.sh` | injects credentials as env vars, starts LiteLLM |
| `gateway/sync-models.py` | Python fallback (not used) |
| `systemd/claude-gateway.service` | systemd user unit for the gateway |
| `install.sh` | deploy repo → `~/.config/claude-gateway/` + systemd unit |

## Files at runtime (`~/.config/claude-gateway/`)

| file | description |
|---|---|
| `credentials.json` | **your keys** (mode 600, not tracked) |
| `litellm_config.yaml` | **static** — hand-edited, not generated |
| `start-gateway.sh` | deployed copy, reads keys from credentials.json |
| `manage-providers.py` | deployed copy for adding providers later |

## Dependencies

- **Rust** (`cargo`) — only for building the validation binary used by `manage-providers.py`
- **Python 3** — for `manage-providers.py` and `start-gateway.sh`'s `readkey` helper
- **LiteLLM** — installed via `uv`/`pipx` (`~/.local/bin/litellm`)
- **FreeLLMAPI** — running on `localhost:3002` (external to this project)
- **systemd user units** — for auto-start on login

## Claude Code setup

In `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_AUTH_TOKEN": "sk-claude-gateway-local",
    "ANTHROPIC_MODEL": "claude-3-5-sonnet-20241022",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-3-5-sonnet-20241022",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-3-5-sonnet-20241022",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-3-5-sonnet-20241022",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1048576"
  }
}
```

The model name `claude-3-5-sonnet-20241022` exists in models.dev's catalog, so the static picker shows no warning. At runtime it hits FreeLLMAPI's `auto` router.

## Why not the old sync?

The original `claude-gateway-sync` probed every model in every provider's catalog (63+ models, 10–25 min cold). FreeLLMAPI's `auto` makes that obsolete — it's one endpoint that routes intelligently. One static config, zero maintenance.