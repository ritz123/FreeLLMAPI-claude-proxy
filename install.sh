#!/usr/bin/env bash
# Install FreeLLMAPI-Claude-Proxy from a fresh clone of this repo.
#
#   git clone <repo> <your-folder>
#   cd <your-folder>
#   ./install.sh
#
# The repo is the single source of truth. This script COPIES the tracked
# scripts into ~/.config/claude-gateway/ and the gateway systemd unit into
# ~/.config/systemd/user/, prompts for FreeLLMAPI URL + key, validates them,
# configures Claude Code settings, and enables the gateway service.
#
# Copies, not symlinks: a real file at the destination behaves predictably for
# editors, backup tools and anything that stats the path. The cost is that they
# can drift -- so re-run this script after editing anything in gateway/ or
# systemd/, and note that manage-providers.py deploys automatically after it
# edits. Idempotent, so re-running after a `git pull` is always safe.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_="${HOME}"
CFG="$HOME_/.config/claude-gateway"
UNITS="$HOME_/.config/systemd/user"
CLAUDE_SETTINGS="$HOME_/.claude/settings.json"

say() { printf '  %s\n' "$*"; }
die() { printf '  error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"
command -v curl >/dev/null || die "curl not found"

say "repo: $REPO"

# ---- 0. prompt for FreeLLMAPI base URL + key (read FIRST, before any heredoc) --
DEFAULT_FREELLAPI_URL="http://localhost:3002"
if [ -t 0 ]; then
  printf "  FreeLLMAPI base URL [%s]: " "$DEFAULT_FREELLAPI_URL"
  IFS= read -r FREELLAPI_URL
  FREELLAPI_URL="${FREELLAPI_URL:-$DEFAULT_FREELLAPI_URL}"
  printf "  Enter your FreeLLMAPI key (input hidden): "
  IFS= read -rs FREELLAPI_KEY
  echo
else
  IFS= read -r FREELLAPI_URL
  FREELLAPI_URL="${FREELLAPI_URL:-$DEFAULT_FREELLAPI_URL}"
  IFS= read -r FREELLAPI_KEY
fi

# Normalize URL: strip trailing slash, ensure /v1 suffix
FREELLAPI_URL="${FREELLAPI_URL%/}"
if [[ "$FREELLAPI_URL" != */v1 ]]; then
  FREELLAPI_URL="${FREELLAPI_URL}/v1"
fi
say "using FreeLLMAPI at $FREELLAPI_URL"

if [ -z "$FREELLAPI_KEY" ]; then
  die "no FreeLLMAPI key supplied"
fi

# ---- 1. check FreeLLMAPI is running ----------------------------------------
say "checking FreeLLMAPI health..."
HEALTH_URL="${FREELLAPI_URL%/v1}/health"
if ! curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" --max-time 5 | grep -q 200; then
  die "FreeLLMAPI not responding on $HEALTH_URL. Check the URL and that it's running."
fi
say "FreeLLMAPI is reachable"

# ---- 2. validate key -------------------------------------------------------
say "validating key against FreeLLMAPI..."
CATALOG_RESP=$(curl -s -w "\n%{http_code}" "$FREELLAPI_URL/models" \
  -H "Authorization: Bearer $FREELLAPI_KEY" --max-time 10)
HTTP_CODE=$(echo "$CATALOG_RESP" | tail -1)
if [ "$HTTP_CODE" != "200" ]; then
  die "FreeLLMAPI catalog fetch failed (HTTP $HTTP_CODE). Check your key."
fi
MODEL_COUNT=$(echo "$CATALOG_RESP" | head -1 | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data', d)))")
say "catalog OK, $MODEL_COUNT models advertised"

COMP_RESP=$(curl -s -w "\n%{http_code}" "$FREELLAPI_URL/chat/completions" \
  -H "Authorization: Bearer $FREELLAPI_KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' --max-time 15)
HTTP_CODE=$(echo "$COMP_RESP" | tail -1)
if [ "$HTTP_CODE" != "200" ]; then
  die "FreeLLMAPI completion test failed (HTTP $HTTP_CODE). Key may be invalid or no providers configured."
fi
say "key validated - FreeLLMAPI auto router works"

# ---- 3. config dir + deploy tracked scripts --------------------------------
mkdir -p "$CFG"
chmod 700 "$CFG"
for f in sync-models.py manage-providers.py start-gateway.sh; do
  install -m 0755 "$REPO/gateway/$f" "$CFG/$f"
  say "deployed $CFG/$f"
done

# Update sync-models.py with the configured FreeLLMAPI URL
sed -i "s|http://localhost:3002/v1|$FREELLAPI_URL|g" "$REPO/gateway/sync-models.py"
sed -i "s|http://localhost:3002/v1|$FREELLAPI_URL|g" "$CFG/sync-models.py"
say "updated sync-models.py with FreeLLMAPI URL"

# ---- 4. write credentials.json ---------------------------------------------
cat > "$CFG/credentials.json" <<JSON
{
  "master_key": "sk-claude-gateway-local",
  "freellmapi": {
    "type": "api",
    "key": "$FREELLAPI_KEY"
  }
}
JSON
chmod 600 "$CFG/credentials.json"
say "credentials.json written (mode 600)"

# ---- 5. systemd unit (rewrite $HOME) ---------------------------------------
mkdir -p "$UNITS"
for u in "$REPO"/systemd/claude-gateway.service; do
  name="$(basename "$u")"
  sed "s|/home/biplab|$HOME_|g; s|$HOME_/work/claude-gateway-sync|$REPO|g" "$u" > "$UNITS/$name"
  say "installed $UNITS/$name"
done

# ---- 6. configure Claude Code settings -------------------------------------
say "configuring Claude Code settings..."
python3 - <<PYEOF
import json, os, sys
from pathlib import Path

settings_path = Path("$CLAUDE_SETTINGS")
settings_path.parent.mkdir(parents=True, exist_ok=True)

if settings_path.exists():
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except json.JSONDecodeError:
        print(f"  warning: {settings_path} is invalid JSON, backing up and replacing")
        settings_path.rename(settings_path.with_suffix(".json.bak"))
        settings = {}
else:
    settings = {}

if "env" not in settings:
    settings["env"] = {}

env = settings["env"]
env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:4000"
env["ANTHROPIC_AUTH_TOKEN"] = "sk-claude-gateway-local"
env["ANTHROPIC_API_KEY"] = ""
env["ANTHROPIC_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "claude-3-5-sonnet-20241022"
env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1048576"

if "modelSettings" not in settings:
    settings["modelSettings"] = {}
settings["modelSettings"]["claude-3-5-sonnet-20241022"] = {"effortLevel": "high"}

tmp = settings_path.with_suffix(".json.tmp")
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
tmp.replace(settings_path)

print(f"  updated {settings_path}")
PYEOF

# ---- 7. enable gateway service ---------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now claude-gateway.service
say "gateway service enabled and started"

# ---- 8. wait for gateway and verify ----------------------------------------
say "waiting for gateway to be ready..."
for i in {1..10}; do
  if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4000/v1/models \
     -H "Authorization: Bearer sk-claude-gateway-local" --max-time 3 | grep -q 200; then
    say "gateway is up"
    break
  fi
  sleep 1
done

MODELS=$(curl -s http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer sk-claude-gateway-local" --max-time 5 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print([m['id'] for m in d['data']])")
say "gateway advertising: $MODELS"

cat <<EOF

  ✓ Installation complete!
  
  Next: run 'claude' — it will use the gateway automatically.
  
  Your FreeLLMAPI key is stored in: $CFG/credentials.json
  Gateway logs: journalctl --user -u claude-gateway.service -f
  
  To add another provider later:
    $REPO/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>
  
  To uninstall:
    systemctl --user disable --now claude-gateway.service
    rm -f $UNITS/claude-gateway.service $CFG/{sync-models.py,manage-providers.py,start-gateway.sh}
    (leave credentials.json and the repo alone)
EOF