# Security Policy

hublane is a **local MITM relay proxy**. To intercept HTTPS it generates and
installs a self-signed **"hublane Local Relay CA"** into your system/browser
trust store, then presents per-session leaf certificates for the hosts you
visit through the proxy.

## Responsible use

- Only install the generated CA certificate on **machines you own and trust**.
- The CA private key is generated **locally on your machine** at install time
  and is **never** transmitted anywhere. It lives only in the install
  directory (e.g. `/opt/hublane` on Linux, `%LOCALAPPDATA%\hublane` on Windows).
- Do **not** share your generated `ca.key` / `server.key` with anyone.
- Remove the CA from your trust store (and the install directory) when you
  stop using hublane.

## Metrics panel and token

- The metrics panel / `/status` / `/pac` bind to `127.0.0.1` by default, where
  no token is required (the OS loopback boundary is the isolation).
- Binding `metrics_host` to any non-loopback address **requires**
  `metrics_token`; configuration validation refuses to start otherwise.
  When a token is set, pass it as the `X-Hublane-Token` header or `?token=...`.
- The token grants read access to health data (including verified IPs and
  upstream names). It does **not** grant any control over the proxy.
- `/healthz` is intentionally unauthenticated and returns only `ok`.

## Local access

By default hublane authenticates nobody: any process (and any user) that can reach
`127.0.0.1:8899` can relay traffic through it, including through the MITM path.
Treat the listening port as a local trust boundary, and note that a Windows service
(`--service`, LocalSystem) makes the proxy reachable to every session on the machine.

Since v0.1.0 you can require credentials:

- `proxy_token` — HTTP clients send `Proxy-Authorization` (`Basic base64(user:token)`),
  failures get `407 Proxy Authentication Required`; SOCKS5 clients must use
  username/password auth (method 0x02, password = token).
  Client写法: `http://hublane:<token>@127.0.0.1:8899`, `curl -x ... --proxy-user hublane:<token>`.
- `proxy_uid_whitelist` (Linux/WSL) — only the listed uids may use the proxy
  (`SO_PEERCRED`); empty list means no restriction.

These are safeguards for shared machines, not a substitute for not exposing the port.

## Private-key handling (for contributors)

- No private key (`server.key`, `ca.key`) or generated certificate
  (`server.crt`, `ca.crt`) is committed to this repository. They are produced
  by the install scripts on the user's machine.
- If you find a committed key/cert, treat it as a leaked secret: report it
  immediately and rotate.
- Key files should be `0600` (and owned by the account that runs the proxy).
  Install scripts set this on POSIX; `hublane.py` also logs a warning at startup
  when the key is group/world readable.

## Reporting a vulnerability

Please report security issues privately using
[GitHub Security Advisories](https://github.com/carsonz/hublane/security/advisories)
for this repository. Do not open public issues for security vulnerabilities.

We aim to acknowledge reports within 72 hours.
