# FreeLLMAPI-Claude-Proxy

Minimal LiteLLM proxy presenting FreeLLMAPI's `auto` router as a catalog-valid Anthropic model to Claude Code.

## Install

```bash
git clone <repo> <your-folder>
cd <your-folder>
./install.sh
```

**Prerequisites:** FreeLLMAPI running (any host/port), Python 3, LiteLLM (`pipx install litellm`), systemd user units.

The installer will prompt for your FreeLLMAPI base URL (default `http://localhost:3002`) and API key.

## What install.sh does

1. Prompts for FreeLLMAPI base URL and API key
2. Validates the key works (catalog + completion test)
3. Deploys proxy scripts to config directory
4. Configures `~/.claude/settings.json` automatically
5. Installs & starts `claude-gateway.service` on port 4000

## After install

Just run `claude`. The model `claude-3-5-sonnet-20241022` is pre-selected and routes through FreeLLMAPI's smart `auto` endpoint.

## Files

| location | purpose |
|---|---|
| `~/.config/claude-gateway/credentials.json` | your keys (mode 600) |
| `~/.config/claude-gateway/litellm_config.yaml` | static proxy config |
| `~/.config/claude-gateway/start-gateway.sh` | starts LiteLLM with env vars |
| `~/.config/claude-gateway/manage-providers.py` | add more providers later |

## Add another provider

```bash
<your-folder>/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>
```

## Logs

```bash
journalctl --user -u claude-gateway.service -f
```

## Uninstall

```bash
systemctl --user disable --now claude-gateway.service
rm -f ~/.config/systemd/user/claude-gateway.service ~/.config/claude-gateway/{sync-models.py,manage-providers.py,start-gateway.sh}
```