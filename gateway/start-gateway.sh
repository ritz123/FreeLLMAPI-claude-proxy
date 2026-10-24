#!/usr/bin/env bash
# Start LiteLLM gateway translating Claude Code (Anthropic) -> cloud providers.
#
# WHY THIS FILE EXISTS
#   litellm_config.yaml refers to credentials by env var name
#   (api_key: os.environ/ABOVE_API_KEY), so the keys must be in the environment
#   before litellm starts. This script is the systemd unit's ExecStart and is
#   the single place that injection happens.
#
# WHY credentials.json INSTEAD OF opencode's auth.json
#   OpenCode treats a provider as selectable as soon as a credential for it
#   exists in auth.json, and populates that provider's model list from the
#   models.dev catalog without verifying anything. With above/nvidia in
#   auth.json that produced 112 unusable entries -- above's 9 listed names all
#   returned 403 because they never existed upstream, and most of nvidia's 103
#   were decommissioned. Moving the keys here keeps the gateway working while
#   removing them from the picker.
#
# The readkey fallback means a missing credentials.json degrades to auth.json
# with a warning rather than failing the unit, so a deletion cannot brick the
# gateway. Note `set -euo pipefail`: a KeyError here would abort startup
# entirely, which is why both paths are handled explicitly.
#
# Keys are never printed. Run by claude-gateway.service.
#
# Each `export` line below corresponds to one provider in
# credentials.json, and the `readkey` call is what binds the two. Adding or
# removing a provider means editing this file too -- use
# manage-providers.py, which updates this script, credentials.json, main.rs and
# sync-models.py together, then rebuilds.
set -euo pipefail

CRED=/home/biplab/.config/claude-gateway/credentials.json
FALLBACK=/home/biplab/.local/share/opencode/auth.json

readkey() {  # $1 = provider id
  local src="$CRED"
  if [ ! -r "$src" ]; then
    src="$FALLBACK"
    echo "claude-gateway: $CRED unreadable, falling back to opencode auth.json" >&2
  fi
  python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]['key'])" "$src" "$1"
}

export ABOVE_API_KEY="$(readkey above)"
export NVIDIA_API_KEY="$(readkey nvidia)"



export FREELLMAPI_API_KEY="$(readkey freellmapi)"

exec ~/.local/bin/litellm --config /home/biplab/.config/claude-gateway/litellm_config.yaml --host 127.0.0.1 --port 4000
