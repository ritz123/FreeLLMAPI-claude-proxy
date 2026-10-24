//! Generates the LiteLLM gateway config from live provider catalogs.
//!
//! # The problem this solves
//!
//! OpenCode's model picker is populated from models.dev, a *public metadata
//! catalog*. It is optimistic by design: it lists what a provider could
//! plausibly offer, not what it actually serves. Measured on this machine:
//!
//! ```text
//! provider   opencode lists   actually usable
//! above              9      0   (all HTTP 403; names never existed)
//! nvidia           103      ~48 (rest decommissioned or unentitled)
//! ```
//!
//! Two distinct failure modes showed up, and they need different fixes:
//!
//! 1. *Phantom entries* -- the catalog advertises names the API rejects. On
//!    above.dev the catalog said `deepseek-v4.1-flash`; the real model is
//!    `deepseek-v4.1-flash-modal`. Different string, so unselectable.
//! 2. *Stale entries* -- Nvidia keeps retired models in `/v1/models` long after
//!    their inference endpoints are decommissioned. The catalog is honest here;
//!    the provider is not.
//!
//! # Why active probing rather than a deny-list
//!
//! The first version of this used a hand-maintained blocklist. That approach
//! cannot work against failure mode 2: Nvidia adds and retires models
//! continuously, so any static list is wrong within days. Asking the question
//! directly -- "does this model return 200 right now?" -- is the only
//! self-maintaining option, so that is what this does. Every candidate gets a
//! real 1-token chat completion. Only HTTP 200 reaches the config.
//!
//! # Why probing is affordable
//!
//! Probing 63 models is slow (a cold sweep takes 10-25 minutes, dominated by
//! Nvidia cold starts of 40-115s), so verdicts are cached in
//! `probe-cache.json` for `PROBE_TTL`. A routine run reuses everything and
//! finishes in ~1.4s; only models the catalog has newly surfaced touch the
//! network. See the `PROBE_TTL` comment for why the TTL is 36h and not 24h.
//!
//! # Structure
//!
//! ```text
//! 1. keys + catalog   read credentials, GET {base}/models per provider
//! 2. probe            cache-aware sweep, then a rescue pass for transients
//! 3. emit config      rank survivors, write litellm_config.yaml, restart
//! ```
//!
//! Every stage is fail-safe: if the catalog fetch fails it falls back to the
//! cached catalog, and if *no* model survives probing it aborts without
//! touching the config. A bad network day cannot produce an empty gateway.
//!
//! # Output shape
//!
//! Emits N live models plus a `claude-gw-auto` alias and a quality-ordered
//! `fallbacks` chain, so a failing hop degrades to the next model instead of
//! erroring out. Model ids are prefixed `claude-gw-` because Claude Code's
//! gateway discovery filters to ids containing "claude"/"anthropic"; without
//! the prefix every model would be invisible in `/model`.
//!
//! # Usage
//!
//! ```text
//! claude-gateway-sync [--dry-run] [--refresh] [--no-restart]
//! ```
//!
//! - `--dry-run`    print what would be written, change nothing
//! - `--refresh`    ignore cached verdicts and re-probe everything
//! - `--no-restart` write the config but leave the gateway running
//!
//! # Known limitations
//!
//! - The per-provider catalog fetch at the `PROVIDERS` loop is sequential
//!   (`for p in PROVIDERS`). Probing is fully concurrent; catalog fetch is not.
//! - The fallback chain includes the top-ranked model, which is also what
//!     `claude-gw-auto` already points at, so the chain's first hop is a no-op
//!     re-attempt. Harmless (LiteLLM moves on), but it should be skipped.
//!
//! # Related files
//!
//! - `../README.md`                    design notes and operational runbook
//! - `sync-models.py`                  Python original, kept as a fallback
//! - `manage-providers.py`             add/remove providers across all 4 sites
//! - `start-gateway.sh`                injects credentials as env vars for LiteLLM
//!
//! To add a provider, use `manage-providers.py add` rather than editing the
//! `PROVIDERS` table here by hand: it also updates `sync-models.py`,
//! `credentials.json` and `start-gateway.sh`, validates the credential with a
//! real completion first, and rebuilds. Editing `PROVIDERS` alone would leave
//! the Rust and Python implementations emitting different configs.

use std::collections::HashMap;
use std::fs;
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use regex::Regex;
use serde::{Deserialize, Serialize};
use tokio::sync::Semaphore;

