# hublane

English | [简体中文](./README.zh-CN.md)

> **hublane** — a local relay proxy that makes GitHub reliably reachable from WSL and Windows.
> Pure Python standard library. Zero third-party dependencies. One codebase, both platforms.
> Version **0.2.0** — closes the items 0.1.0 documented but never delivered, and adds
> a dynamic verified-IP pool, an `abort` fast-fail upstream, panel controls
> (refresh / pause / sortable columns / URL→command), `--check-update` / `--update`,
> and read-only `GET /hosts`. The version lives in exactly one place: `VERSION` in
> `hublane.py`; the packaging config reads it at build time.
> Relay core (streaming, SOCKS5 + HTTP on one port), adaptive upstream chains,
> the HTML panel with `/status` / `/requests` / `/diag`, the hardening baseline
> and the install scripts ship together. The Rust/Go
> port is deliberately not planned; see [ROADMAP](./ROADMAP.md),
> [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) and
> [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md).

![CI](https://github.com/carsonz/hublane/actions/workflows/ci.yml/badge.svg?branch=main&logo=github&label=CI)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/carsonz/hublane/badge)](https://securityscorecards.dev/viewer/?uri=github.com/carsonz/hublane)

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

The script: deploys to `/opt/hublane` (an **existing `config.json` is preserved**; the new defaults are written to `config.json.new` for you to merge) → config validation → installs the CA into the system trust store (Debian `update-ca-certificates`, automatic fallback to RPM `update-ca-trust`) → writes a systemd unit (`Restart=always`) → injects proxy variables into your shell config (picked from the login shell recorded in passwd: zsh -> `~/.zshenv`, bash -> `~/.bashrc`; `.zshenv` rather than `.zshrc` because zsh reads it for every shell, not just interactive ones) → verifies the three target commands.

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
curl http://127.0.0.1:28898/hosts     # verified IPs (hosts format, read-only; hublane never writes /etc/hosts)
curl http://127.0.0.1:28898/healthz   # liveness probe (the only endpoint that ignores the token)
curl -X POST http://127.0.0.1:28898/refresh  # re-verify IPs + probe now (after switching networks)
curl -X POST http://127.0.0.1:28898/pause    # stop intercepting (service keeps running, all traffic tunnels through)
curl -X POST http://127.0.0.1:28898/resume   # resume intercepting
python hublane.py --check-update      # check for a newer version only (downloads nothing)
python hublane.py --update            # fetch latest release (backs up first, rolls back if --check fails)
curl -X POST http://127.0.0.1:28898/reload   # hot-reload config (POSIX: kill -HUP also works)
sudo journalctl -u hublane -f        # logs (WSL)
python hublane.py --check            # config validation
python hublane.py --renew-certs      # renew the leaf cert (keeps the CA); --renew-ca renews both
python -m unittest discover -s tests # unit + integration tests (165 cases, no internet needed)

# Windows only: build a standalone hublane.exe (no Python needed on the target)
pwsh -File tools/setup-windows-env.ps1    # winget: Python + OpenSSL, creates .venv
python tools/build-exe.py                 # -> dist/hublane.exe (--one-dir lowers AV false positives)

# Windows admin-grade end-to-end check (task mode + service mode, self-cleaning)
pwsh -Command "Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile','-File','tools\verify-windows.ps1' -Wait"
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
| `github_upstreams` | default `["ghproxy_com_gh", "ghfast_gh", "ghproxy_net_gh", "direct", "watt"]` (mirrors must precede `direct`, see below) |
| `per_host_upstreams` | per-domain chains, wildcards supported: `{"github.com": ["watt","direct"], "*.example.com": ["chain"]}`; mentioning a host here also marks it as managed |
| `extra_hosts` / `extra_upstreams` | additional managed sites and their chain |
| `custom_mirrors` | custom mirror templates `{"name": "https://host/{path}"}` |
| `preset_ips` | preset IP candidates `{"host": ["1.2.3.4"]}`; merged with DoH results and previously verified IPs, then **all re-verified** — stale ones drop out when you change networks |
| `abort` (upstream name) | pseudo-upstream: fail fast — no connection is made and no timeout is spent, for domains that are blocked with no substitute |
| `verbose` | force DEBUG-level logging when `true` (default `false`; `log_level` is the day-to-day control) |
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
| `panel_scroll_rows` | how many rows the panel's "recent requests" and "verified IPs" blocks show (fixed height + scroll; 5-40, default 16) |
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
- **The proxy itself is unauthenticated by default**: any process on the machine can relay through it. On multi-user machines (or when running as a LocalSystem service) set `proxy_token` — HTTP uses `Proxy-Authorization`, SOCKS5 uses username/password (password = token); on Linux you can also restrict callers with `proxy_uid_whitelist`. Three spellings are accepted: `Basic base64(user:token)` (standard, e.g. `curl -U any:token`), `Basic base64(token)` (username omitted), and `Bearer token`. Certificates start warning 90 days before expiry; renew with `--renew-certs`.
- raw content is fetched through third-party mirrors; the default first choice is `gh-proxy.com` (fetches live, unmodified content).
  For higher trust, set `raw_upstreams` to `["jsdelivr_fastly"]` (mainstream CDN, but has cache lag and file-size limits).
- **Requests that carry credentials are never sent to third-party mirrors.** `Authorization` is an end-to-end header, so relaying it to a public mirror would hand your token to whoever operates that mirror. hublane therefore detects an `Authorization` header (or a `git-receive-pack` write, i.e. `git push`) and restricts such requests to upstreams you control (`direct` / `watt` / `chain`). Consequence: **`git push` is not relayed** — use SSH (`git remote set-url origin git@github.com:OWNER/REPO.git`) or bypass the proxy for that one command (`git -c http.proxy= -c https.proxy= push`). Anonymous `git clone` / `fetch` are unaffected and still benefit from mirrors.
- **`git push` works out of the box.** Restricted networks often hijack `github.com` DNS to `127.0.0.1` — so `ssh` lands on your *own* sshd and is rejected (it looks like a broken key) — or block port 22 outright. The installer's final step detects this and, when needed, points `git@github.com` at GitHub's official `ssh.github.com:443` entry point. You can also run `bash tools/setup-git-ssh.sh` (Windows: `tools/setup-git-ssh.ps1`) on its own. See `docs/TROUBLESHOOTING.md` 7e.
- **If you ever used a GitHub PAT through hublane, revoke and reissue it.** Older versions forwarded it to third-party mirror operators; rotating the token is the only way to retire a credential that has already leaked.
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

## Troubleshooting

Full manual with numbered cases: [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md).

**Windows gotchas confirmed during Windows verification** — every row below was
reproduced on Windows 11, not guessed:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `curl: (60) schannel: the revocation status is unknown` | Windows schannel performs an OCSP revocation check; a locally generated CA has no responder | add `--ssl-no-revoke`, and pass `--cacert %LOCALAPPDATA%\hublane\ca.crt` |
| `SSL: CA cert does not include key usage extension` | Python 3.14 / OpenSSL 3.5 rejects a CA cert without the `keyUsage` extension | `python hublane.py --renew-ca`, then reinstall trust in the root store |
| A `.bat` prints `xxx was unexpected at this time.` | inside an `if (...)` block, an `echo` line mixing non-ASCII text with ASCII `()` breaks cmd's block parser | keep `.bat` files UTF-8, and avoid parentheses in `echo` text |
| `./install.sh: bad interpreter: /usr/bin/env bash^M` (WSL) | the shell script was checked out with CRLF | a `.gitattributes` pins `.sh` to LF and `.bat` to CRLF; re-clone if you have an old checkout |
| Proxy dies a few seconds after logon, nothing in the log | the generated `run-loop.bat` called `py`, which is not on the scheduled task's PATH | fixed — re-run `install-windows.bat`; the absolute interpreter path is now baked in |
| `ERROR: Input redirection is not supported` | `wscript.exe` launched with redirected stdin (CI, agents, SSH) | harmless artifact of non-interactive sessions |
| Process lingers a few seconds after `sc stop` | `concurrent.futures`' atexit hook joins its thread pool, waiting for in-flight DoH requests | by design: the SCM wait hint is 30 s, measured exit is ~10–15 s |
| Switching from task mode to service mode: an unknown `python.exe` holds the port | `run-loop.bat` is a `goto loop` loop, so the old supervisor relaunches python | step `[3/6]` now kills the whole process tree by command line; reinstall once |
| `openssl` not found | Windows ships no system OpenSSL; only Git for Windows bundles one | `pwsh -File tools/setup-windows-env.ps1` installs it via winget |

**Reading the panel counters.** `HTTP requests` counts only requests that hublane
decrypted (MITM). Pure TCP tunnels — non-managed domains, plain `http://`, SSH — are
counted separately as `Tunnel connections` / `Tunnel failures` / `Tunnel bytes`, so
total traffic is visible without corrupting the HTTP numbers. `Upstream failures`
counts *per upstream attempt*: one request that tries three upstreams adds three, so
it can legitimately exceed the request count. Tunnel durations are deliberately kept
out of the latency percentiles, since a tunnel may live for minutes and would skew
P50/P95. Hover any card for its exact definition.

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
