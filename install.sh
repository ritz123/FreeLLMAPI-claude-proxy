#!/usr/bin/env bash
# Install the Claude gateway from a fresh clone of this repo.
#
#   git clone <repo> ~/work/claude-gateway-sync
#   cd ~/work/claude-gateway-sync
#   ./install.sh
#
# The repo is the single source of truth. This script COPIES the tracked
# scripts into ~/.config/claude-gateway/ and the units into
# ~/.config/systemd/user/, builds the Rust binary, and registers the daily
# timer.
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
  "master_key": "CHANGE-ME-local-only",
  "yourprovider": {
    "type": "api",
    "key": "PASTE-KEY-HERE"
  }
}
JSON
  chmod 600 "$CFG/credentials.json"
  say "  ^ edit this before starting the gateway"
else
  say "credentials.json exists, leaving it alone"
fi

# ---- 3. systemd units (rewrite $HOME) --------------------------------------
mkdir -p "$UNITS"
for u in "$REPO"/systemd/*.service "$REPO"/systemd/*.timer; do
  name="$(basename "$u")"
  sed "s|/home/biplab|$HOME_|g; s|$HOME_/work/claude-gateway-sync|$REPO|g" "$u" > "$UNITS/$name"
  say "installed $UNITS/$name"
done

# ---- 4. build --------------------------------------------------------------
say "building the sync binary (this takes a minute on first run)"
( cd "$REPO" && cargo build --release ) >/dev/null
say "built $REPO/target/release/claude-gateway-sync"

# ---- 5. enable -------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now claude-gateway-sync.timer
say "timer enabled (daily at midnight, runs on login if missed)"

cat <<EOF

  next:
    1. edit $CFG/credentials.json   -- real keys + master_key
    2. $REPO/gateway/manage-providers.py add <prefix> <base-url> <ENV_VAR>
    3. systemctl --user start claude-gateway.service
    4. curl -H 'Authorization: Bearer <master_key>' http://127.0.0.1:4000/v1/models

  to uninstall:
    systemctl --user disable --now claude-gateway-sync.timer claude-gateway.service
    rm -f $UNITS/claude-gateway.* $CFG/{sync-models.py,manage-providers.py,start-gateway.sh}
    (leave credentials.json and the repo alone)
EOF
