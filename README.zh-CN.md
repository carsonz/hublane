# hublane

[English](./README.md) | 简体中文

> **hublane** —— 让 WSL 与 Windows 稳定访问 GitHub 的本地中继代理。
> 纯 Python 标准库，零第三方依赖，一份代码同时跑在 WSL 和 Windows。
> 版本 **0.1.1** —— 在 0.1.0 基础上修掉本机（WSL/Ubuntu + Python 3.13）实测暴露的
> 打包、安装与转发问题，并给 github.com 补上镜像链（解决 `git clone/pull` 卡死）。
> 版本号只在一处设置：`hublane.py` 的 `VERSION`，打包配置在构建期读取它。
> Rust/Go 移植明确不做，见 [ROADMAP](./ROADMAP.md) · 架构见 [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) ·
> 排障见 [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md)。

![CI](https://github.com/carsonz/hublane/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

---

## 它解决什么问题

在中国大陆网络环境下，GitHub 经常**在 TCP 层被阻断**。本机实测：

| 目标 | 直连真实 IP | Watt Toolkit |
|---|---|---|
| `github.com` | 超时 | 可通，但 **8~19 秒** |
| `raw.githubusercontent.com` | **TCP 立即 RST** | **完全卡死**（握手完成、请求已发、上游永不响应） |

而 `raw.githubusercontent.com` 是绝大多数安装脚本的来源：

```bash
curl -fsSL https://opencode.ai/install | bash
# -> 307 跳转到 https://raw.githubusercontent.com/anomalyco/opencode/refs/heads/dev/install
```

只要 raw 不通，这类命令必然失败。**hublane 就是为了让这两条命令能跑通而写的。**

本机实测成绩：

| 用例 | 结果 |
|---|---|
| hermes install.sh | 200 / 44739B / **1.9s** |
| github.com | 200 / 577KB / **1.2s** |
| baidu（隧道直通） | 200 / 0.8s |

---

## 工作原理

监听 `127.0.0.1:8899`（HTTP 与 SOCKS5 共用，按首字节自动判别），按域名分流：

1. **受管域名**（GitHub 系 + 已验证可通的站点）→ 终止 TLS，按上游链取回
2. **其它一切域名** → **纯 TCP 隧道直通，不解密、不改写**，与直连完全一致

因此 baidu、`apt`、内网服务**完全不受影响**。

### 上游类型

| 上游 | 说明 |
|---|---|
| `direct` | 多路 DoH 解析（`A` = IPv4 地址记录、`AAAA` = IPv6 地址记录）→ **并行真实 TLS 握手验真**（TLS 是 HTTPS 的加密协议；SNI = 握手时告知证书对应的域名）→ 直连最快 IP |
| `watt` | 转发给 Windows 侧 Watt Toolkit 本地加速器（`127.0.0.1:443`） |
| `chain` | 经你自己的节点代理（Clash / v2rayN / sing-box）用 HTTP `CONNECT` 指令转发 |
| `ghproxy_com` / `ghproxy` / `jsdelivr*` | `raw.githubusercontent.com` 的公共镜像（直连必被 RST，只能走镜像） |

镜像站存活状态变化很快，因此库里内置了十余个常用加速站，运行后由健康度排序自动选出能用的
（失效者自动沉到队尾，无需手工维护顺序）：

| 家族 | 上游名 |
|---|---|
| 前缀式（`https://host/https://raw.githubusercontent.com{path}`） | `ghproxy_com`、`ghproxy`、`ghproxy_homeboyc`、`mirror_ghproxy`、`ghfast`、`ghp_ci`、`gitdl`、`moeyy`、`llkk`、`akams`、`jiasu`、`mirror7ed`、`wget_la` |
| 域名替换式（直接换掉 raw 域名） | `gitmirror`（`raw.gitmirror.com`）、`kkgithub`（`raw.kkgithub.com`） |
| CDN 式 | `jsdelivr`、`jsdelivr_fastly`、`jsdelivr_gcore`、`jsdelivr_cf` |

### 自适应排序（EWMA × 成功率）

按 `域名|上游` 记录：每个上游的 **EWMA 延迟**（Exponentially Weighted Moving Average，
指数加权移动平均 —— 越近的样本权重越大，用来平滑"这次有多快"）、连续失败次数、
**最近 20 次滑动窗口成功率**（滑动窗口 = 只看最近 20 次样本，比历史平均值更贴近当下状态）：

- 成功 → 更新 EWMA，失败计数清零，成功率窗口记 1
- 失败 → 冷却 `min(30 × 失败次数, 300)s`，成功率窗口记 0
- 排序分 = `EWMA / max(成功率, 0.5)`：**"慢但稳" 会胜过 "快但常超时"**
  （失败要多付一次回落代价，纯看延迟会做出错误选择）
- 成功率低于 `success_floor`（默认 0.5）且样本足够 → 直接降到"低成功率"档；
  样本不足（< 3 次）视为"未证明"，排在已证明可用的上游之后
- 后台每 120 秒探测一轮（启动 10 秒先跑一轮）；状态落盘 `state.json`，重启即恢复

实测：初始默认 `ghproxy.net`（1.77~2.0s）会自动切到 `gh-proxy.com`（**0.76~0.99s**），**快约 2 倍**。

### 连接池：探活 + 透明重试

上游（`watt` / `direct`）的连接会被复用，但**复用前先探活**：

- 明文连接用 `select` + `MSG_PEEK` 判断对端是否已关闭；
- TLS 连接无法 PEEK，因此"可读"即视为不可复用（空闲的健康连接必然不可读）；
- 万一还是拿到了被上游单方面关闭的连接（例如探活关闭时），
  **自动用新连接把这一次请求重试一遍**，客户端完全无感（计数见面板 `复用重试`）。

### 响应完整性校验

对付"连接被中途掐断"的封锁手法：

- 已知 `Content-Length` 且不超过 `integrity_buffer_max`（默认 1 MiB）的响应，
  **先整体读入并校验长度**，确认完整才写给客户端 ——
  长度不符就判定为截断，**透明回落到下一个上游**；
- 大响应仍走流式（不牺牲大文件吞吐），但会预读首块并在结束时比对字节数；
  一旦发现截断，**不补 chunked 结束标记**并主动断开，让客户端明确失败，
  而不是把残缺响应伪装成完整响应；
- 截断的上游同时被降级（计入失败 + 冷却）。

### 应对间歇性封锁

实测存在 **DPI 主动探测**：`github.com` 真实 IP 有时握手仅 0.17s，完整 GET 却超时。

因此 `direct` 采用**机会主义快闪**：短超时（6s）抢一次，失败立即回落 Watt；连续失败 3 次后进入 600s 冷却直接跳过直连。

---

## 安装

### WSL（Ubuntu / Debian）

```bash
sudo bash ~/hublane/install.sh && exec zsh
sudo bash ~/hublane/install.sh --dry-run      # 只看将执行的步骤, 不做改动
sudo bash ~/hublane/install.sh -y --skip-verify   # 非交互
```

脚本会：部署到 `/opt/hublane`（**已有的 `config.json` 不会被覆盖**，新版默认配置另存为 `config.json.new` 供你合并）→ 配置校验 → 安装 CA 到系统信任库（Debian 系 `update-ca-certificates`，RPM 系自动回退 `update-ca-trust`）→ 写入 systemd 服务（`Restart=always`）→ 注入代理变量到 shell 配置（按 passwd 里登记的登录 shell 判定：zsh 写 `~/.zshenv`，bash 写 `~/.bashrc`）→ 验证三条目标命令。

卸载：`sudo bash uninstall.sh`（停服务 + 撤 CA + 清 shell 变量；加 `--purge` 连 `/opt/hublane` 一起删）。

### Windows（两种自启模式）

```bat
install-windows.bat
```

脚本会：查找 Python → 部署到 `%LOCALAPPDATA%\hublane` → 配置校验 → 安装 CA 到当前用户根信任库 → 创建**自愈式**计划任务（崩溃 3 秒重启）→ 设置系统代理。

**注销后仍要运行 → 注册成真正的服务**（管理员权限）：

```bat
install-windows-service.bat
```

脚本会：停用上面的计划任务（避免重复实例）→
`sc create hublane binPath= "pythonw.exe hublane.py --service ..." start= auto` →
`sc failure` 配置崩溃自动重启 → `sc start`。之后用
`sc query hublane` / `net stop hublane` 管理，`--service` 模式支持 SCM 优雅停止
（进程内用 ctypes 注册了服务控制处理函数）。

卸载：`uninstall-windows.bat`（计划任务模式）或
`uninstall-windows-service.bat`（服务模式，需管理员）。

> Windows 需要 Python 3.8+。未安装则 `winget install Python.Python.3.12`。
> 浏览器信任差异：Chrome/Edge 走系统信任库；**Firefox 用自己的信任库**，
> 需要在设置里手动导入 `%LOCALAPPDATA%\hublane\ca.crt`（或 `security.enterprise_roots.enabled=true`）。

---

## 验证

```bash
curl -I https://www.baidu.com                                                              # 200
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
curl -fsSL https://opencode.ai/install | bash
```

## 运维入口

```bash
curl http://127.0.0.1:28898/          # HTML 面板：健康度 + 延迟分位数 + 最近请求，5 秒自刷
curl http://127.0.0.1:28898/status    # 指标 JSON：上游/DoH 健康度、延迟、计数器、已验真 IP、连接池
curl http://127.0.0.1:28898/requests  # 最近请求样本（域名/上游/结果/耗时/字节）
curl http://127.0.0.1:28898/diag      # 诊断包：版本+配置(打码)+状态+日志尾部，报障贴这个
curl http://127.0.0.1:28898/pac       # PAC 自动代理脚本
curl http://127.0.0.1:28898/healthz   # 存活探针（唯一不需要 token 的端点）
curl -X POST http://127.0.0.1:28898/reload   # 热重载配置（POSIX 也可 kill -HUP）
sudo journalctl -u hublane -f        # 日志（WSL）
python hublane.py --check            # 配置校验
python hublane.py --renew-certs      # 续期叶证书(保留 CA); --renew-ca 连 CA 一起换
python -m unittest discover -s tests # 单元 + 集成测试（165 项，集成测试不需要外网）

# 仅 Windows：打包成免 Python 的独立 exe
pwsh -File tools/setup-windows-env.ps1    # winget 装 Python + OpenSSL，并建 .venv
python tools/build-exe.py                 # -> dist/hublane.exe（--one-dir 可降低杀软误报）

# Windows 管理员级实测（计划任务模式 + 服务模式全流程，自动清理）
pwsh -Command "Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile','-File','tools\verify-windows.ps1' -Wait"
```

面板/JSON/PAC/样本/诊断包/重载都受 `metrics_token` 保护（请求头 `X-Hublane-Token: <token>`
或 `?token=<token>`）。**改了 `config.json` 不必重启**：`kill -HUP <pid>`（Windows 用
`/reload` 或面板按钮）；新配置非法时会保留旧配置并告警，监听地址/指标端口这类
结构性改动会提示"需重启"。

---

## 配置

WSL：`/opt/hublane/config.json`；Windows：`%LOCALAPPDATA%\hublane\config.json`。

| 键 | 说明 |
|---|---|
| `raw_upstreams` | raw 镜像链（仅为初始顺序，运行后按 EWMA × 成功率自动排序） |
| `github_upstreams` | 默认 `["ghproxy_com_gh", "ghfast_gh", "ghproxy_net_gh", "direct", "watt"]`（镜像必须排在 `direct` 前，见下） |
| `per_host_upstreams` | 按域名的上游链，支持通配：`{"github.com": ["watt","direct"], "*.example.com": ["chain"]}`；出现过的域名自动视为受管域名 |
| `extra_hosts` / `extra_upstreams` | 额外受管站点及其上游链 |
| `custom_mirrors` | 自定义镜像模板 `{"名字": "https://host/{path}"}` |
| `chain_port` / `chain_socks_port` | 你的节点代理端口（Clash 7890 / v2rayN 10809） |
| `enable_socks5` / `enable_ipv6` | SOCKS5 入站 / IPv6 |
| `metrics_enabled` / `metrics_port` / `metrics_host` | 指标面板开关 / 端口 / 绑定地址（绑非本机地址必须设 token） |
| `metrics_token` | 面板与指标端点的访问 token（默认空 = 仅本机可不鉴权） |
| `direct_timeout` / `direct_fail_max` / `direct_cooldown` | 直连的快闪与冷却参数 |
| `pool_enabled` / `pool_max_idle` / `pool_probe` | 上游连接池 / 空闲上限 / 复用前探活 |
| `pool_max_per_key` / `pool_max_total` | 每个 `域名\|上游` 的连接上限（默认 4）/ 全局上限（默认 64，按 LRU 淘汰） |
| `client_timeout` / `max_conns` | 客户端连接空闲上限（默认 60s，防 slowloris —— 故意慢速发请求头、长时间占住连接）/ 并发上限（默认 256，超限直接拒绝） |
| `probe_enabled` / `probe_delay` / `probe_interval` / `probe_extra_max` | 全链路主动探测：开关 / 首轮延迟 / 间隔（0 = 跟随 `refresh_interval`）/ 每轮探测几个额外站点 |
| `cert_expire_warn_days` | 证书剩余天数低于该值时启动告警（默认 90 天），续期用 `--renew-certs` |
| `proxy_token` / `proxy_uid_whitelist` | 本地访问控制：代理口令（HTTP `407` / SOCKS5 用户名密码）/ 允许的 uid 白名单（Linux 上按发起方进程的用户 ID 判定） |
| `log_format` / `sample_size` | 日志格式 `text` 或 `json`（单行结构化）/ 最近请求样本条数（0 = 关闭） |
| `panel_scroll_rows` | 面板里「最近请求」「已校验真实 IP」两块显示多少行（固定高度 + 滚动，可配 5–40，默认 16） |
| `extra_host_groups` / `extra_host_groups_enabled` | 境外站点分组库与整组开关，见下 |
| `integrity_check` / `integrity_buffer_max` | 响应完整性校验开关 / 整体校验的响应大小上限（默认 1 MiB） |
| `success_window` / `success_min_samples` / `success_floor` | 成功率窗口长度 / 判定"已证明"所需样本 / 降级阈值 |
| `doh_endpoints` | DoH 端点列表，元素可为 URL 模板或 `{"url","name","enabled"}`；按配置顺序使用，连续失败者自动排到末尾 |
| `upstream_list_url` | 远程上游清单（默认关闭） |

### 已验证可通的额外站点

经 Watt 实测可通、已内置：**hcaptcha 全系**、**arkoselabs 全系**（Arkose Labs 验证码）、onedrive.live.com、dropbox.com、mega.nz / mega.io、gravatar.com、fonts.googleapis.com、ajax.googleapis.com、vercel.app、github.dev。

**实测 502 故排除**：Google 翻译、huggingface.co、storage.live.com、greasyfork.org。

### 境外站点分组（按需整组启用）

`extra_hosts` 之外的常备库，写入 `extra_host_groups_enabled` 即可整组接管：

| 分组 | 内容 | 默认 |
|---|---|---|
| `fonts_cdn` | fonts.gstatic.com、unpkg.com、esm.sh、cdnjs、jsdelivr | **开**（配合已内置的 fonts.googleapis.com） |
| `pages` | netlify.app / workers.dev / pages.dev / railway.app / fly.dev | 关 |
| `container` | ghcr.io、docker hub、quay.io、gcr.io、registry.k8s.io | 关（大流量建议配自己的节点） |
| `toolchain` | nodejs.org、golang.org / go.dev、proxy.golang.org、rust 静态资源、crates.io | 关 |
| `git_hosting` | gitlab.com、bitbucket.org、codeberg.org、sourceforge.net | 关 |
| `ai_models` | **huggingface.co**、cdn-lfs.huggingface.co、hf.co | 关 |
| `python` | pypi.org、files.pythonhosted.org | 关 |
| `npm` | registry.npmjs.org、www.npmjs.com | 关 |
| `go` | proxy.golang.org、sum.golang.org | 关 |
| `rust` | index.crates.io、static.crates.io、docs.rs | 关 |
| `conda` | repo.anaconda.com、conda.anaconda.org | 关 |

分组里出现未知名字会在 `--check` 时直接报错。

### 非 GitHub 站点：整站换源

开启上面几组后，**换源是自动的**（见 `BUILTIN_PER_HOST_UPSTREAMS`）：域名被接管 →
按内置链先走镜像（路径与官方一致，实测见 [docs/MIRRORS.md](./docs/MIRRORS.md)）→
失败再 `direct`：

| 站点 | 镜像（实测可用） |
|---|---|
| huggingface.co | `hf-mirror.com`（API / 页面 / `/resolve/` 全路径一致） |
| pypi.org | 清华 `pypi.tuna.tsinghua.edu.cn`、阿里 `mirrors.aliyun.com/pypi` |
| registry.npmjs.org | `registry.npmmirror.com` |
| proxy.golang.org | `goproxy.cn`、阿里 `goproxy` |
| repo.anaconda.com / conda.anaconda.org | 清华 `/anaconda` 与 `/anaconda/cloud` |
| index.crates.io | `rsproxy.cn/index` |
| nodejs.org | `registry.npmmirror.com/-/binary/node`（自动剥掉 `/dist`） |
| cdn.jsdelivr.net | `fastly.jsdelivr.net` / `jsd.onmicrosoft.cn` |
| fonts.googleapis.com + fonts.gstatic.com | `fonts.googleapis.cn` + `fonts.gstatic.cn`（**必须成对**） |

注意两点：**HuggingFace 的大模型文件走 `cdn-lfs.huggingface.co`，路径与镜像不兼容**
（下大模型建议 `HF_ENDPOINT=https://hf-mirror.com`，或把该域名指向 `chain`）；
**字体必须 CSS 与字体文件一起换**，否则 CSS 里的 gstatic 链接仍然被墙。

---

## 安全须知

- 对受管域名做 **TLS 中间人**，会生成并安装自有 CA（`hublane Local Relay CA`）。私钥只存在本机。
- 启动时会检查私钥权限，过宽（如 0644）会打警告；建议 `chmod 600 server.key ca.key`。
- 面板默认只监听 `127.0.0.1`（无需鉴权）；一旦 `metrics_host` 绑到非本机地址，
  配置校验会**强制要求**设置 `metrics_token`，否则拒绝启动。
- **代理本身默认不鉴权**：同机任意进程都能用它转发流量。多用户机器（或以服务模式
  跑在 LocalSystem（Windows 内置的系统账户）下）建议设置 `proxy_token`——HTTP 侧走 `Proxy-Authorization`，
  SOCKS5 侧走用户名/密码（密码即 token）；Linux 还可用 `proxy_uid_whitelist`
  限制发起方 uid。`proxy_token` 的三种写法都支持：
  `Basic base64(用户名:token)`（标准，`curl -U 任意名:token`）、`Basic base64(token)`
  （省略用户名）、`Bearer token`。证书会在到期前 90 天开始告警，用 `--renew-certs` 续期。
- raw 内容经第三方镜像取回，默认首选 `gh-proxy.com`（实时拉取原版）。
  更高可信度可改为 `["jsdelivr_fastly"]`（主流 CDN，但有缓存延迟与大文件限制）。
- **携带凭证的请求绝不交给第三方镜像**。`Authorization` 是端到端头，转发给公共镜像
  等于把令牌交给镜像运营方。因此 hublane 一旦识别到 `Authorization` 头或
  `git-receive-pack`（即 `git push`），就把该请求收敛到自有上游（`direct` / `watt` /
  `chain`）。由此产生的后果是：**`git push` 不走 hublane** —— 请改用 SSH
  （`git remote set-url origin git@github.com:OWNER/REPO.git`），或单条命令绕过代理
  （`git -c http.proxy= -c https.proxy= push`）。匿名的 `git clone` / `fetch` 不受影响，
  仍然优先走镜像。
- **装完就能 `git push`**：受限网络常把 `github.com` 的 DNS 劫持到 `127.0.0.1`，
  于是 `ssh` 连到的是**本机 sshd**、必然被拒（看起来像密钥没配好），或者干脆封掉
  22 端口。安装脚本的最后一步会自动检测，必要时把 `git@github.com` 指向 GitHub
  官方的 `ssh.github.com:443` 入口；也可单独运行 `bash tools/setup-git-ssh.sh`
  （Windows 为 `tools/setup-git-ssh.ps1`）。排查见 `docs/TROUBLESHOOTING.md` 7e。
- **若你曾通过 hublane 使用过 GitHub 个人访问令牌（PAT），请 revoke 并重新签发**。
  旧版本会把令牌转发给第三方镜像运营方；已泄漏的凭证无法追回，轮换是唯一解。
- 重要用途请自行校验 checksum。

## 已知限制

- **没有出境节点就访问不了 Google / YouTube**。本项目不含也不推荐任何"免节点翻墙"方案。
- **访问控制默认关闭**：未设置 `proxy_token` 时，同机任意进程都能用代理
  （只在 `127.0.0.1` 上监听是当前的隔离手段）。多用户/服务模式请显式开启。
- 每个 `域名|上游` 在池中最多缓存 `pool_max_per_key` 条（默认 4），
  全局 `pool_max_total` 条（默认 64）；超出按最久未用淘汰。
- 大流量场景（容器镜像、大型 Release 包）经 Python 中继吞吐有限，
  建议这类域名用 `per_host_upstreams` 指向 `chain`（自己的节点）。
- `raw` 之外的链（`github_upstreams` / `extra_upstreams`）没有后台主动探测，
  健康度只来自真实流量；raw 镜像则每 120 秒主动探测一轮。
- 排障手册见 [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md)。

## 排障

完整手册（编号案例）见 [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md)。

**Windows 实测确认的坑** —— 下面每一条都在 Windows 上真实复现过，不是推测：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `curl: (60) schannel: the revocation status is unknown` | Windows schannel 会做 OCSP 吊销检查，而本地自签 CA 没有吊销响应者 | 加 `--ssl-no-revoke`，并用 `--cacert %LOCALAPPDATA%\hublane\ca.crt` |
| `SSL: CA cert does not include key usage extension` | Python 3.14 / OpenSSL 3.5 拒绝缺少 `keyUsage` 扩展的 CA | `python hublane.py --renew-ca` 后重新安装信任 |
| `.bat` 打印 `xxx was unexpected at this time.` | 在 `if (...)` 块里，`echo` 行同时含非 ASCII 文字和 ASCII 括号会破坏 cmd 的块解析 | `.bat` 保持 UTF-8，并避免在 `echo` 文本里写 `(...)` |
| WSL 下 `./install.sh: bad interpreter: /usr/bin/env bash^M` | 脚本被以 CRLF 检出 | 仓库已带 `.gitattributes` 固定 `.sh` 为 LF、`.bat` 为 CRLF；旧检出请重新 clone |
| 登录后代理几秒就没了，日志还是空的 | 生成的 `run-loop.bat` 调的是 `py`，而计划任务环境里没有它 | 已修复，重跑 `install-windows.bat` 即可，现在写入绝对解释器路径 |
| `ERROR: Input redirection is not supported` | `wscript.exe` 在 stdin 被重定向时启动（CI / agent / SSH） | 无害，仅出现在非交互会话里 |
| `sc stop` 后进程要过几秒才消失 | `concurrent.futures` 的 atexit 钩子要 join 线程池，等途中 DoH 请求返回 | 设计内：给 SCM 的等待提示就是 30 秒，实测约 10~15 秒 |
| 从计划任务模式换服务模式后，端口被一个不明 `python.exe` 占着 | `run-loop.bat` 是 `goto loop` 死循环，旧实例的监管进程会把 python 重新拉起 | 已在 `[3/6]` 按命令行收掉整个进程树，重装一次即可 |
| 找不到 `openssl` | Windows 不自带 OpenSSL，只有 Git for Windows 附带一份 | `pwsh -File tools/setup-windows-env.ps1` 会用 winget 装好 |

**怎么看面板计数**：`HTTP 请求` 只统计被 hublane 解密（MITM）过的请求。纯 TCP 隧道
（非托管域名、明文 `http://`、SSH）单独统计为 `隧道连接` / `隧道失败` / `隧道字节`，
这样总流量可见，又不会污染 HTTP 口径。`上游失败` 数的是**单次上游尝试**：一个请求
依次试三个上游就加三，所以它可能大于请求数，这不是 bug。隧道耗时**故意不计入延迟
分位数** —— 隧道可能存活数分钟，混进去会把 P50/P95 带偏。鼠标悬停任意卡片可看精确口径。

## 法律与合规使用

hublane 是一款**个人、自托管的效率工具**，用于帮助处于受限网络下的开发者访问其有权访问的开发资源（如 GitHub 原文件、安装脚本）。

- 你需自行遵守所在地区适用的法律法规与网络使用政策。
- 请勿利用它访问你无权访问的资源。
- CA 证书在**本机安装时本地生成**，绝不会被发送到任何地方。请仅在你信任且可控制的机器上安装它，并在不再使用时移除。
- 私钥/证书处理与漏洞上报策略见 [SECURITY.md](./SECURITY.md)。

## 许可证

MIT —— 见 [LICENSE](./LICENSE)。

---

## 术语速查

正文里缩写第一次出现时会就地解释，这里放一份完整对照表。

**网络与协议**

| 术语 | 含义 |
|---|---|
| DNS | 域名解析：把 `github.com` 这样的域名翻译成 IP 地址 |
| `A` / `AAAA` | DNS 记录类型：`A` 是 IPv4 地址，`AAAA` 是 IPv6 地址；一次解析可同时查两类 |
| DoH | DNS over HTTPS，用 HTTPS 加密地查询 DNS，避免明文 DNS 被篡改或投毒 |
| TCP RST | TCP 复位报文：对端**立即断开**连接。与"超时"不同，RST 是被明确拒绝，往往是封锁的信号 |
| TLS | HTTPS 使用的加密协议；本项目需为受管域名代管证书，故必须解密 |
| SNI | TLS 握手时客户端告知服务器"我要访问哪个域名"，证书按域名签发，验真时必须带上 |
| HTTP `CONNECT` | HTTP 代理里"帮我连到 `host:port` 并双向转发"的那条指令，本项目用它建隧道 |
| SOCKS5 | 另一种代理协议（不解析 HTTP，只转发 TCP），本项目与 HTTP **共用同一端口** |
| MITM / TLS 中间人 | 在客户端与目标站之间解密再加密；本项目只对受管域名这样做，所以要装自签 CA |
| CA | 证书颁发机构。本项目自签一个只存在本机的 CA，用它签发各受管域名的叶证书 |
| PAC | Proxy Auto-Config，浏览器用它自动决定"哪些域名走代理" |
| 回环地址 `127.0.0.1` | 只指向本机自己、不经过网卡的地址，因此对局域网与外部不可见 |
| DPI | 深度包检测：按流量特征识别并阻断。本项目对抗手段是镜像换源 + TLS 验真 |
| chunked 传输编码 | HTTP 的分块传输方式，边收边发，以"长度为 0 的空块"作为结束标记 |
| MiB | 1024 × 1024 字节 |

**上游与选路**

| 术语 | 含义 |
|---|---|
| 上游（upstream） | 实际去取内容的服务器：`direct`（直连真实 IP）、`watt`（Windows 侧加速器）、`chain`（你自己的节点代理）、镜像站 |
| raw 镜像站 | 代理 `raw.githubusercontent.com` 的第三方站点；该域名直连必被 RST，只能走镜像 |
| 上游链 / 回落 | 同一域名的候选上游按顺序尝试，失败就自动退到下一个 |
| 探活 | 复用连接前先确认对端没关闭连接；探活失败则换新连接重试 |
| 连接池 / LRU | 缓存并复用上游连接；超出上限时淘汰最久未用的（LRU = Least Recently Used） |
| 流式转发 | 边收边转发，不把整个响应读进内存，大文件才不会撑爆内存 |
| 响应完整性校验 | 比对实际收到的字节数与 `Content-Length`，判断响应是否被中途掐断 |

**测速与统计**

| 术语 | 含义 |
|---|---|
| EWMA | Exponentially Weighted Moving Average，指数加权移动平均；越近的样本权重越大，用来平滑"这次有多快" |
| 滑动窗口 | 只统计最近 N 次样本（如 20 次）的窗口；比"历史平均值"更能反映当下状态 |
| 成功率 | 窗口内成功次数 ÷ 总次数；样本不足时视为"未证明"，排在已证明可用的上游之后 |
| 冷却（cooldown） | 失败后暂时跳过该上游，随时间推移自动恢复，避免反复撞墙 |
| 分位数 P50 / P95 / P99 | 延迟排序后的第 50% / 95% / 99% 分位点：P50 是"典型速度"，P99 是"最慢的那 1% 有多慢" |

**自启相关**

| 术语 | 含义 |
|---|---|
| SCM | Service Control Manager，Windows 的服务控制管理器；`--service` 通过它注册服务 |
| ctypes | Python 标准库里调用系统 DLL 的接口，因此实现真服务不需要第三方依赖 |
| 计划任务 / systemd | 两种"开机自启 + 崩溃自动重启"机制，分别对应 Windows 与 Linux/WSL |
