# claude-gateway

Minimal LiteLLM gateway presenting FreeLLMAPI's `auto` router as a catalog-valid Anthropic model to Claude Code.

## Install

```bash
git clone <repo> ~/work/claude-gateway-sync
cd ~/work/claude-gateway-sync
./install.sh
```

**Prerequisites:** FreeLLMAPI running on `localhost:3002`, Python 3, LiteLLM (`pipx install litellm`), systemd user units.

## What install.sh does

1. Checks FreeLLMAPI is reachable
2. Prompts for your FreeLLMAPI key (hidden input)
3. Validates the key works (catalog + completion test)
4. Deploys gateway scripts to `~/.config/claude-gateway/`
5. Configures `~/.claude/settings.json` automatically
6. Installs & starts `claude-gateway.service` on port 4000

## After install

Just run `claude`. The model `claude-3-5-sonnet-20241022` is pre-selected and routes through FreeLLMAPI's smart `auto` endpoint.

## Files

| location | purpose |
|---|---|
| `~/.config/claude-gateway/credentials.json` | your keys (mode 600) |
| `~/.config/claude-gateway/litellm_config.yaml` | static gateway config |
| `~/.config/claude-gateway/start-gateway.sh` | starts LiteLLM with env vars |
| `~/.config/claude-gateway/manage-providers.py` | add more providers later |

## Add another provider

```bash
~/work/claude-gateway-sync/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>
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