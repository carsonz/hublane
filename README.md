# hublane

English | [简体中文](./README.zh-CN.md)

> **hublane** — a local relay proxy that makes GitHub reliably reachable from WSL and Windows.
> Pure Python standard library. Zero third-party dependencies. One codebase, both platforms.
> Version **0.1.0** — first release: relay core (streaming, SOCKS5 + HTTP on one port),
> adaptive upstream chains, the HTML panel with `/status` / `/requests` / `/diag`,
> the hardening baseline and the install scripts all ship together. The Rust/Go
> port is deliberately not planned; see [ROADMAP](./ROADMAP.md),
> [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) and
> [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md).

![CI](https://github.com/carsonz/hublane/actions/workflows/ci.yml/badge.svg?branch=main&logo=github&label=CI)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

---

## The problem

On mainland-China networks, GitHub is frequently blocked **at the TCP layer**. Measured on a real machine:

| Target | Direct to real IP | Watt Toolkit |
|---|---|---|
| `github.com` | timeout | works, but **8–19 s** |
| `raw.githubusercontent.com` | **immediate TCP RST** | **completely stuck** (handshake completes, GET is sent, upstream never responds) |

`raw.githubusercontent.com` is where virtually every install script lives:

```bash
curl -fsSL https://opencode.ai/install | bash
# -> 307 redirect to https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/install
```

If raw is unreachable, those commands always fail. **hublane exists to make them work.**

Measured results:

| Case | Result |
|---|---|
| hermes install.sh | 200 / 44 739 B / **1.9 s** |
| github.com | 200 / 577 KB / **1.2 s** |
| baidu (tunnelled) | 200 / 0.8 s |

---

## How it works

Listens on `127.0.0.1:8899` — **HTTP and SOCKS5 share the port** (protocol auto-detected from the first byte) — and routes by hostname:

1. **Managed domains** (GitHub family + verified-working sites) → terminate TLS, fetch via the upstream chain
2. **Everything else** → **plain TCP tunnel, no decryption, no rewriting** — byte-for-byte identical to a direct connection

So baidu, `apt`, and internal services are **completely unaffected**.

### Upstream types

| Upstream | Description |
|---|---|
| `direct` | Multi-path DoH (A + AAAA) → **parallel real TLS handshake verification** (SNI = target hostname + certificate validation) → connect to the fastest IP |
| `watt` | Forward to the Windows-side Watt Toolkit local accelerator (`127.0.0.1:443`) |
| `chain` | CONNECT through your own node proxy (Clash / v2rayN / sing-box) |
| `ghproxy_com` / `ghproxy` / `jsdelivr*` | Public mirrors for `raw.githubusercontent.com` (direct is always RST'd) |

Mirror sites come and go, so the built-in library ships a dozen of them and lets the
health ranking pick the working ones (failures sink to the tail, no manual ordering):

| Family | Upstream names |
|---|---|
| prefix style (`https://host/https://raw.githubusercontent.com{path}`) | `ghproxy_com`, `ghproxy`, `ghproxy_homeboyc`, `mirror_ghproxy`, `ghfast`, `ghp_ci`, `gitdl`, `moeyy`, `llkk`, `akams`, `jiasu`, `mirror7ed`, `wget_la` |
| domain-swap style (replace the raw domain) | `gitmirror` (`raw.gitmirror.com`), `kkgithub` (`raw.kkgithub.com`) |
| CDN style | `jsdelivr`, `jsdelivr_fastly`, `jsdelivr_gcore`, `jsdelivr_cf` |

### Adaptive ranking (EWMA × success rate)

Tracks EWMA latency, consecutive failures and a **20-sample sliding-window success
rate** per upstream, keyed by `domain|upstream`:

- Success → update EWMA, reset the failure counter, record 1 in the window
- Failure → cooldown `min(30 × failures, 300 s)`, record 0 in the window
- Rank score = `EWMA / max(success_rate, 0.5)`, so **slow-but-stable beats
  fast-but-flaky** — a failure also costs a fallback round-trip, so ranking on
  latency alone makes the wrong call
- Below `success_floor` (default 0.5) with enough samples → demoted to a
  "low success rate" tier; too few samples (< 3) counts as "unproven" and sorts
  after proven upstreams
- A background probe runs every 120 s (first round 10 s after start); state is
  persisted to `state.json`, so the best ordering survives restarts

Measured: the default `ghproxy.net` (1.77–2.0 s) is automatically replaced by `gh-proxy.com` (**0.76–0.99 s**) — **roughly 2× faster**.

### Connection pool: liveness probe + transparent retry

Connections to `watt` / `direct` upstreams are reused, but **probed before reuse**:

- plaintext connections are checked with `select` + `MSG_PEEK`;
- TLS connections cannot be peeked, so "readable" counts as "not safely reusable"
  (a healthy idle connection is never readable);
- if a stale connection still slips through (e.g. with probing disabled), the
  request is **retried once on a fresh connection** — invisible to the client
  (see the `pool_retry` counter on the panel).

### Response integrity check

Counters the "connection cut mid-transfer" blocking trick:

- when `Content-Length` is known and the response is below
  `integrity_buffer_max` (1 MiB by default), the body is **fully read and
  verified before a single byte is sent** — on a length mismatch the response is
  treated as truncated and the proxy **falls back to the next upstream**;
- larger responses stay streaming (no throughput regression), but the first
  chunk is pre-read and the byte count is compared at the end; on truncation the
  chunked terminator is **omitted** and the connection is dropped, so the client
  sees a hard failure instead of a silently truncated file;
- the truncating upstream is demoted (failure + cooldown).

### Handling intermittent blocking

Measurements show **DPI active probing**: real `github.com` IPs sometimes complete a handshake in 0.17 s, yet a full GET times out.

So `direct` uses an **opportunistic dash**: try once with a short timeout (6 s), fall back to Watt immediately. After 3 consecutive failures it enters a 600 s cooldown and skips direct entirely.

---

## Install

### WSL (Ubuntu / Debian)

```bash
sudo bash ~/hublane/install.sh && exec zsh
sudo bash ~/hublane/install.sh --dry-run           # print the steps, change nothing
sudo bash ~/hublane/install.sh -y --skip-verify    # non-interactive
```

The script: deploys to `/opt/hublane` → config validation → installs the CA into the system trust store (Debian `update-ca-certificates`, automatic fallback to RPM `update-ca-trust`) → writes a systemd unit (`Restart=always`) → injects proxy variables into your shell config (auto-detects zsh/bash) → verifies the three target commands.

To uninstall: `sudo bash uninstall.sh` (stops the service, removes the CA and the shell
block; add `--purge` to delete `/opt/hublane` too).

### Windows (two autostart modes)

```bat
install-windows.bat
```

The script: locates Python → deploys to `%LOCALAPPDATA%\hublane` → config validation → installs the CA into the current-user root store → creates a **self-healing** scheduled task (restarts 3 s after a crash) → sets the system proxy.

**Need it to survive logoff? Register a real service** (run as Administrator):

```bat
install-windows-service.bat
```

The script: disables the scheduled task above (no double instances) →
`sc create hublane binPath= "pythonw.exe hublane.py --service ..." start= auto` →
`sc failure` for crash auto-restart → `sc start`. Manage it with
`sc query hublane` / `net stop hublane`; `--service` registers a ctypes SCM
control handler, so stop requests are handled gracefully.

To uninstall: `uninstall-windows.bat` (task mode) or
`uninstall-windows-service.bat` (service mode, Administrator required).

> Windows requires Python 3.8+. If missing: `winget install Python.Python.3.12`.
> Browser trust differs: Chrome/Edge use the system store, while **Firefox keeps its
> own** — import `%LOCALAPPDATA%\hublane\ca.crt` there manually (or set
> `security.enterprise_roots.enabled=true`).

---

## Verify

```bash
curl -I https://www.baidu.com                                                              # 200
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
curl -fsSL https://opencode.ai/install | bash
```

## Operations

```bash
curl http://127.0.0.1:28898/          # HTML panel: health + latency percentiles + recent requests
curl http://127.0.0.1:28898/status    # metrics JSON: health, latency, counters, verified IPs, pool
curl http://127.0.0.1:28898/requests  # recent request samples (host/upstream/result/ms/bytes)
curl http://127.0.0.1:28898/diag      # diag bundle: version + config (masked) + status + log tail
curl http://127.0.0.1:28898/pac       # PAC auto-proxy script
curl http://127.0.0.1:28898/healthz   # liveness probe (the only endpoint that ignores the token)
curl -X POST http://127.0.0.1:28898/reload   # hot-reload config (POSIX: kill -HUP also works)
sudo journalctl -u hublane -f        # logs (WSL)
python hublane.py --check            # config validation
python hublane.py --renew-certs      # renew the leaf cert (keeps the CA); --renew-ca renews both
python -m unittest discover -s tests # unit + integration tests (148 cases, no internet needed)
```

The panel, JSON, PAC, samples, diag bundle and reload all accept `metrics_token`
(header `X-Hublane-Token: <token>` or `?token=<token>`). **Editing `config.json` does
not need a restart**: `kill -HUP <pid>` (on Windows use `/reload` or the panel button);
an invalid config is rejected and the previous one kept, while structural changes
(listen address, metrics port) are reported as "restart required".

---

## Configuration

WSL: `/opt/hublane/config.json`; Windows: `%LOCALAPPDATA%\hublane\config.json`.

| Key | Description |
|---|---|
| `raw_upstreams` | raw mirror chain (initial order only; re-ranked by EWMA × success rate) |
| `github_upstreams` | default `["direct", "watt"]` |
| `per_host_upstreams` | per-domain chains, wildcards supported: `{"github.com": ["watt","direct"], "*.example.com": ["chain"]}`; mentioning a host here also marks it as managed |
| `extra_hosts` / `extra_upstreams` | additional managed sites and their chain |
| `custom_mirrors` | custom mirror templates `{"name": "https://host/{path}"}` |
| `chain_port` / `chain_socks_port` | your node proxy port (Clash 7890 / v2rayN 10809) |
| `enable_socks5` / `enable_ipv6` | SOCKS5 inbound / IPv6 |
| `metrics_enabled` / `metrics_port` / `metrics_host` | panel toggle / port / bind address (non-loopback bind requires a token) |
| `metrics_token` | access token for the panel and metrics endpoints (empty = unauthenticated on loopback) |
| `direct_timeout` / `direct_fail_max` / `direct_cooldown` | opportunistic-dash and cooldown tuning |
| `pool_enabled` / `pool_max_idle` / `pool_probe` | connection pool / idle cap / liveness probe before reuse |
| `pool_max_per_key` / `pool_max_total` | connections per `domain\|upstream` (4) / global cap (64, LRU eviction) |
| `client_timeout` / `max_conns` | client idle cap (60 s, slowloris guard) / concurrent connection cap (256, rejects beyond it) |
| `probe_enabled` / `probe_delay` / `probe_interval` / `probe_extra_max` | active probing: toggle / first-round delay / interval (0 = follow `refresh_interval`) / how many extra sites per round |
| `cert_expire_warn_days` | warn at startup when a certificate has fewer days left (90); renew with `--renew-certs` |
| `proxy_token` / `proxy_uid_whitelist` | local access control: proxy password (HTTP `407` / SOCKS5 user+pass) / allowed uid list (Linux) |
| `log_format` / `sample_size` | log format `text` or `json` (single-line structured) / number of recent request samples (0 = off) |
| `extra_host_groups` / `extra_host_groups_enabled` | curated foreign-site groups and their on/off switches, see below |
| `integrity_check` / `integrity_buffer_max` | response integrity check / size limit for buffered verification (1 MiB) |
| `success_window` / `success_min_samples` / `success_floor` | sliding window size / samples needed to count as proven / demotion threshold |
| `doh_endpoints` | DoH endpoint list; entries are URL templates or `{"url","name","enabled"}`; used in configured order, repeatedly failing endpoints sink to the end |
| `upstream_list_url` | remote upstream list (disabled by default) |

### Verified additional sites

Measured reachable through Watt and built in: **hcaptcha.com family**, **arkoselabs.com family** (Arkose Labs CAPTCHA), onedrive.live.com, dropbox.com, mega.nz / mega.io, gravatar.com, fonts.googleapis.com, ajax.googleapis.com, vercel.app, github.dev.

**Measured 502, therefore excluded**: Google Translate, huggingface.co, storage.live.com, greasyfork.org.

### Foreign-site groups (opt-in)

Beyond `extra_hosts`, a curated library you can switch on group by group via
`extra_host_groups_enabled`:

| Group | Contents | Default |
|---|---|---|
| `fonts_cdn` | fonts.gstatic.com, unpkg.com, esm.sh, cdnjs, jsdelivr | **on** (completes the built-in fonts.googleapis.com) |
| `pages` | netlify.app / workers.dev / pages.dev / railway.app / fly.dev | off |
| `container` | ghcr.io, Docker Hub, quay.io, gcr.io, registry.k8s.io | off (high bandwidth — prefer `chain`) |
| `toolchain` | nodejs.org, golang.org / go.dev, proxy.golang.org, Rust static assets, crates.io | off |
| `git_hosting` | gitlab.com, bitbucket.org, codeberg.org, sourceforge.net | off |
| `ai_models` | **huggingface.co**, cdn-lfs.huggingface.co, hf.co | off |
| `python` | pypi.org, files.pythonhosted.org | off |
| `npm` | registry.npmjs.org, www.npmjs.com | off |
| `go` | proxy.golang.org, sum.golang.org | off |
| `rust` | index.crates.io, static.crates.io, docs.rs | off |
| `conda` | repo.anaconda.com, conda.anaconda.org | off |

An unknown group name is rejected by `--check`.

### Non-GitHub sites: whole-site mirror swap

Once a group is enabled, **the mirror swap is automatic** (`BUILTIN_PER_HOST_UPSTREAMS`):
the domain gets intercepted → its built-in chain tries mirrors first (path-compatible,
measured — see [docs/MIRRORS.md](./docs/MIRRORS.md)) → falls back to `direct`:

| Site | Mirror (measured working) |
|---|---|
| huggingface.co | `hf-mirror.com` (API / pages / `/resolve/` — identical paths) |
| pypi.org | TUNA `pypi.tuna.tsinghua.edu.cn`, Aliyun `mirrors.aliyun.com/pypi` |
| registry.npmjs.org | `registry.npmmirror.com` |
| proxy.golang.org | `goproxy.cn`, Aliyun `goproxy` |
| repo.anaconda.com / conda.anaconda.org | TUNA `/anaconda` and `/anaconda/cloud` |
| index.crates.io | `rsproxy.cn/index` |
| nodejs.org | `registry.npmmirror.com/-/binary/node` (strips `/dist` automatically) |
| cdn.jsdelivr.net | `fastly.jsdelivr.net` / `jsd.onmicrosoft.cn` |
| fonts.googleapis.com + fonts.gstatic.com | `fonts.googleapis.cn` + `fonts.gstatic.cn` (**must be swapped as a pair**) |

Two caveats: **HuggingFace large files are served from `cdn-lfs.huggingface.co`, whose
paths are not mirror-compatible** (for big models prefer `HF_ENDPOINT=https://hf-mirror.com`
or point that host at `chain`), and **fonts must be swapped in pairs** — otherwise the
CSS still references the blocked gstatic host.

---

## Security notes

- Performs **TLS interception** on managed domains and installs its own CA (`hublane Local Relay CA`). The private key never leaves the machine.
- Startup warns when the key files are group/world readable (POSIX); `chmod 600 server.key ca.key` is recommended.
- The panel binds to `127.0.0.1` by default (no token needed). Binding `metrics_host` to a non-loopback address **requires** `metrics_token`, otherwise config validation refuses to start.
- **The proxy itself is unauthenticated by default**: any process on the machine can relay through it. On multi-user machines (or when running as a LocalSystem service) set `proxy_token` — HTTP uses `Proxy-Authorization`, SOCKS5 uses username/password (password = token); on Linux you can also restrict callers with `proxy_uid_whitelist`. Certificates start warning 90 days before expiry; renew with `--renew-certs`.
- raw content is fetched through third-party mirrors; the default first choice is `gh-proxy.com` (fetches live, unmodified content).
  For higher trust, set `raw_upstreams` to `["jsdelivr_fastly"]` (mainstream CDN, but has cache lag and file-size limits).
- Verify checksums for anything security-sensitive.

## Known limitations

- **Without an overseas exit node, Google / YouTube cannot be reached.** This project ships no "free VPN" trick.
- **Access control is off by default**: without `proxy_token`, any process on the
  machine can use the proxy (listening on `127.0.0.1` is the only boundary). Turn it
  on explicitly for multi-user or service-mode setups.
- The pool keeps at most `pool_max_per_key` connections (4) per `domain|upstream`
  and `pool_max_total` (64) overall, evicting the least recently used.
- High-bandwidth use (container images, large release archives) is throughput-limited
  by a Python relay; point such domains at `chain` (your own node) via
  `per_host_upstreams`.
- Chains other than `raw` (`github_upstreams` / `extra_upstreams`) get no active
  background probing — their health comes from real traffic only, while raw mirrors
  are probed every 120 s.
- Troubleshooting: [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md).

## Legal & responsible use

hublane is a **personal, self-hosted productivity tool** intended to help
developers on restricted networks reach developer resources (e.g. GitHub raw
files, install scripts) they are authorized to access.

- You are responsible for complying with the laws and network policies that
  apply to you.
- Do **not** use it to access resources you are not authorized to access.
- The CA certificate is generated **locally on your machine** at install time
  and is never transmitted anywhere. Only install it on machines you trust and
  control, and remove it when you stop using hublane.
- See [SECURITY.md](./SECURITY.md) for private-key handling and the
  vulnerability-reporting policy.

## License

MIT — see [LICENSE](./LICENSE).