/// Gateway-owned credentials. Deliberately NOT opencode's auth.json: keeping
/// above/nvidia out of that file stops OpenCode listing them as direct
/// providers, where the models.dev catalog advertises model names above.dev
/// does not actually serve. Falls back to auth.json if this file goes missing.
const CRED: &str = "/home/biplab/.config/claude-gateway/credentials.json";
const AUTH_FALLBACK: &str = "/home/biplab/.local/share/opencode/auth.json";
const OUT: &str = "/home/biplab/.config/claude-gateway/litellm_config.yaml";
const CATALOG_CACHE: &str = "/home/biplab/.config/claude-gateway/catalog-cache.json";
const PROBE_CACHE: &str = "/home/biplab/.config/claude-gateway/probe-cache.json";
const AUTO: &str = "claude-gw-auto";

/// Probe verdicts older than this are re-probed. Nvidia cold starts run
/// 40-115s, so a cache hit keeps the nightly sync to a few milliseconds.
///
/// This MUST stay comfortably above the timer interval (daily). If TTL equals
/// the interval, every run lands exactly on the boundary -- `86400 < 86400` is
/// false -- and the cache expires nightly, forcing a full 63-model re-probe
/// every single day. 36h leaves 12h of slack for timer drift and clock jumps,
/// so a daily run always hits cache and full revalidation happens every 2 days.
const PROBE_TTL: i64 = 36 * 3600;

const PROBE_TIMEOUT: Duration = Duration::from_secs(30);
const PROBE_WORKERS: usize = 4;
const PROBE_RETRIES: u32 = 2;

/// Cold starts and 503s get a second pass with a much longer timeout.
const RESCUE_TIMEOUT: Duration = Duration::from_secs(150);
const RESCUE_WORKERS: usize = 2;

struct Provider {
    prefix: &'static str,
    base: &'static str,
    env: &'static str,
}

const PROVIDERS: &[Provider] = &[
    Provider { prefix: "above", base: "https://api.above.dev/v1", env: "ABOVE_API_KEY" },
    Provider { prefix: "nvidia", base: "https://integrate.api.nvidia.com/v1", env: "NVIDIA_API_KEY" },
    Provider { prefix: "freellmapi", base: "http://localhost:3002/v1", env: "FREELLMAPI_API_KEY" },
];

/// Quality ranking for the auto-fallback chain: best coding/general first.
/// Unlisted models sort after these so new discoveries stay reachable but
/// never outrank a known-good model.
const RANK: &[&str] = &[
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
];

/// A candidate model: which provider, and its upstream id.
#[derive(Clone)]
struct Cand {
    prefix: &'static str,
    base: &'static str,
    env: &'static str,
    mid: String,
}

impl Cand {
    /// Cache key. Prefixed because the same model id can appear under more
    /// than one provider, and their verdicts are independent.
    fn key(&self) -> String {
        format!("{}:{}", self.prefix, self.mid)
    }
}

/// One probe result, cached to disk. `ts` is what `PROBE_TTL` is measured
/// against; `note` carries the failure reason so a later run can tell a
/// definitive 404 apart from a transient timeout.
#[derive(Serialize, Deserialize, Clone)]
struct Verdict {
    ok: bool,
    #[serde(default)]
    ms: u64,
    #[serde(default)]
    note: String,
    ts: i64,
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// Provider base must end in exactly one /v1.
fn norm_base(base: &str) -> String {
    let b = base.trim_end_matches('/');
    if b.ends_with("/v1") { b.to_string() } else { format!("{b}/v1") }
}

fn load_json<T: for<'de> Deserialize<'de>>(path: &str) -> Option<T> {
    fs::read_to_string(path).ok().and_then(|s| serde_json::from_str(&s).ok())
}

fn save_json_atomic<T: Serialize>(path: &str, value: &T) -> std::io::Result<()> {
    let tmp = format!("{path}.tmp");
    let body = serde_json::to_string_pretty(value).unwrap_or_default();
    fs::write(&tmp, body)?;
    fs::rename(&tmp, path)
}

fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent("claude-gateway-sync/2.0")
        .build()
        .expect("client")
}

/// GET {base}/models. `base` already carries the /v1 suffix.
async fn get_models(
    client: &reqwest::Client,
    base: &str,
    key: &str,
) -> Result<Vec<String>, String> {
    let url = format!("{}/models", norm_base(base));
    let resp = client
        .get(&url)
        .bearer_auth(key)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status().as_u16()));
    }
    let v: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    let arr = match v.get("data") {
        Some(d) => d.clone(),
        None => v,
    };
    Ok(arr
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|m| m.get("id").and_then(|i| i.as_str()).map(str::to_string))
                .collect()
        })
        .unwrap_or_default())
}

