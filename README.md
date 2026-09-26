# claude-gateway-sync

Generates `litellm_config.yaml` from **live** provider catalogs, so the LiteLLM
gateway only ever advertises models that actually answer.

Runs nightly via `claude-gateway-sync.timer`, plus on login if a run was missed.

## Installation

Fresh machine, reproducible from the repo:

```bash
git clone <repo> ~/work/claude-gateway-sync
cd ~/work/claude-gateway-sync
./install.sh
```

`install.sh` copies the tracked scripts into `~/.config/claude-gateway/`,
installs the systemd user units into `~/.config/systemd/user/`,
builds the Rust binary, and registers the daily timer.

Secrets are *not* checked into the repo. On first run create
`~/.config/claude-gateway/credentials.json`:

```json
{
  "master_key": "sk-claude-gateway-local",
  "above": { "type": "api", "key": "PASTE-KEY" },
  "nvidia": { "type": "api", "key": "PASTE-KEY" }
}
```

Mode 600. The repo is the source of truth for code; `credentials.json`,
`litellm_config.yaml`, `probe-cache.json` and `catalog-cache.json` are
local, generated, or ignored.

The repo layout:

```
gateway/manage-providers.py    add/remove/list providers
gateway/start-gateway.sh       injects credentials as env vars
gateway/sync-models.py         Python fallback for the sync
src/main.rs                    Rust sync implementation
systemd/*.service/*.timer      user units for gateway + daily sync
install.sh                     deploy repo -> ~/.config
```

All three gateway scripts are tracked. `install.sh` copies them to
`~/.config/claude-gateway/` on install and after every `manage-providers.py`
change. The code lives in the repo; the live copies are deployed
artifacts. Re-run `./install.sh` after a `git pull` to refresh them.

---

## Why this exists

OpenCode's model picker is populated from **models.dev**, a public metadata
catalog. It is optimistic by design — it lists what a provider *could* offer,
not what it serves. Audited on this machine:

| provider  | opencode lists | actually usable |
|-----------|---------------|-----------------|
| `above`   | 9             | **0** — all HTTP 403, names never existed |
| `nvidia`  | 103           | ~48 — rest decommissioned or unentitled |
| `openai`  | 55            | **0** — credential was a FreeLLMAPI key |

Two distinct failure modes, needing different fixes:

1. **Phantom entries.** The catalog advertises names the API rejects. above.dev's
   catalog says `deepseek-v4.1-flash`; the real model is
   `deepseek-v4.1-flash-**modal**`. Different string, so unselectable.
2. **Stale entries.** Nvidia keeps retired models in `/v1/models` long after the
   inference endpoints are decommissioned. The catalog is honest; the provider
   isn't.

Neither is fixable from the OpenCode side — `models.json` is a refetched remote
cache, so hand-edits are overwritten and custom providers never appear there
anyway.

## Why probing, not a deny-list

The first version used a hand-maintained blocklist. It cannot work against
failure mode 2: Nvidia adds and retires models continuously, so any static list
is wrong within days. Asking directly — *does this return 200 right now?* — is
the only self-maintaining option.

So every candidate gets a real **1-token chat completion**. Only HTTP 200
reaches the config. Cost is negligible; the latency is handled by caching.

## Why caching is not optional

A cold sweep of 63 models takes **10–25 minutes**, dominated by Nvidia cold
starts of 40–115s. A nightly timer running that would be unusable.

Verdicts persist in `probe-cache.json`, so a routine run reuses everything and
finishes in **~1.4s**. Only models the catalog newly surfaces touch the network.

### Why `PROBE_TTL` is 36h and not 24h

The timer runs daily, so the tempting choice is a 24h TTL. That is a trap:

```rust
Some(v) if !refresh && t0 - v.ts < PROBE_TTL   // 86400 < 86400 == false
```

With TTL == interval, every run lands exactly on the boundary, the cache always
expires, and the sync re-probes all 63 models **every single day**.

36h leaves 12h of slack for timer drift and clock jumps, so a daily run always
hits cache and full revalidation happens every 2 days. Verified: 24h → hit,
35h → hit, 37h → miss.

## Architecture

```
1. keys + catalog   read credentials, GET {base}/models per provider
2. probe            cache-aware sweep, then a rescue pass for transients
3. emit config      rank survivors, write litellm_config.yaml, restart
```

**Two-tier probing.** Pass 1 is a fast sweep (4 workers, 30s timeout, 2 retries).
Failures are split by `is_definitive`: 404/410 mean the endpoint is gone, so
those are dropped. Everything else — timeouts, 429s, 503s — is transient and gets
a rescue pass (2 workers, 150s timeout). Without this, one cold start would
permanently evict a healthy model.

**Fail-safe at every stage.** A failed catalog fetch falls back to the cached
catalog. If *no* model survives probing, it aborts without touching the config.
A bad network day cannot produce an empty gateway.

**Why the `claude-gw-` prefix.** Claude Code's gateway discovery filters to ids
containing `claude` or `anthropic`. Unprefixed ids would be invisible in `/model`.
`slug()` also collapses runs of punctuation and caps length at 64.

## Usage

First, build (only needed after a `src/` change):

```bash
cargo build --release
```

Run the sync:

```bash
./target/release/claude-gateway-sync --dry-run     # print, change nothing
./target/release/claude-gateway-sync --refresh     # ignore cache, re-probe all
./target/release/claude-gateway-sync --no-restart  # write config, leave gateway up
```

`manage-providers.py` is the preferred operator interface:

