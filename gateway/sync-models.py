#!/usr/bin/env python3
"""Sync provider catalogs (above.dev, Nvidia) into the LiteLLM gateway config.

Fetches each provider's model catalog, then ACTIVELY PROBES every candidate
with a 1-token chat completion. Only models returning HTTP 200 are written.

WHY PROBING
    OpenCode's picker is populated from models.dev, a public metadata catalog
    that lists what a provider *could* offer. Two failure modes result:
      * phantom entries -- above.dev's catalog says `deepseek-v4.1-flash`, the
        real model is `deepseek-v4.1-flash-modal`; all 9 listed names 403'd
      * stale entries -- Nvidia keeps retired models in /v1/models after the
        inference endpoints are decommissioned
    A hand-maintained deny-list cannot track Nvidia's continuous churn, so the
    only self-maintaining option is to ask whether the model answers right now.

WHY CACHING
    A cold sweep of 63 models takes 10-25 minutes (Nvidia cold starts are
    40-115s), so verdicts are cached in probe-cache.json for PROBE_TTL. A
    routine run finishes in ~1.9s and touches the network only for models the
    catalog has newly surfaced. TTL is 36h rather than 24h deliberately: the
    timer runs daily, and an exact 24h/24h boundary means the cache expires
    every run and forces a full re-probe nightly.

STRUCTURE
    1. keys + catalog  read credentials, GET {base}/models per provider
    2. probe           cache-aware sweep, then a rescue pass for transients
    3. emit config     rank survivors, write litellm_config.yaml, restart

    Fail-safe throughout: a failed catalog fetch falls back to the cached
    catalog, and if nothing survives probing the config is left untouched.

OUTPUT
    * claude-gw-auto -- alias pinned to the highest-ranked live model
    * fallbacks      -- quality-ordered chain, so a failed hop degrades to the
                       next model instead of erroring out

    Ids are prefixed `claude-gw-` because Claude Code's gateway discovery
    filters to ids containing "claude"/"anthropic"; unprefixed ids would be
    invisible in /model.

USAGE
    python3 sync-models.py [--dry-run]

NOTES
    This is the original implementation, kept as a fallback for machines where
    the Rust binary cannot be built. The two are verified to produce
    byte-identical output -- keep them in sync when changing behaviour.

    Do NOT add a provider by editing PROVIDERS below on its own: a provider
    must appear here AND in src/main.rs AND in credentials.json AND in
    start-gateway.sh. Use manage-providers.py, which handles all four and
    rebuilds.

    This file is tracked in the claude-gateway-sync repo; the copy in
    ~/.config/claude-gateway/ is a symlink to it, so edits here are the real
    ones. See README.md for full design notes.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Gateway-owned credentials. Deliberately NOT opencode's auth.json: keeping
# above/nvidia out of that file stops OpenCode listing them as direct providers,
# where the models.dev catalog advertises names above.dev does not serve.
CRED = "/home/biplab/.config/claude-gateway/credentials.json"
AUTH_FALLBACK = "/home/biplab/.local/share/opencode/auth.json"
OUT = "/home/biplab/.config/claude-gateway/litellm_config.yaml"
CACHE = "/home/biplab/.config/claude-gateway/catalog-cache.json"
PROBE_CACHE = "/home/biplab/.config/claude-gateway/probe-cache.json"
MASTER_KEY = "sk-claude-gateway-local"
AUTO = "claude-gw-auto"

# Probe results older than this are re-probed. Probing is slow (Nvidia cold
# starts run 40-115s), so a cache hit keeps routine syncs to a few seconds.
#
# This MUST stay comfortably above the timer interval (daily). If TTL equals
# the interval, every run lands exactly on the boundary -- 86400 < 86400 is
# False -- and the cache expires nightly, forcing a full 63-model re-probe
# every single day. 36h leaves 12h of slack for timer drift and clock jumps,
# so a daily run always hits cache and full revalidation happens every 2 days.
PROBE_TTL = 36 * 3600

# Skip non-chat model families (embedders, guards, translators, ...).
SKIP_RE = re.compile(
    r"embed|guard|safety|parse|riva|translate|retriever|detector|clip|reward|calibration",
    re.I,
)

PROVIDERS = [
    ("above", "https://api.above.dev/v1", "ABOVE_API_KEY"),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
]

# Quality ranking for the auto-fallback chain: best coding/general model first.
# Unlisted models sort after these, alphabetically, so newly discovered models
# are still reachable but never outrank a known-good one.
RANK = [
    "moonshotai/kimi-k3",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-super-120b-a12b",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "glm-5.3-flash-modal",
    "nvidia/nemotron-3-5-lightning-30b-a3b",
    "poolside/laguna-xs-2.1",
    "mistralai/codestral-22b-instruct-v0.1",
    "deepseek-ai/deepseek-v4.1-flash",
    "moonshotai/kimi-k2.6",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/nemotron-nano-3-30b-a3b",
    "meta/llama-3.3-70b-instruct",
    "mistralai/mistral-nemotron",
    "meta/muse-glimmer-30b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
]

PROBE_TIMEOUT = 30
PROBE_WORKERS = 4
PROBE_RETRIES = 2

# Transient failures (cold start, 503, rate limit) get a second pass with a
# much longer timeout. Nvidia spins models up on first request, so a 30s
# timeout in the parallel sweep produces false negatives.
RESCUE_TIMEOUT = 150
RESCUE_WORKERS = 2


def load_probe_cache():
    try:
        with open(PROBE_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_probe_cache(pc):
    tmp = PROBE_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pc, f, indent=1, sort_keys=True)
    os.replace(tmp, PROBE_CACHE)


def _norm_base(base):
    """Provider base must end in exactly one /v1."""
    return base.rstrip("/") if base.rstrip("/").endswith("/v1") else base.rstrip("/") + "/v1"


def get_models(base, key):
    """GET {base}/models -- base already carries the /v1 suffix."""
    url = _norm_base(base) + "/models"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    data = d.get("data", d)
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def probe(base, key, mid, timeout=PROBE_TIMEOUT, retries=PROBE_RETRIES):
    """Fire a real 1-token completion. Returns (ok, ms, note)."""
    body = json.dumps(
        {
            "model": mid,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode()
    url = _norm_base(base) + "/chat/completions"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    return True, int((time.monotonic() - start) * 1000), ""
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 429 and attempt < retries:
                time.sleep(5 * (attempt + 1))  # free-tier rate limit, back off
                continue
            return False, 0, f"HTTP {code}"
        except Exception as e:  # noqa: BLE001 - timeout/DNS/etc
            if attempt < retries:
                time.sleep(2)
                continue
            return False, 0, type(e).__name__
    return False, 0, "exhausted"


# 404/410 are definitive: the endpoint is gone, no amount of waiting helps.
DEFINITIVE = re.compile(r"^HTTP (404|410)$")


def slug(prefix, mid):
    # Claude Code's gateway discovery only lists IDs containing
    # "claude" or "anthropic" (official docs: llm-gateway-protocol).
    # Prefix keeps our models visible in the /model picker.
    s = re.sub(r"[^a-z0-9]+", "-", f"claude-gw-{prefix}-{mid}".lower()).strip("-")
    return s[:64]


def rank_key(prefix, mid):
    """Sort by RANK position; unlisted models go last, then alphabetical."""
    try:
        return (0, RANK.index(mid))
    except ValueError:
        return (1, mid)


def main():
    dry = "--dry-run" in sys.argv
    try:
        auth = json.load(open(CRED))
        print(f"credentials: {CRED}")
    except (OSError, ValueError):
        auth = json.load(open(AUTH_FALLBACK))
        print(f"WARNING: {CRED} unreadable, falling back to {AUTH_FALLBACK}",
              file=sys.stderr)
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}

    # ---- 1. discover candidates -------------------------------------------
    candidates = []  # (prefix, base, env, mid)
    for prefix, base, env in PROVIDERS:
        key = auth[prefix]["key"]
        try:
            ids = get_models(base, key)
            cache[prefix] = sorted(ids)
            print(f"{prefix}: catalog ok, {len(ids)} models")
        except Exception as e:  # noqa: BLE001 - fall back to cache
            ids = cache.get(prefix, [])
            print(f"{prefix}: catalog FAILED ({type(e).__name__}: {e}), cache has {len(ids)}")
        for mid in sorted(ids):
            if SKIP_RE.search(mid):
                continue
            candidates.append((prefix, base, env, mid))

    if not candidates:
        print("ABORT: no candidates resolved, leaving config untouched")
        return 1

    # ---- 2. actively probe every candidate --------------------------------
    # Fresh cache entries are reused verbatim; only models we have not seen
    # within PROBE_TTL touch the network. On a routine nightly run that is
    # zero probes, so the whole sync finishes in about a second.
    keys = {(p, b): auth[p]["key"] for p, b, _ in PROVIDERS}
    now = int(time.time())
    refresh = "--refresh" in sys.argv
    pc = load_probe_cache()

    def do_probe(cand, timeout, retries):
        return probe(cand[1], keys[(cand[0], cand[1])], cand[3], timeout, retries)

    results, stale = {}, []
    for cand in candidates:
        ck = f"{cand[0]}:{cand[3]}"
        hit = pc.get(ck)
        if not refresh and hit and now - hit.get("ts", 0) < PROBE_TTL:
            results[ck] = (hit["ok"], hit.get("ms", 0), hit.get("note", ""))
        else:
            stale.append(cand)

    reused = len(candidates) - len(stale)
    if reused:
        print(f"probe cache: reusing {reused} results "
              f"(TTL {PROBE_TTL // 3600}h), probing {len(stale)}")
    else:
        print(f"probe cache: empty/expired, probing all {len(stale)}")

    if stale:
        print(f"pass 1: sweeping {len(stale)} models "
              f"(workers={PROBE_WORKERS}, timeout={PROBE_TIMEOUT}s)...")
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
            pass1 = list(
                ex.map(lambda c: do_probe(c, PROBE_TIMEOUT, PROBE_RETRIES), stale)
            )

        alive = []
        transient, dead = [], []
        for cand, (ok, ms, note) in zip(stale, pass1):
            results[f"{cand[0]}:{cand[3]}"] = (ok, ms, note)
            if ok:
                alive.append(cand)
            elif DEFINITIVE.match(note):
                dead.append(cand)
            else:
                transient.append(cand)

        if transient:
            print(f"pass 2: re-probing {len(transient)} transient failures "
                  f"(workers={RESCUE_WORKERS}, timeout={RESCUE_TIMEOUT}s)...")
            with ThreadPoolExecutor(max_workers=RESCUE_WORKERS) as ex:
                pass2 = list(
                    ex.map(lambda c: do_probe(c, RESCUE_TIMEOUT, 1), transient)
                )
            for cand, (ok, ms, note) in zip(transient, pass2):
                results[f"{cand[0]}:{cand[3]}"] = (ok, ms, note)
                if ok:
                    alive.append(cand)
                    print(f"  rescue {cand[0]}/{cand[3]}  OK ({ms}ms)")
                else:
                    dead.append(cand)
                    print(f"  drop   {cand[0]}/{cand[3]}  ({note})")

        # Persist probe verdicts so the next run can skip the network.
        for cand in stale:
            ok, ms, note = results[f"{cand[0]}:{cand[3]}"]
            pc[f"{cand[0]}:{cand[3]}"] = {"ok": ok, "ms": ms, "note": note, "ts": now}
        if not dry:
            save_probe_cache(pc)

    alive = [c for c in candidates if results[f"{c[0]}:{c[3]}"][0]]
    dead = [c for c in candidates if not results[f"{c[0]}:{c[3]}"][0]]

    print(f"\nalive={len(alive)}  dead={len(dead)}")
    for cand in dead:
        print(f"  drop {cand[0]}/{cand[3]}  ({results[f'{cand[0]}:{cand[3]}'][2]})")

    if not alive:
        print("ABORT: every model failed the probe, leaving config untouched")
        return 1

    # ---- 3. emit config ----------------------------------------------------
    alive.sort(key=lambda c: rank_key(c[0], c[3]))
    entries, chain, seen = [], [], set()
    for prefix, base, env, mid in alive:
        name = slug(prefix, mid)
        if name in seen:
            continue
        seen.add(name)
        if name != AUTO:
            chain.append(name)
        entries.append(
            f"  - model_name: {name}\n"
            f"    litellm_params:\n"
            f"      model: hosted_vllm/{mid}\n"
            f"      api_base: {base}\n"
            f"      api_key: os.environ/{env}\n"
        )

    # claude-gw-auto is an alias pinned to the top-ranked live model; the
    # fallbacks chain carries it to the rest in quality order.
    top_prefix, top_base, top_env, top_mid = alive[0]
    entries.insert(
        0,
        f"  # auto = highest-ranked live model, with a quality-ordered fallback chain\n"
        f"  - model_name: {AUTO}\n"
        f"    litellm_params:\n"
        f"      model: hosted_vllm/{top_mid}\n"
        f"      api_base: {top_base}\n"
        f"      api_key: os.environ/{top_env}\n",
    )

    out = ["general_settings:\n  master_key: " + MASTER_KEY + "\n"]
    out.append("router_settings:\n  routing_strategy: simple-shuffle\n")
    out.append("fallbacks:\n  - " + AUTO + ":\n")
    for name in chain:
        out.append(f"      - {name}\n")
    out.append("\nmodel_list:\n")
    out.append("\n".join(entries) + "\n")
    text = "".join(out)

    if dry:
        print(f"\n--- DRY RUN, would write {len(alive)} models "
              f"+ {AUTO} alias + {len(chain)}-deep fallback chain ---")
        print(text[:1200])
        return 0

    with open(CACHE, "w") as f:
        json.dump(cache, f)
    with open(OUT, "w") as f:
        f.write(text)
    print(f"\nwrote {len(alive)} models + {AUTO} alias to {OUT}")
    print(f"auto -> {top_mid}  ({results[f'{top_prefix}:{top_mid}'][1]}ms)")
    print(f"fallback chain: {len(chain)} deep")

    subprocess.run(
        ["systemctl", "--user", "restart", "claude-gateway.service"], check=True
    )
    print("gateway restarted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