/// Fire a real 1-token completion against one model.
async fn probe(
    client: &reqwest::Client,
    base: &str,
    key: &str,
    mid: &str,
    timeout: Duration,
    retries: u32,
) -> (bool, u64, String) {
    let url = format!("{}/chat/completions", norm_base(base));
    let body = serde_json::json!({
        "model": mid,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    });
    for attempt in 0..=retries {
        let started = std::time::Instant::now();
        let sent = client
            .post(&url)
            .bearer_auth(key)
            .json(&body)
            .timeout(timeout)
            .send();
        match sent.await {
            Ok(resp) => {
                let code = resp.status();
                if code.as_u16() == 200 {
                    return (true, started.elapsed().as_millis() as u64, String::new());
                }
                if code.as_u16() == 429 && attempt < retries {
                    tokio::time::sleep(Duration::from_secs(5 * (attempt as u64 + 1))).await;
                    continue;
                }
                return (false, 0, format!("HTTP {}", code.as_u16()));
            }
            Err(e) => {
                if e.is_timeout() {
                    if attempt < retries {
                        tokio::time::sleep(Duration::from_secs(2)).await;
                        continue;
                    }
                    return (false, 0, "TimeoutError".into());
                }
                if attempt < retries {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    continue;
                }
                let kind = if e.is_connect() { "ConnectionError" } else { "RequestError" };
                return (false, 0, kind.into());
            }
        }
    }
    (false, 0, "exhausted".into())
}

/// 404/410 are definitive: the endpoint is gone, no amount of waiting helps.
fn is_definitive(note: &str) -> bool {
    note == "HTTP 404" || note == "HTTP 410"
}

/// Claude Code's gateway discovery only lists ids containing "claude" or
/// "anthropic", so every alias is prefixed to stay visible in /model.
fn slug(prefix: &str, mid: &str) -> String {
    let raw = format!("claude-gw-{prefix}-{mid}").to_lowercase();
    let mut out = String::new();
    let mut prev_dash = false;
    for ch in raw.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
            prev_dash = false;
        } else if !prev_dash {
            out.push('-');
            prev_dash = true;
        }
    }
    let trimmed = out.trim_matches('-');
    if trimmed.len() > 64 { trimmed[..64].to_string() } else { trimmed.to_string() }
}

