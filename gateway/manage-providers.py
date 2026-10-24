#!/usr/bin/env python3
"""Add, list, or remove a LiteLLM gateway provider across three config sites.

WHY A SCRIPT
    Adding a provider means touching three files that must stay in sync.
    Doing it by hand is easy to get half-right. This script validates the
    credential and catalog BEFORE editing anything, so a bad provider never
    reaches the config.

WHAT IT TOUCHES
    ~/.config/claude-gateway/credentials.json          the key
    ~/.config/claude-gateway/start-gateway.sh          env var for litellm
    ~/work/claude-gateway-sync/gateway/sync-models.py  PROVIDERS list (fallback)

    All three are edited together or not at all.

USAGE
    manage-providers.py list
    manage-providers.py add   [PREFIX] [BASE_URL] [ENV_VAR] [API_KEY]
    manage-providers.py remove PREFIX

    With no arguments, `add` prompts interactively. The API key can be given
    as an argument or read from stdin so it never lands in shell history.
    Every subcommand accepts --dry-run.

ROLLBACK
    Each mutating command writes timestamped .bak-<stamp> files next to each
    target before editing, and prints the exact command to undo the change.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = os.path.expanduser("~")
CRED = f"{HOME}/.config/claude-gateway/credentials.json"


def find_repo() -> str:
    """Locate the repo root, whether run from the repo or the installed copy.

    Deriving it from __file__ alone is not enough: this script is *copied* to
    ~/.config/claude-gateway/, where walking up from __file__ lands in ~/.config
    rather than the repo. So walk up looking for a marker, and fall back to
    $CLAUDE_GATEWAY_REPO or the conventional path.
    """
    env = os.environ.get("CLAUDE_GATEWAY_REPO")
    if env and os.path.exists(f"{env}/gateway/manage-providers.py"):
        return env
    for parent in Path(__file__).resolve().parents:
        if (parent / "gateway").is_dir():
            return str(parent)
    default = f"{HOME}/work/claude-gateway-sync"
    if os.path.exists(f"{default}/gateway/manage-providers.py"):
        return default
    print(
        "  error: cannot locate the claude-gateway-sync repo. Set CLAUDE_GATEWAY_REPO "
        f"to its path (looked in {default}).",
        file=sys.stderr,
    )
    sys.exit(1)


REPO = find_repo()
DEST = f"{HOME}/.config/claude-gateway"
START = f"{REPO}/gateway/start-gateway.sh"
PY = f"{REPO}/gateway/sync-models.py"

DEPLOYED = {
    f"{REPO}/gateway/sync-models.py": f"{DEST}/sync-models.py",
    f"{REPO}/gateway/manage-providers.py": f"{DEST}/manage-providers.py",
    f"{REPO}/gateway/start-gateway.sh": f"{DEST}/start-gateway.sh",
}

TIMEOUT = 25


def die(msg: str, code: int = 1):
    print(f"  error: {msg}", file=sys.stderr)
    sys.exit(code)


def ok(msg: str):
    print(f"  {msg}")


def norm_base(base: str) -> str:
    """Provider base must end in exactly one /v1."""
    b = base.strip().rstrip("/")
    return b if b.endswith("/v1") else f"{b}/v1"


def read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        die(f"cannot read {path}: {e}")


def write(path: str, body: str):
    with open(path, "w") as f:
        f.write(body)


def backup(paths: list[str], stamp: str) -> list[str]:
    made = []
    for p in paths:
        if os.path.exists(p):
            dst = f"{p}.bak-{stamp}"
            shutil.copy2(p, dst)
            made.append(dst)
    return made


def deploy() -> list[str]:
    """Copy repo -> ~/.config/claude-gateway/ so the installed copies match."""
    pushed = []
    for src, dst in DEPLOYED.items():
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
            pushed.append(dst)
    return pushed


# ---------------------------------------------------------------- validation

def probe_catalog(base: str, key: str) -> tuple[list[str], str]:
    """GET {base}/models. Returns (ids, error). Never raises.

    NOTE: a successful catalog fetch does NOT mean the key is valid. above.dev
    serves /v1/models publicly, so a garbage key still returns 9 model names.
    Callers must follow this with probe_completion() -- same reasoning as the
    sync itself: the catalog is not evidence.
    """
    url = f"{norm_base(base)}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code} from {url}"
    except urllib.error.URLError as e:
        return [], f"cannot reach {url}: {e.reason}"
    except Exception as e:
        return [], f"unexpected error from {url}: {e}"

    arr = payload.get("data", payload)
    if not isinstance(arr, list):
        return [], f"{url} returned no model array"
    ids = [m.get("id") for m in arr if isinstance(m, dict) and m.get("id")]
    return ids, "" if ids else f"{url} returned an empty catalog"


def probe_completion(base: str, key: str, mid: str) -> str:
    """Real 1-token chat completion. Returns "" on HTTP 200, else an error string.

    This is the only trustworthy check that a credential works. Catalog access
    is frequently unauthenticated, so a key that lists models may still be
    rejected at inference time (above.dev: lists 9, completes on 2).
    """
    url = f"{norm_base(base)}/chat/completions"
    body = json.dumps({
        "model": mid,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return "" if r.status == 200 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return (f"HTTP {e.code} -- the key was rejected by the API. "
                    f"(the catalog endpoint answered anyway; it is public on some providers)")
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return f"inference endpoint unreachable: {e.reason}"
    except Exception as e:
        return f"inference probe failed: {e}"


def first_live_model(base: str, key: str, ids: list[str], tries: int = 4) -> tuple[str, str]:
    """Return (model_id, "") for the first model that completes, else ("", error).

    Tries a few ids because a provider's catalog routinely leads with models
    that 404 -- above.dev lists `deepseek-v4-pro` first, which does not exist,
    while the two that do work sit further down the list.
    """
    last = "no candidate attempted"
    for mid in ids[:tries]:
        err = probe_completion(base, key, mid)
        if not err:
            return mid, ""
        last = f"{mid}: {err}"
    return "", f"none of the first {min(tries, len(ids))} catalog models completed ({last})"


def valid_prefix(p: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", p):
        die("prefix must be lowercase letters/digits/-/_ (it becomes a model-id segment)")
    return p


def valid_env(e: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", e):
        die("env var must be UPPERCASE (e.g. NEWCO_API_KEY)")
    return e


# ------------------------------------------------------------------ mutators

def add_to_python(prefix: str, base: str, env: str) -> str:
    src = read(PY)
    if re.search(rf'\(\s*"{re.escape(prefix)}"\s*,', src):
        die(f"provider '{prefix}' is already in {PY}")
    line = f'    ("{prefix}", "{base}", "{env}"),\n'
    m = re.search(r"^PROVIDERS = \[.*?^\]", src, re.S | re.M)
    if not m:
        die(f"could not locate the PROVIDERS list in {PY}")
    block = m.group(0)
    write(PY, src.replace(block, block[:-1] + line + "]"))
    return line


def add_to_cred(prefix: str, key: str) -> None:
    try:
        cred = json.load(open(CRED))
    except (OSError, ValueError) as e:
        die(f"cannot parse {CRED}: {e}")
    cred[prefix] = {"type": "api", "key": key}
    with open(CRED, "w") as f:
        json.dump(cred, f, indent=2)
        f.write("\n")
    os.chmod(CRED, 0o600)


def add_to_start(prefix: str, env: str) -> str:
    src = read(START)
    if f'"{prefix}"' in src and f"export {env}=" in src:
        die(f"provider '{prefix}' is already wired in {START}")
    line = f'export {env}="$(readkey {prefix})"'
    if "exec " not in src:
        die(f"could not find the exec line in {START}")
    head, _, tail = src.partition("\nexec ")
    write(START, f"{head}\n{line}\n\nexec {tail}")
    return line


def remove_everywhere(prefix: str) -> list[str]:
    """Strip a provider from all three sites. Returns human-readable notes."""
    notes = []
    for path, pat in (
        (PY, rf'^\s*\(\s*"{re.escape(prefix)}"\s*,.*\n'),
        (START, rf'^export [A-Z0-9_]+="\$\(readkey {re.escape(prefix)}\)"\n'),
    ):
        src = read(path)
        new = re.sub(pat, "", src, flags=re.M)
        if new != src:
            write(path, new)
            notes.append(path)
    try:
        cred = json.load(open(CRED))
    except (OSError, ValueError):
        cred = {}
    if prefix in provider_entries(cred):
        cred.pop(prefix)
        with open(CRED, "w") as f:
            json.dump(cred, f, indent=2)
            f.write("\n")
        os.chmod(CRED, 0o600)
        notes.append(CRED)
    return notes


# ------------------------------------------------------------------ commands

def current_providers() -> list[str]:
    try:
        return list(provider_entries(json.load(open(CRED))))
    except (OSError, ValueError):
        return []


def provider_entries(cred: dict) -> dict:
    """Only the {"type","key"} objects are providers.

    credentials.json also carries scalar settings such as `master_key`, which
    must not be reported as a provider or removed by `remove`.
    """
    return {k: v for k, v in cred.items() if isinstance(v, dict) and "key" in v}


def cmd_list(args) -> int:
    print("\n  providers wired into the gateway\n")
    try:
        cred = provider_entries(json.load(open(CRED)))
    except (OSError, ValueError):
        cred = {}
    for p in cred:
        in_py = f'("{p}",' in read(PY)
        in_start = f"readkey {p}" in read(START)
        mark = lambda b: "ok" if b else "MISSING"  # noqa: E731
        print(f"    {p:10} key:set  python:{mark(in_py):7} start:{mark(in_start)}")
    stale = [p for p in current_providers() if p not in cred]
    print(f"\n  total: {len(cred)}\n")
    if stale:
        print(f"  note: {stale} appear in config but not credentials.json\n")
    print("  models currently live in the gateway:")
    try:
        import urllib.request as u
        req = u.Request("http://127.0.0.1:4000/v1/models",
                        headers={"Authorization": "Bearer sk-claude-gateway-local"})
        with u.urlopen(req, timeout=6) as r:
            n = len(json.loads(r.read())["data"])
        print(f"    {n} advertised\n")
    except Exception:
        print("    gateway not responding -- is claude-gateway.service running?\n")
    return 0


def cmd_add(args) -> int:
    prefix = valid_prefix(args.prefix or input("  prefix (lowercase, e.g. newco): ").strip())
    base_in = args.base or input(f"  base URL (e.g. https://api.newco.com or http://host:11434 for Ollama): ").strip()
    env = valid_env(args.env or input("  env var name (e.g. NEWCO_API_KEY): ").strip())

    is_ollama = ":11434" in base_in
    key = args.key
    if not key:
        if sys.stdin.isatty():
            if is_ollama:
                print("  Note: For Ollama, you can use any API key (e.g., 'ollama').")
                key = getpass.getpass("  API key (input hidden, default: ollama): ").strip()
                if not key:
                    key = "ollama"
            else:
                key = getpass.getpass("  API key (input hidden): ").strip()
        else:
            key = sys.stdin.readline().strip()
            if not key and is_ollama:
                key = "ollama"
    if not key:
        die("no API key supplied")

    base = norm_base(base_in)
    if base != base_in.rstrip("/"):
        ok(f"base URL normalised to {base}")

    print(f"\n  validating {prefix} against {base} ...")
    ids, err = probe_catalog(base, key)
    if err:
        print(f"\n  {prefix} FAILED validation: {err}")
        print("  nothing was changed. fix the key or URL and retry.\n")
        return 2
    ok(f"catalog reachable, {len(ids)} models advertised")
    shown = ", ".join(ids[:4]) + (" ..." if len(ids) > 4 else "")
    ok(f"  e.g. {shown}")

    print("  confirming the key works (real completion, not just catalog access) ...")
    live, err = first_live_model(base, key, ids)
    if not live:
        print(f"\n  {prefix} FAILED validation: {err}")
        print("  nothing was changed. the catalog listed models, but none completed.\n")
        return 2
    ok(f"key works - {live} returned a completion")
    print("\n  note: the gateway uses a static litellm_config.yaml.")
    print("        add your model entry there manually after adding the provider.\n")

    targets = [CRED, START, PY]
    if args.dry_run:
        print("  --dry-run: would do the following, changing nothing\n")
        print(f"    credentials.json   + {prefix!r} key (mode 600)")
        print(f"    start-gateway.sh   + export {env}=\"$(readkey {prefix})\"")
        print(f"    sync-models.py     + ({prefix!r}, {base!r}, {env!r})")
        return 0

    if not args.yes and sys.stdin.isatty():
        if input(f"  add {prefix}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  aborted, nothing changed\n")
            return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    made = backup(targets, stamp)
    ok(f"backed up {len(made)} file(s) as .bak-{stamp}")

    try:
        add_to_cred(prefix, key)
        add_to_start(prefix, env)
        add_to_python(prefix, base, env)
    except SystemExit:
        for b in made:
            shutil.copy2(b, b[: -len(f".bak-{stamp}")])
            os.remove(b)
        die("rolled back -- provider not added")
    ok(f"added {prefix} to credentials.json, start-gateway.sh, sync-models.py")

    for p in deploy():
        ok(f"deployed {p.replace(HOME, '~')}")

    print(f"\n  to undo:     {sys.argv[0]} remove {prefix}")
    print(f"               (or restore the .bak-{stamp} files)\n")
    return 0


def cmd_remove(args) -> int:
    prefix = args.prefix
    cred = {}
    try:
        cred = provider_entries(json.load(open(CRED)))
    except (OSError, ValueError):
        cred = {}
    if prefix not in cred and f'("{prefix}",' not in read(PY):
        die(f"'{prefix}' is not a configured provider")

    if args.dry_run:
        print(f"\n  --dry-run: would remove {prefix} from all three config sites\n")
        return 0
    if not args.yes and sys.stdin.isatty():
        if input(f"  remove {prefix}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  aborted, nothing changed\n")
            return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    targets = [CRED, START, PY]
    backup(targets, stamp)
    notes = remove_everywhere(prefix)
    ok(f"removed {prefix} from: {', '.join(n.replace(HOME, '~') for n in notes) or 'nothing found'}")

    for p in deploy():
        ok(f"deployed {p.replace(HOME, '~')}")

    print(f"\n  re-ran sync: {len(notes)} site(s) updated, gateway will advertise fewer models\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Manage LiteLLM gateway providers across all config sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1],
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="show what would change, touch nothing")
    common.add_argument("--yes", "-y", action="store_true",
                        help="skip the confirmation prompt")
    common.add_argument("--refresh", action="store_true",
                        help="reserved for compatibility")
    for f in ("--dry-run", "--yes", "-y", "--refresh"):
        ap.add_argument(f, action="store_true" if f != "-y" else "store_true",
                        dest={"-y": "yes"}.get(f, f.lstrip("-")),
                        help=argparse.SUPPRESS)

    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", parents=[common], help="show wired providers and config consistency")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("add", parents=[common], help="validate and add a provider")
    p.add_argument("prefix", nargs="?")
    p.add_argument("base", nargs="?")
    p.add_argument("env", nargs="?")
    p.add_argument("key", nargs="?")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove", parents=[common], help="remove a provider everywhere")
    p.add_argument("prefix")
    p.set_defaults(fn=cmd_remove)

    args = ap.parse_args()
    for f in ("dry_run", "yes", "refresh"):
        if not getattr(args, f, False):
            setattr(args, f, getattr(args, f, False))
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    for p in (CRED, START, PY):
        if not os.path.exists(p):
            die(f"expected file missing: {p}")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())