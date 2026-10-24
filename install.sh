#!/usr/bin/env bash
# Install the Claude gateway from a fresh clone of this repo.
#
#   git clone <repo> ~/work/claude-gateway-sync
#   cd ~/work/claude-gateway-sync
#   ./install.sh
#
# The repo is the single source of truth. This script COPIES the tracked
# scripts into ~/.config/claude-gateway/ and the gateway systemd unit into
# ~/.config/systemd/user/, configures Claude Code settings, and enables the gateway service.
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

say "repo: $REPO"

# ---- 1. config dir + deploy tracked scripts --------------------------------
mkdir -p "$CFG"
chmod 700 "$CFG"
for f in sync-models.py manage-providers.py start-gateway.sh; do
  install -m 0755 "$REPO/gateway/$f" "$CFG/$f"
  say "deployed $CFG/$f"
done

# ---- 2. credentials -------------------------------------------------------
if [ ! -f "$CFG/credentials.json" ]; then
  say "creating $CFG/credentials.json -- ADD YOUR REAL KEYS, it is untracked by design"
  cat > "$CFG/credentials.json" <<'JSON'
{
  "master_key": "sk-claude-gateway-local",
  "freellmapi": {
    "type": "api",
    "key": "PASTE-YOUR-FREELLAPI-KEY-HERE"
  }
}
JSON
  chmod 600 "$CFG/credentials.json"
  say "  ^ edit this before starting the gateway"
else
  say "credentials.json exists, leaving it alone"
fi

# ---- 3. systemd unit (rewrite $HOME) ---------------------------------------
mkdir -p "$UNITS"
for u in "$REPO"/systemd/claude-gateway.service; do
  name="$(basename "$u")"
  sed "s|/home/biplab|$HOME_|g; s|$HOME_/work/claude-gateway-sync|$REPO|g" "$u" > "$UNITS/$name"
  say "installed $UNITS/$name"
done

# ---- 4. configure Claude Code settings -------------------------------------
say "configuring Claude Code settings..."
python3 - <<PYEOF
import json, os, sys
from pathlib import Path

settings_path = Path("$CLAUDE_SETTINGS")
settings_path.parent.mkdir(parents=True, exist_ok=True)

# Load existing or create new
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

# Ensure env dict exists
if "env" not in settings:
    settings["env"] = {}

env = settings["env"]

# Gateway configuration
env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:4000"
env["ANTHROPIC_AUTH_TOKEN"] = "sk-claude-gateway-local"
env["ANTHROPIC_API_KEY"] = ""
env["ANTHROPIC_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-3-5-sonnet-20241022"
env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "claude-3-5-sonnet-20241022"
env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1048576"

# Preserve existing modelSettings if present, update the key
if "modelSettings" not in settings:
    settings["modelSettings"] = {}
settings["modelSettings"]["claude-3-5-sonnet-20241022"] = {"effortLevel": "high"}

# Write atomically
tmp = settings_path.with_suffix(".json.tmp")
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
tmp.replace(settings_path)

print(f"  updated {settings_path}")
PYEOF

# ---- 5. enable gateway service ---------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now claude-gateway.service
say "gateway service enabled and started"

cat <<EOF

  next:
    1. edit $CFG/credentials.json   -- real freellmapi key + master_key
    2. systemctl --user restart claude-gateway.service
    3. curl -H 'Authorization: Bearer <master_key>' http://127.0.0.1:4000/v1/models
    4. run 'claude' -- model is pre-configured

  to add another provider:
    $REPO/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>

  to uninstall:
    systemctl --user disable --now claude-gateway.service
    rm -f $UNITS/claude-gateway.service $CFG/{sync-models.py,manage-providers.py,start-gateway.sh}
    (leave credentials.json and the repo alone)
EOF