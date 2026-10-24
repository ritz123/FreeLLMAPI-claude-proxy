#!/usr/bin/env bash
# Install the Claude gateway from a fresh clone of this repo.
#
#   git clone <repo> ~/work/claude-gateway-sync
#   cd ~/work/claude-gateway-sync
#   ./install.sh
#
# The repo is the single source of truth. This script COPIES the tracked
# scripts into ~/.config/claude-gateway/ and the gateway systemd unit into
# ~/.config/systemd/user/, builds the Rust binary (for manage-providers.py
# validation), and enables the gateway service.
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

say() { printf '  %s\n' "$*"; }
die() { printf '  error: %s\n' "$*" >&2; exit 1; }

command -v cargo >/dev/null || die "cargo not found -- install Rust first: https://rustup.rs"
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

# ---- 4. build (for manage-providers.py validation) -------------------------
say "building validation binary (this takes a minute on first run)"
( cd "$REPO" && cargo build --release ) >/dev/null
say "built $REPO/target/release/claude-gateway-sync"

# ---- 5. enable gateway service ---------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now claude-gateway.service
say "gateway service enabled and started"

cat <<EOF

  next:
    1. edit $CFG/credentials.json   -- real freellmapi key + master_key
    2. systemctl --user restart claude-gateway.service
    3. curl -H 'Authorization: Bearer <master_key>' http://127.0.0.1:4000/v1/models
    4. configure Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:4000
       ANTHROPIC_AUTH_TOKEN=<master_key>
       ANTHROPIC_MODEL=claude-3-5-sonnet-20241022

  to add another provider:
    $REPO/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>

  to uninstall:
    systemctl --user disable --now claude-gateway.service
    rm -f $UNITS/claude-gateway.service $CFG/{sync-models.py,manage-providers.py,start-gateway.sh}
    (leave credentials.json and the repo alone)
EOF