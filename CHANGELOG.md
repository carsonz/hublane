# Changelog

本文件记录 hublane 的每个发布版本，格式基于 [Keep a Changelog](https://keepachangelog.com/)。

**格式约定**

- `## [版本号] - YYYY-MM-DD` —— 一个发布版本的段落，日期为该版本的发布日期；
  版本号遵循 SemVer，同时必须与 `hublane.py` 的 `VERSION`、`pyproject.toml` 的
  `version` 一致（CI 用 `tools/check_version.py` 门禁）。
- `## [Unreleased]` —— 尚未发布的改动，发版时整体移到新版本段落下。
- 段落内小节固定为：`Added` 新增功能 / `Changed` 行为变更 / `Fixed` 修复 bug /
  `Security` 安全相关 / `Documentation` 文档 / `Performance` 性能与基准 /
  `Known issues` 已知问题（写明计划修复的版本）。

---

## [Unreleased]

（暂无）

## [0.1.0] - 2026-10-07

首个发布版本。此前所有改动只在本地经过人工验证、从未打过 tag，因此原先内部按
1.0.0 → 1.4.0 记录的里程碑全部合并为 0.1.0 一次性交付。0.x 阶段**不承诺接口稳定**。

### Added

**转发与协议**

- 流式转发：响应与请求体都走流式，大响应与大 POST/PUT 不整体缓冲进内存，
  chunked 请求体逐块解析。
- 单端口双协议：`127.0.0.1:8899` 上按首字节判别 HTTP 与 SOCKS5；
  受管域名终止 TLS 后走上游链，非受管域名走纯 TCP 隧道（不解密、不改写）。
- SSH(22) 转发：git-over-SSH 经 `chain_socks_port` 走节点 SOCKS5。
- IPv6 与多路径解析：`enable_ipv6` 控制 A/AAAA 双查；启动后台线程预热 DNS，
  首请求不等 DoH。
- PAC 自动代理配置：`/pac` 输出精确 + 通配规则，浏览器/PAC 客户端零配置。

**上游选择与健康度**

- 四种上游类型：`direct`（DoH 多路径 + 并发真实 TLS 握手验真，连最快 IP）、
  `watt`（Windows 侧 Watt Toolkit）、`chain`（Clash/v2rayN/sing-box）、镜像站。
- raw 镜像库：15 个前缀式 + 域名替换式（`gitmirror`、`kkgithub`）+ CDN 变体
  （jsDelivr fastly/gcore/cf）；默认链自动追加尾部镜像，可用 `custom_mirrors`
  插拔自定义镜像、用 `upstream_list_url` 拉远程上游清单。
- 非 GitHub 站点整站换源：HuggingFace→hf-mirror、PyPI→清华/阿里、
  npm→npmmirror、Go→goproxy.cn、Conda→清华、Crates→rsproxy、nodejs→npmmirror、
  jsDelivr→fastly/jsd、字体→`fonts.googleapis.cn` + `gstatic.cn`（必须成对替换）。
  均为受限网络下联网实测后才入库，原始记录见 `docs/MIRRORS.md`。
- 按域名的上游链：`per_host_upstreams` 支持精确域名与 `*.example.com`
  （最长后缀优先），出现过的域名自动视为受管域名，PAC 同步输出通配规则。
- 境外站点分组库 `extra_host_groups`：字体 / Pages / 容器 / 工具链 / Git 托管 /
  AI 模型 / Python / npm / Go / Rust / Conda 共 11 组，整组开关
  （默认只开 `fonts_cdn`），校验拒绝未知分组名。
- 自适应排序：EWMA × 成功率（最近 20 次滑动窗口）、失败冷却、样本不足视为"未证明"；
  状态持久化到 `state.json`，旧格式（无 `hist`）向后兼容。
- 上游连接池：复用前探活（TLS 连接单独处理，`SSL_read` 不能 PEEK），失效时用新连接
  透明重试一次；每个 `域名|上游` 多条连接 + 全局 LRU 容量上限
  （`pool_max_per_key` / `pool_max_total`）。
- DoH 端点可插拔：支持 URL 模板或 `{"url","name","enabled"}`；按配置顺序取前 2 个
  并发查询；端点级成功/失败/EWMA/冷却持久化并在面板展示，连续失败自动下沉；
  内置 11 个端点（国内优先）。
- 全链路主动探测：探测目标覆盖 raw 镜像、`github.com` 与 extra 站点代表路径，
  与真实流量共用同一份健康度；新增 `probe_enabled` / `probe_delay` /
  `probe_interval` / `probe_extra_max`。
- 响应完整性校验：已知 `Content-Length` 且 ≤ `integrity_buffer_max`（默认 1 MiB）
  的响应先整体校验再转发，截断可**透明回落到下一个上游**；大响应保持流式，
  截断时不补 chunked 结束标记并主动断开。

**健壮性与安全基线**

- 客户端连接治理：`client_timeout`（默认 60s，防 slowloris 占住线程）、
  `max_conns`（默认 256，超限直接关闭并计 `counters.rejected`）；限流按监听实例隔离。
- 本地访问控制：`proxy_token`（HTTP 侧校验 `Proxy-Authorization`，失败 407 +
  `Proxy-Authenticate`；SOCKS5 侧启用 method 0x02，失败分别 0xFF / 0x01）；
  Linux/WSL 另有 `proxy_uid_whitelist`（`SO_PEERCRED` 校验发起方 uid）。
  未配置时行为与无鉴权版一致。
- 证书生命周期：启动与面板显示 CA/叶证书剩余天数（<90 天告警）；
  `hublane.py --renew-certs` 只换叶证书（保留 CA，无需重装信任）、`--renew-ca` 连 CA 换。
- Windows 真服务：`--service` 用 ctypes 注册 SCM 处理函数（`sc stop` 优雅退出），
  配 `install-windows-service.bat` / `uninstall-windows-service.bat`
  （`sc create start= auto` + `sc failure` 崩溃自愈 + 停用原计划任务）；
  日志目录不可写时回退临时目录。
- 配置校验入口：`hublane.py --check` 只校验配置即退出（部署前门禁，CI 也跑）、
  `--config` 支持自定义配置路径。
- Windows 自愈计划任务：`install-windows.bat` 部署 run-loop 脚本 + 隐藏 vbs，
  进程被杀掉后自动拉起。

**可观测性与运维**

- HTML 面板：根路径为面板，含上游与 DoH 健康度表、计数器与延迟卡片、延迟分位数表、
  最近请求表、已校验真实 IP 表，以及平台/监听/连接池/活动连接/运行时长/日志路径/
  证书剩余/访问控制/已启用分组等元信息，5 秒自动刷新。
- 指标端点：`/status`、`/requests`、`/pac`、`/healthz`、`/diag`（`/healthz` 唯一免 token）。
- 指标鉴权：`metrics_token`（请求头 `X-Hublane-Token` 或 `?token=`）、
  `metrics_host` 绑非本机地址时强制要求 token、`metrics_enabled` 可整体关掉指标端点
  （失败仅告警，不影响代理转发）。
- 延迟分位数与请求样本：固定桶直方图按 `域名|上游` 统计 P50/P95/P99；
  最近 N 条请求样本（`sample_size`，默认 200，0 = 关闭）经 `/requests` 输出并在面板展示。
  隐私边界：**仅内存、不落盘、不出本机**。
- 结构化日志：`log_format: json` 单行 JSON（ts/level/logger/msg/…），
  systemd `journalctl -o json` 与 Windows 服务模式都可用；文件日志轮转 2 MiB × 3。
- 配置热重载：`SIGHUP` + `POST /reload`（面板有按钮，token 保护）重载 `config.json`；
  **先校验再切换**，非法配置保留旧配置并告警；监听端口等结构性参数提示"需重启"。
- 诊断包 `/diag`：版本/平台/监听/运行时长 + 配置摘要（token 打码）+ `/status` 全量
  + 日志尾部 200 行；issue 模板改为"贴 `/diag` 输出"。

**安装、分发与工程化**

- `install.sh`：systemd 服务 + zsh/bash rc 注入 + deb/rpm 双信任库；
  `-y/--yes`（非交互）、`--dry-run`（只打印步骤）、`--skip-verify`、`--help`；
  WSL 自检（systemd 可用性、`resolv.conf` 提示）；systemd 不可用时回退 nohup。
- `install-windows.bat`：Python 探测、本地生成 CA + 叶证书、certutil 装信任、
  系统代理设置、Firefox 需单独导入 CA 的提示（Firefox 不用系统证书库）。
- 卸载器：`uninstall.sh`（`--purge` + shell rc 备份）、`uninstall-windows.bat`、
  `uninstall-windows-service.bat`。
- Release 直附"下载即用"的 `hublane.py` 单文件 + `config.json` + 各平台安装/卸载脚本，
  另附源码 tar.gz、sdist/wheel 与 `SHA256SUMS`。
- GitHub artifact attestation（`actions/attest-build-provenance`）对发布产物签名。
- CI 矩阵（ubuntu/windows × py3.9–3.13）：flake8、配置校验、单元 + 集成测试、
  版本一致性、覆盖率报告、本地压测（仅提示不阻断）；另有 CodeQL 工作流、
  dependabot、PR/issue 模板、`setup.cfg`（flake8 规则）、`Makefile`。
- tag 驱动发布流程：版本一致性门禁（`tools/check_version.py`）→ flake8 + 测试 →
  打包 → `SHA256SUMS` → 用 `tools/changelog_section.py` 从本文件取对应段落作 Release 说明。
- 压测基线：`bench/bench_relay.py`（本地假上游 + 进程内 hublane + 真 TLS 客户端）输出
  rps/P50/P95/失败数，`--save` 存基线、`--check` 对比（P95 超基线 1.5 倍判回归）；
  已采集 `bench/baseline.json`（200 请求 / 8 并发 / 64KB：约 300 rps，P95 34ms）。
- 测试：`tests/test_hublane.py`（纯逻辑，含 `TestCertSan` 校验三处 SAN 一致）+
  `tests/test_integration.py`（本地假上游 + 真 TLS 客户端，**不需要外网**，
  缺 `openssl` 时自动跳过）。

### Changed

- 版本号统一为 `0.1.0`（`hublane.py` / `pyproject.toml`，CI 校验）：
  原先内部里程碑 1.0.0 → 1.4.0 从未发布，合并为首个版本。
- `LEAF_SAN` 常量改为 `leaf_san()` 函数，随站点库自动覆盖新域名（现约 90 条）。
- `_read_head()` 现在同时返回请求头（访问控制需要 `Proxy-Authorization`）。
- 就绪探测改用指标端点 `/healthz`（不再占用代理的并发名额）。
- `validate_config()` 可选接受一份待生效的配置字典，供热重载先验证后切换。
- `load_config()` 拆出 `_load_merged()`（合并 CONF 与默认值、返回新字典）。
- 限流器按监听实例隔离（重启/多实例不会互相干扰计数）。
- 内部请求（DoH / 校验 / 镜像）改走内部 opener，不再受 `http_proxy` 影响。

### Fixed

- 探活对 TLS 连接使用 `MSG_PEEK` 会抛 `ValueError`，导致连接池对 watt/direct 完全失效。
- `direct_cooldown` 配置项此前从未生效（固定走 30 × 失败次数）。
- 流式响应的 `finally` 无条件补 chunked 结束标记，把"被截断"伪装成完整响应。
- `http.client` 因 `Connection: close` 关闭的连接仍被放回池中。
- 代理内部请求（DoH / 校验 / 镜像）此前会受 `http_proxy` 影响绕回自己。
- 探测写入的 scope 是 `"raw"`，与真实流量的 `raw.githubusercontent.com` 不是同一个键 ——
  探测数据一直没有参与排序，现统一为真实域名。
- `CONNECT [::1]:443` 形式的 IPv6 字面量解析错误。
- 证书 SAN 漏掉分组站点（`pypi.org` / `registry.npmjs.org` / `proxy.golang.org` /
  `repo.anaconda.com` / `nodejs.org` / `index.crates.io` 等不在叶证书 SAN 里，
  启用分组后报"证书名不匹配"）；现由 `leaf_san()` 从站点库推导，
  已安装用户执行 `python hublane.py --renew-certs` 即可获得新 SAN。
- `jsdelivr_fastly` 同时是 raw 镜像名（会被重写成 `/gh/...` 路径），用作通用 CDN
  反代会拼错路径 —— 改名 `jsdelivr_fastly_cdn`，并新增"镜像名不得与 raw 镜像冲突"的测试。
- `pyproject.toml` 的 `Operating Language :: Python :: 3.9` 是无效 classifier。
- README 徽章与早期说明中声称的 CI 工作流此前并不存在（本版补齐）。

### Security

- 证书与私钥改为**安装时本地生成**（`openssl`），**私钥从不入库**；
  此前仓库曾附带私钥，本版起只保留生成脚本与文档说明（见 `SECURITY.md`）。
- 启动时提示私钥文件权限过宽（POSIX，建议 `chmod 600`）。
- `metrics_host` 绑到非本机地址必须设置 `metrics_token`，否则配置校验拒绝启动。
- 新增 `proxy_token` / `proxy_uid_whitelist`：本地回环端口从"人人可用"变为
  可要求凭据（HTTP 407 / SOCKS5 0xFF、0x01）。

### Documentation

- `docs/ARCHITECTURE.md`：一次请求的完整时序图、模块地图、线程模型、
  状态与持久化清单（哪些落盘、哪些只在内存）、安全模型、容易踩的坑。
- `docs/MIRRORS.md`：镜像与站点库的实测记录、复测方法、新增条目的检查清单。
- `docs/TROUBLESHOOTING.md`：常见故障与处理命令。
- `docs/EVALUATIONS.md`：`[A]` 待评估项的结论（Windows 免 Python exe、macOS 支持、
  HTTP/2 与压缩透传、单文件拆分）。
- `README.md` / `README.zh-CN.md`：配置项表、镜像库表、非 GitHub 整站换源表、
  排障与安全章节。

### Known issues

以下问题已知，计划在 v0.2.0 修复（详见 `ROADMAP.md`）：

- `enable_socks5` 目前只用于面板与 `/status` 展示，`handle_client` 仍无条件嗅探 SOCKS5
  —— 该配置项尚未真正生效。
- `verbose` 写在配置默认值里，但全代码没有读取点。
- 安装脚本没有透传 `--renew-certs` / `--renew-ca`，证书续期需手敲命令。
- Firefox 集成只提示手动导入 CA，未实现 `policies.json`
  （`security.enterprise_roots`）。
- 覆盖率只输出报告，尚未加 `--fail-under` 门禁，也未在 PR 展示。
- OpenSSF Scorecard 徽章与 scoop/winget/Homebrew 分发尚未提供。