fn rank_key(mid: &str) -> (u8, usize, String) {
    match RANK.iter().position(|r| *r == mid) {
        Some(i) => (0, i, String::new()),
        None => (1, 0, mid.to_string()),
    }
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    let dry = argv.iter().any(|a| a == "--dry-run");
    let refresh = argv.iter().any(|a| a == "--refresh");
    let no_restart = argv.iter().any(|a| a == "--no-restart");

    let skip_re = Regex::new(
        r"(?i)embed|guard|safety|parse|riva|translate|retriever|detector|clip|reward|calibration",
    )
    .expect("skip regex");

    // ---- 1. keys + catalog ------------------------------------------------
    let (auth, auth_src) = match load_json::<HashMap<String, serde_json::Value>>(CRED) {
        Some(a) => (a, CRED),
        None => match load_json(AUTH_FALLBACK) {
            Some(a) => {
                eprintln!("WARNING: {CRED} unreadable, falling back to {AUTH_FALLBACK}");
                (a, AUTH_FALLBACK)
            }
            None => {
                eprintln!("ABORT: cannot read {CRED} or {AUTH_FALLBACK}");
                return std::process::ExitCode::FAILURE;
            }
        },
    };
    println!("credentials: {auth_src}");

    // Gateway master key. Read from credentials.json rather than hardcoded so
    // no secret lives in version control. Non-dict entries (this one) sit
    // alongside the per-provider {"key": ...} objects by design.
    let master_key = auth
        .get("master_key")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    if master_key.is_empty() {
        eprintln!("ABORT: 'master_key' missing from {auth_src}");
        return std::process::ExitCode::FAILURE;
    }

    let key_of = |p: &str| -> String {
        auth.get(p)
            .and_then(|v| v.get("key"))
            .and_then(|k| k.as_str())
            .unwrap_or_default()
            .to_string()
    };

    let client = build_client();
    let mut catalog_cache: HashMap<String, Vec<String>> = load_json(CATALOG_CACHE).unwrap_or_default();
    let mut candidates: Vec<Cand> = Vec::new();

    for p in PROVIDERS {
        let key = key_of(p.prefix);
        let ids = match get_models(&client, p.base, &key).await {
            Ok(ids) => {
                println!("{}: catalog ok, {} models", p.prefix, ids.len());
                let mut sorted = ids.clone();
                sorted.sort();
                catalog_cache.insert(p.prefix.to_string(), sorted);
                ids
            }
            Err(e) => {
                let cached = catalog_cache.get(p.prefix).cloned().unwrap_or_default();
                println!(
                    "{}: catalog FAILED ({e}), cache has {}",
                    p.prefix,
                    cached.len()
                );
                cached
            }
        };
        for mid in ids {
            if skip_re.is_match(&mid) {
                continue;
            }
            candidates.push(Cand {
                prefix: p.prefix,
                base: p.base,
                env: p.env,
                mid,
            });
        }
    }

    if candidates.is_empty() {
        eprintln!("ABORT: no candidates resolved, leaving config untouched");
        return std::process::ExitCode::FAILURE;
    }

    // ---- 2. probe, reusing fresh cache verdicts ---------------------------
    let mut probe_cache: HashMap<String, Verdict> = load_json(PROBE_CACHE).unwrap_or_default();
    let t0 = now();
    let mut results: HashMap<String, Verdict> = HashMap::new();
    let mut stale: Vec<Cand> = Vec::new();

    for c in &candidates {
        match probe_cache.get(&c.key()) {
            Some(v) if !refresh && t0 - v.ts < PROBE_TTL => {
                results.insert(c.key(), v.clone());
            }
            _ => stale.push(c.clone()),
        }
    }
    let reused = candidates.len() - stale.len();
    if reused > 0 {
        println!(
            "probe cache: reusing {reused} results (TTL {}h), probing {}",
            PROBE_TTL / 3600,
            stale.len()
        );
    } else {
        println!("probe cache: empty/expired, probing all {}", stale.len());
    }

    if !stale.is_empty() {
        println!(
            "pass 1: sweeping {} models (workers={PROBE_WORKERS}, timeout={}s)",
            stale.len(),
            PROBE_TIMEOUT.as_secs()
        );
        let sem = arc_new(PROBE_WORKERS);
        let mut set = Vec::new();
        for c in &stale {
            let client = client.clone();
            let key = key_of(c.prefix);
            let (base, mid) = (c.base.to_string(), c.mid.clone());
            let sem = sem.clone();
            set.push(tokio::spawn(async move {
                let _g = sem.acquire_owned().await;
                probe(&client, &base, &key, &mid, PROBE_TIMEOUT, PROBE_RETRIES).await
            }));
        }
        let pass1 = join_all(set).await;

        let (mut alive, mut transient, mut dead) = (Vec::new(), Vec::new(), Vec::new());
        for (c, (ok, ms, note)) in stale.iter().zip(pass1) {
            let v = Verdict { ok, ms, note, ts: t0 };
            results.insert(c.key(), v);
            if ok {
                alive.push(c.clone());
            } else if is_definitive(&results[&c.key()].note) {
                dead.push(c.clone());
            } else {
                transient.push(c.clone());
            }
        }

        if !transient.is_empty() {
            println!(
                "pass 2: re-probing {} transient failures (workers={RESCUE_WORKERS}, timeout={}s)",
                transient.len(),
                RESCUE_TIMEOUT.as_secs()
            );
            let sem = arc_new(RESCUE_WORKERS);
            let mut set = Vec::new();
            for c in &transient {
                let client = client.clone();
                let key = key_of(c.prefix);
                let (base, mid) = (c.base.to_string(), c.mid.clone());
                let sem = sem.clone();
                set.push(tokio::spawn(async move {
                    let _g = sem.acquire_owned().await;
                    probe(&client, &base, &key, &mid, RESCUE_TIMEOUT, 1).await
                }));
            }
            let pass2 = join_all(set).await;
            for (c, (ok, ms, note)) in transient.iter().zip(pass2) {
                let note = note.clone();
                results.insert(c.key(), Verdict { ok, ms, note: note.clone(), ts: t0 });
                if ok {
                    println!("  rescue {}/{}  OK ({}ms)", c.prefix, c.mid, ms);
                    alive.push(c.clone());
                } else {
                    println!("  drop   {}/{}  ({})", c.prefix, c.mid, note);
                    dead.push(c.clone());
                }
            }
        }
        let _ = dead;

        for c in &stale {
            probe_cache.insert(c.key(), results[&c.key()].clone());
        }
        if !dry {
            let _ = save_json_atomic(PROBE_CACHE, &probe_cache);
        }
    }

    let alive: Vec<Cand> = candidates
        .iter()
        .filter(|c| results[&c.key()].ok)
        .cloned()
        .collect();
    let dead: Vec<&Cand> = candidates
        .iter()
        .filter(|c| !results[&c.key()].ok)
        .collect();

    println!("\nalive={}  dead={}", alive.len(), dead.len());
    for c in &dead {
        println!("  drop {}/{}  ({})", c.prefix, c.mid, results[&c.key()].note);
    }

    if alive.is_empty() {
        eprintln!("ABORT: every model failed the probe, leaving config untouched");
        return std::process::ExitCode::FAILURE;
    }

    // ---- 3. emit config ---------------------------------------------------
    let mut alive = alive;
    alive.sort_by(|a, b| rank_key(&a.mid).cmp(&rank_key(&b.mid)));

    let mut chain: Vec<String> = Vec::new();
    let mut entries: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for c in &alive {
        let name = slug(c.prefix, &c.mid);
        if seen.contains(&name) {
            continue;
        }
        seen.push(name.clone());
        chain.push(name.clone());
        entries.push(format!(
            "  - model_name: {name}\n    litellm_params:\n      model: hosted_vllm/{}\n      api_base: {}\n      api_key: os.environ/{}\n",
            c.mid, c.base, c.env
        ));
    }

    let top = &alive[0];
    entries.insert(
        0,
        format!(
            "  # auto = highest-ranked live model, with a quality-ordered fallback chain\n  - model_name: {AUTO}\n    litellm_params:\n      model: hosted_vllm/{}\n      api_base: {}\n      api_key: os.environ/{}\n",
            top.mid, top.base, top.env
        ),
    );

    let mut out = String::new();
    out.push_str(&format!("general_settings:\n  master_key: {master_key}\n"));
    out.push_str("router_settings:\n  routing_strategy: simple-shuffle\n");
    out.push_str(&format!("fallbacks:\n  - {AUTO}:\n"));
    for name in &chain {
        out.push_str(&format!("      - {name}\n"));
    }
    out.push_str("\nmodel_list:\n");
    out.push_str(&entries.join("\n"));
    out.push('\n');

    if dry {
        println!(
            "\n--- DRY RUN, would write {} models + {AUTO} alias + {}-deep fallback chain ---",
            alive.len(),
            chain.len()
        );
        println!("{}", &out[..out.len().min(1200)]);
        return std::process::ExitCode::SUCCESS;
    }

    let _ = save_json_atomic(CATALOG_CACHE, &catalog_cache);
    if let Err(e) = fs::write(OUT, &out) {
        eprintln!("ABORT: cannot write {OUT}: {e}");
        return std::process::ExitCode::FAILURE;
    }
    println!("\nwrote {} models + {AUTO} alias to {OUT}", alive.len());
    println!("auto -> {}  ({}ms)", top.mid, results[&top.key()].ms);
    println!("fallback chain: {} deep", chain.len());

    if !no_restart {
        match Command::new("systemctl")
            .args(["--user", "restart", "claude-gateway.service"])
            .status()
        {
            Ok(s) if s.success() => println!("gateway restarted"),
            Ok(s) => eprintln!("gateway restart failed: {s}"),
            Err(e) => eprintln!("gateway restart error: {e}"),
        }
    }
    std::process::ExitCode::SUCCESS}

fn arc_new(n: usize) -> std::sync::Arc<Semaphore> {
    std::sync::Arc::new(Semaphore::new(n))
}

/// Collect spawned probe results in submission order.
///
/// Despite the name this does NOT drive concurrency -- every task is already
/// running because `tokio::spawn` was called at submission, and the semaphore
/// is what caps how many are in flight. This only awaits the handles and keeps
/// the results positionally aligned with the input slice, which the caller
/// relies on to zip verdicts back onto candidates.
///
/// A panicked task surfaces as a failed verdict rather than unwinding the run.
async fn join_all(
    set: Vec<tokio::task::JoinHandle<(bool, u64, String)>>,
) -> Vec<(bool, u64, String)> {
    let mut out = Vec::with_capacity(set.len());
    for h in set {
        match h.await {
            Ok(v) => out.push(v),
            Err(e) => out.push((false, 0, format!("JoinError: {e}"))),
        }
    }
    out
}
