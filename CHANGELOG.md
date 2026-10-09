# Changelog

本文件记录 hublane 的每个发布版本，格式基于 [Keep a Changelog](https://keepachangelog.com/)。


**格式约定**

- `## [版本号] - YYYY-MM-DD` —— 一个发布版本的段落，日期为该版本的发布日期；
  版本号遵循 SemVer。**版本号只有一处来源：`hublane.py` 里的 `VERSION`**；
  `pyproject.toml` 不写死版本，而是在构建期通过 `[tool.setuptools.dynamic]`
  读取它。因此发版只需改 `hublane.py` 一处，
  `tools/check_version.py` 会拦住任何想在别处再写一份的改动。
- `## [Unreleased]` —— 尚未发布的改动，发版时整体移到新版本段落下。
- 段落内小节固定为：`Added` 新增功能 / `Changed` 行为变更 / `Fixed` 修复 bug /
  `Security` 安全相关 / `Documentation` 文档 / `Performance` 性能与基准 /
  `Known issues` 已知问题（写明计划修复的版本）。
- **段落正文会原样成为 GitHub Release 说明**（`tools/changelog_section.py` 抽取），
  所以写的时候按"给用户的发布公告"来写：只写用户能感知的变化，每条一两句，
  内部论证与排查过程放 `docs/`。

---

## [Unreleased]

## [0.2.0] - 2026-10-09

### Added

- **安装时自动修好 GitHub 的 SSH 入口**：检测到 `github.com` 被劫持或 22 端口不通时，
  把 `git@github.com` 指到官方 `ssh.github.com:443`，解决 `git push` 报
  `Permission denied (publickey)` 的问题。默认只在异常时写，且只往 `~/.ssh/config` 追加。
- **动态已验真 IP 池**：预置 IP、历史已验真 IP 与 DoH 新解析合并后统一重新验真，
  验不过的自动淘汰 —— 换网络不用手工清理，DoH 全挂时仍能顶上。
- **`abort` 伪上游**：写进上游链即快速失败，不发起连接、不付超时代价，
  用来让"装了代理反而更卡"的站点直连，已内置一批默认规则。
- **面板：URL → 等价命令**，粘贴 URL 直接给出 `curl` / `git clone` / `npm` / `pip` 的一行命令。
- **面板：立即刷新与暂停**，刷新不等后台周期；暂停后所有域名走纯 TCP 隧道直通，可随时恢复。
- **面板：健康度表列头可点击排序**。
- **`GET /hosts`**：以 hosts 格式导出已验真 IP。
- **`--check-update` / `--update`**：前者只提示，后者拉取新版并在校验失败时整体回滚。
- **Gitee 镜像同步 Release 附件**。
- **接入 OpenSSF Scorecard**。

### Changed

- **`enable_socks5` 真正生效**：置 `false` 后该端口只服务 HTTP。
- **`verbose` 接上，默认值改为 `false`**。
- **安装脚本透传 `--renew-certs` / `--renew-ca`**，证书续期不必再手敲命令。
- **Windows 安装脚本自动写 Firefox 企业策略**（`tools/setup-firefox-policy.ps1`，
  `Certificates.Install` + `ImportEnterpriseRoots`），Firefox 不再需要手动导入 CA；
  未装 Firefox 时自动跳过。
- **面板 5 秒自动刷新改为 JS 定时**：URL 工具输入框有内容时跳过本轮刷新，
  不再把贴进去的 URL 和生成结果每 5 秒清空一次；无输入时刷新行为不变。
- **PAC 覆盖 `per_host_upstreams` 里的精确域名**，浏览器不再绕过这些规则。

### Fixed

- **`/diag` 会泄露 `metrics_token`**：日志里的 `token=` 现在写盘之前打码。
- **PowerShell 脚本缺 UTF-8 BOM**：Windows PowerShell 5.1 对无 BOM 脚本按 ANSI
  解码，中文双字节序列可能"造出"`{` 或引号使整段脚本语法错误。
- **安装脚本里的 `timeout` 等待会落空**：stdin 不是控制台时（脚本调脚本、
  输出被重定向）`timeout` 直接报 `Input redirection is not supported` 退出，
  改用 `ping -n` 兜底。
- **`uninstall-windows-service.bat` 误报"服务仍在"**：先按 `sc queryex` 拿到的 PID
  杀掉服务进程，再用 `sc query` 的存在性（而非 `sc delete` 的报错）来判定。

### Documentation

- `docs/EVALUATIONS.md` 新增 5 项结案，其中 ECH 明确不做。
- `docs/TROUBLESHOOTING.md` 补充 `git push` 失败的分情况排查。

## [0.1.1] - 2026-10-08

WSL/Ubuntu 上的实测（Python 3.13 + OpenSSL 3.6、systemd 启用）暴露了下面这些问题，
都已修复。原先这些问题在"只跑进程内集成测试 + 老 OpenSSL"的组合下发现不了。

### Added

- `tools/build-exe.py`：PyInstaller 打 Windows 单文件 exe。
- `.gitattributes` 固定 `.sh`=LF、`.bat`/`.ps1`=CRLF。
- **github.com 专用镜像链（解决 `git clone/fetch` 卡死）**。原先 github.com 只有
  `direct` / `watt` 两个上游：
  结果：`git ls-remote` 与小仓库能过（响应 < 128 KiB），**真实的 fetch/clone 都卡死**。
  现在新增 `GH_MIRROR_PREFIX`（`ghproxy_com_gh` / `ghfast_gh` / `ghproxy_net_gh`），
  并把它们排到默认 `github_upstreams` 的**最前面**（必须早于 `direct`，否则会先撞上掐断）。
  实测：deepseek-harness `git fetch` 144 MB / 9.96s；hermes-agent 浅克隆 85.8 MB / 8.5s。
  > 注：`ghproxy.link`（307 跳转后大文件只回 1739B）与 `gitclone.com`（502）在本机实测
  > 不可用，故未内置；如在你网络下可用，用 `custom_mirrors` 自行加入即可。

- `tools/build_exe.sh`：单文件可执行文件打包（PyInstaller），`--install-deps` 用
  apt 补齐系统依赖。
- `tools/verify_install_sandbox.sh`：**无需 root** 即可验证 `install.sh` /
  `uninstall.sh` 的逻辑（43 项断言：证书链与扩展、临时文件清理、私钥权限、
  systemd unit 内容、解释器一致性、shell 变量注入与幂等、sudo 归属、
  信任库增删、`--purge`）。此前安装脚本从未被任何自动化测试覆盖。
- `tests/test_runtime_e2e.py`：以**独立进程**跑的端到端验收（18 项）—— 真TLS
  客户端走 MITM、纯隧道不落手 SOCKS5、代理/metrics 双层鉴权、面板与 `/status` /
  `/requests` / `/diag` / `/pac`、延迟分位与请求样本、`POST /reload` 与 `SIGHUP`
  热重载、JSON 结构化日志。上游是本地假服务器，**不需要外网**。
- 面板新增隧道维度（隧道连接 / 隧道失败 / 隧道字节）与计数口径悬停提示

### Changed

- **版本号收敛为单一来源**： `pyproject.toml` 改用 `[tool.setuptools.dynamic]` 在构建期读取
  `hublane.VERSION`，从根上消掉重复。
- `tunnel()` 流量纳入独立统计维度，隧道耗时不计入延迟直方图（避免带偏 P95）
- 抽出 `_start_metrics()`，`main()` 复杂度由 16 降到 ≤15（修复 flake8 C901 门禁）

- **面板的"最近请求"与"已校验真实 IP"增加滚动条**。

### Fixed

- **通过实测修复0.1.0中的大量BUG**。

### Security

- **本地 CA 证书补上 `keyUsage` 等扩展**。否则OpenSSL 3.5+ 会直接报
  `CA cert does not include key usage extension` 拒绝校验。
- CA 自签改用 `CSR + x509 -signkey` 而非 `req -x509`：`req -x509` 不接受
  `-extfile`，只能退回 `-addext`；而 **`-addext` 会让 openssl 载入配置/provider，
  在 PyInstaller 冻结产物里调用会导致 SIGSEGV**（实测），正好卡住"免安装 exe"这条路。
- **带凭证 / git 写操作的请求不再交给第三方镜像**。

### Documentation

- README 排障章节、`docs/TROUBLESHOOTING.md` 第 9 章（Windows 专项）、
  第 10 章（计数口径）
- 补充说明「`git push` 应直连或改用 SSH」：hublane 的价值在于匿名只读流量的加速，
  写操作不在其设计范围内

## [0.1.0] - 2026-10-07

首个发布版本。hublane 是跑在你自己机器上的中继代理：把 GitHub、raw 安装脚本、
`git clone`、npm/pip/conda 这类境外流量接过来，自动挑可用上游与真实 IP 发出去。
不改系统 hosts，也不用手工找镜像。0.x 阶段不承诺接口稳定。

### 核心功能

- **整站换源，开箱可用**：内置 raw 镜像库，以及 HuggingFace / PyPI / npm / Go / Conda /
  Crates / jsDelivr / Google Fonts 等整站换源规则，均已实测后才入库。
- **自动选路**：直连、Watt Toolkit、自有节点、镜像站四类上游按健康度排序，
  响应被截断会自动回落到下一个上游。
- **绕开污染 DNS**：DoH 多路径解析加真实 TLS 握手验真，只连真正握得通的 IP。
- **单端口双协议**：同一端口按首字节判别 HTTP 与 SOCKS5，非受管域名走纯 TCP 隧道，
  不解密、不改写。
- **PAC 自动配置**：浏览器零配置。
- **看得见**：Web 面板展示上游与 DoH 健康度、延迟分位、最近请求，配置支持热重载。
- **管得住**：可限制谁能用这个本地端口、谁能看指标；CA 与私钥本机生成，私钥不入库。
- **双平台一键安装与卸载**：Linux/WSL 走 systemd 与系统信任库，Windows 走服务或计划任务。