```bash
MG=~/.config/claude-gateway/manage-providers.py

$MG list                                    # audit what's wired in
$MG add                                     # interactive, key typed blind
$MG add newco https://api.newco.com NEWCO_API_KEY
$MG remove newco                            # undo, everywhere
$MG add --dry-run newco https://… NEWCO_API_KEY
$MG add --refresh newco https://… NEWCO_API_KEY   # add, then re-probe
```

The scripts live in the repo under `gateway/` and are deployed to
`~/.config/claude-gateway/` by `install.sh`. `manage-providers.py` edits the
repo copies and then redeploys them, so a `git pull` on another machine
followed by `./install.sh` restores the exact same scripts.

## Adding a provider

Providers are defined in four places and must stay in sync:

```
credentials.json    + the key (mode 600)
start-gateway.sh    + export NEWCO_API_KEY="$(readkey newco)"
src/main.rs         + Provider { prefix: "newco", ... }
gateway/sync-models.py      + ("newco", "https://api.newco.com/v1", "NEWCO_API_KEY")
                    → cargo build --release
```

Use `manage-providers.py`. Do **not** hand-edit the files — it's easy to get half-right
and leave Rust and Python emitting different configs. The script edits the repo copies
under `gateway/` and then deploys them to `~/.config/claude-gateway/`. That keeps the
repo authoritative and the live copies identical across machines.

```bash
MG=~/.config/claude-gateway/manage-providers.py

$MG list                                    # audit what's wired in
$MG add                                     # interactive, key typed blind
$MG add newco https://api.newco.com NEWCO_API_KEY
$MG remove newco                            # undo, everywhere
$MG add --dry-run newco https://… NEWCO_API_KEY
$MG add --refresh newco https://… NEWCO_API_KEY   # add, then re-probe
```


### Validation happens before any edit

`add` refuses to write anything until the credential proves itself:

1. `GET {base}/models` — is the endpoint reachable and does it list models?
2. a **real 1-token completion** — does the key actually work?

Step 2 is not redundant. above.dev serves `/v1/models` **publicly**, so a
garbage key still returns 9 model names and a catalog-only check passes. This
is the same principle the sync is built on: the catalog is not evidence. The
script tries up to 4 catalog models, because a provider's list routinely leads
with models that don't exist — above.dev advertises `deepseek-v4-pro` first,
which 403s, while the two working models sit further down.

Rejection leaves all four files byte-identical.

### Safety

- Timestamped `.bak-<stamp>` on every target before the first edit
- Auto-rollback if the Rust build fails after the edits
- Prompts for confirmation; key read via `getpass`, never echoed to history
- Prints the exact undo command
- `--refresh` runs the sync afterwards; without it a new provider is registered
  but has no probe verdicts until the next scheduled run

### Ranking caveat

A new provider's models are not in `RANK`, so they sort **after** every ranked
model. They work and are reachable, but they will not lead `claude-gw-auto`
until added to the `RANK` table in both `main.rs` and `sync-models.py`. Unlisted
models are still reachable — they just never outrank a known-good model.

## Files

| path | role |
|---|---|
| `gateway/manage-providers.py` | add/remove/list providers, deploys to `~/.config` |
| `gateway/start-gateway.sh` | injects credentials as env vars (deployed copy) |
| `gateway/sync-models.py` | Python fallback for the sync (deployed copy) |
| `src/main.rs` | all logic |
| `target/release/claude-gateway-sync` | binary the timer runs |
| `install.sh` | deploy repo -> `~/.config/claude-gateway/` + systemd units |
| `~/.config/claude-gateway/credentials.json` | gateway keys (`above`, `nvidia`, `master_key`) |
| `~/.config/claude-gateway/litellm_config.yaml` | **generated** — do not hand-edit |
| `~/.config/claude-gateway/probe-cache.json` | cached verdicts |

`litellm_config.yaml` and both caches are overwritten on every run. Only
`credentials.json` is stable user data; everything else is tracked in this
repo and deployed by `install.sh`.

## Credentials

Keys live in `~/.config/claude-gateway/credentials.json` (mode 600), **not** in
OpenCode's `auth.json`. That separation is deliberate: while `above`/`nvidia`
were in `auth.json`, OpenCode offered 112 unusable entries. A provider only
becomes selectable when a credential for it exists, so moving the keys out
removes the phantom entries while the gateway keeps working.

`credentials.json` is **not tracked**. It is created by `install.sh` as a
template on first install and never committed. Secrets stay local.

Both `main.rs` and `start-gateway.sh` fall back to `auth.json` if the file goes
missing, so a deletion degrades instead of breaking the gateway.

## Known limitations

- **Catalog fetch is sequential.** The `for p in PROVIDERS` loop awaits each
  provider in turn. Probing is fully concurrent; catalog fetch is not. Two
  providers, so the cost is small, but it is inconsistent with the rest of the
  design.
- **`MASTER_KEY` is read from `credentials.json`**, not hardcoded, so no secret
  lives in version control. A missing value aborts the run before the config is
  touched rather than writing a gateway with an empty key.
- **The fallback chain repeats the top model.** `claude-gw-auto` points at the
  highest-ranked live model, and that model is also the chain's first hop — a
  no-op re-attempt. Harmless (LiteLLM moves on to the next hop), but it should
  be skipped.
- **No change detection.** Every run restarts the gateway, even when the config
  is byte-identical, costing ~20s of downtime.
- **`RANK` is hand-curated.** Unlisted models sort after listed ones, so new
  discoveries stay reachable but never outrank a known-good model. The list
  needs occasional maintenance as the model landscape shifts. A provider added
  via `manage-providers.py` starts unranked.

## Related

`claude-gateway-sync` and `sync-models.py` are verified to produce
**byte-identical** output. The Python version is the fallback if the binary
can't be built on a new machine; keep the two in sync when changing behaviour.
