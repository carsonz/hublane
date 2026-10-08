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
  首个版本只介绍核心功能、不列 bug 修复。内部论证与排查过程放 `docs/`。

---

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
- **PAC 覆盖 `per_host_upstreams` 里的精确域名**，浏览器不再绕过这些规则。

### Fixed

- **`/diag` 会泄露 `metrics_token`**：日志里的 `token=` 现在写盘之前就打码。

### Documentation

- `docs/EVALUATIONS.md` 新增 5 项结案，其中 ECH 明确不做。
- `docs/TROUBLESHOOTING.md` 补充 `git push` 失败的分情况排查。

## [0.1.1] - 2026-10-08

WSL/Ubuntu 上的实测（Python 3.13 + OpenSSL 3.6、systemd 启用）暴露了下面这些问题，
都已修复。原先这些问题在"只跑进程内集成测试 + 老 OpenSSL"的组合下发现不了。

### Added

**Windows 侧（管理员会话实测收口）**

- `tools/build-exe.py`：PyInstaller 打 Windows 单文件 exe。处理三个真跑才暴露的坑 ——
  onefile 下 `__file__` 指向会被清空的 `_MEI` 目录（已改取 `sys.executable` 所在目录，
  并支持 `HUBLANE_HOME` 覆盖）；目标机常无 openssl（构建时预先生成 CA + 叶证书打进包，
  运行时由 `seed_bundled()` 播种）；杀软误报（提示 `--onedir` 更不易被拦）。
- `tools/verify-windows.ps1`：管理员级全流程实测，45 项断言，自带清理。
- `tools/setup-windows-env.ps1`：winget 依赖引导。
- `tools/build-exe.py` 与 `tools/build_exe.sh` 分工：前者打 Windows，后者打 Linux/WSL
  并用 apt 补系统依赖 + 跑冻结冒烟。
- `.gitattributes` 固定 `.sh`=LF、`.bat`/`.ps1`=CRLF。
- `_win_service()` 抽成可测的 `ServiceCore` 状态机，补 `tests/test_windows_service.py`。

- **github.com 专用镜像链（P0，解决 `git clone/fetch` 卡死）**。原先 github.com 只有
  `direct` / `watt` 两个上游：
  - `direct` 会被链路在**正好 128 KiB** 处掐断 —— 实测
    `SSLEOFError: EOF occurred in violation of protocol`，`传输中断于 131072 字节`；
    凡是大于 128 KiB 的响应（也就是 git 的 pack）永远下不来；
  - `watt` 很慢（114 KB 要 16.9s ≈ 6.8 KB/s），且部分大请求返回 0 字节。
  结果：`git ls-remote` 与小仓库能过（响应 < 128 KiB），**任何真实的 fetch/clone 都卡死**，
  最终 ~127s 后拿到 502（`fatal: error reading section header 'acknowledgments'`）。
  现在新增 `GH_MIRROR_PREFIX`（`ghproxy_com_gh` / `ghfast_gh` / `ghproxy_net_gh`），
  并把它们排到默认 `github_upstreams` 的**最前面**（必须早于 `direct`，否则会先撞上掐断）。
  实测：deepseek-harness `git fetch` 144 MB / 9.96s；hermes-agent 浅克隆 85.8 MB / 8.5s。
  > 注：`ghproxy.link`（307 跳转后大文件只回 1739B）与 `gitclone.com`（502）在本机实测
  > 不可用，故未内置；如在你网络下可用，用 `custom_mirrors` 自行加入即可。

- `tools/build_exe.sh`：单文件可执行文件打包（PyInstaller），`--install-deps` 用
  apt 补齐系统依赖，`--smoke` 打包后自动验证冻结产物能在稳定目录生成并校验证书。
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

- **版本号收敛为单一来源**：原先 `hublane.py` 的 `VERSION` 与 `pyproject.toml` 的
  `version` 各写一份，靠 `tools/check_version.py` 事后比对 —— 漏改就会构建出版本号
  错误的产物。现在 `pyproject.toml` 改用 `[tool.setuptools.dynamic]` 在构建期读取
  `hublane.VERSION`，从根上消掉重复；`check_version.py` 的职责相应改为**守住单一来源**
  （发现有人重新写死 `version`、或 dynamic 指错、或 tag 不符都会失败）。
  已验证 `python -m build` 产出的 wheel/sdist 正确带上版本号。
- `tunnel()` 流量纳入独立统计维度，隧道耗时不计入延迟直方图（避免带偏 P95）
- 抽出 `_start_metrics()`，`main()` 复杂度由 16 降到 ≤15（修复 flake8 C901 门禁）

- **面板的"最近请求"与"已校验真实 IP"改为固定高度 + 滚动**。这两块条数多、
  信息价值低（排障时看前几条就够），原先不限高会把页脚顶出屏幕。
  新增 `panel_scroll_rows`（默认 16 行，可配5–40 行），表头吸顶，滚动时仍能对列。
  健康度 / DoH / 延迟分位三张表仍完整展示，不限高。

### Fixed

- **打包配置导致 sdist/wheel 根本构建不出来（P0）**。`pyproject.toml` 同时写了
  PEP 639 的 `license = "MIT"` 与旧式 `License :: OSI Approved :: MIT License`
  分类器，setuptools ≥ 77 会直接 `InvalidConfigError`。
  `.github/workflows/release.yml` 的打包步骤因此必然失败 —— 也就是说 v0.1.0 一旦
  打 tag 就会在打包环节挂掉。已移除冲突的分类器。
- `make package` 把sdist/wheel 的构建失败吞掉并打印"未安装 build"，把**配置错误
  伪装成缺工具**。改成先探测 `build` 是否可用，失败则正常报错。
- **冻结形态下 `INSTALL_DIR` 指向临时解压目录**。PyInstaller 打包后 `__file__`
  在进程退出即被删除的 `_MEIxxxx` 下，证书/配置/状态写进去等于每次启动全部丢失
  （CA 要重新信任，实际不可用）。冻结形态改用稳定目录（`%LOCALAPPDATA%\hublane`，
  可用 `HUBLANE_HOME` 覆盖）。
- **`install.sh` 在 `sudo` 下把代理变量写进 root 的家目录**。脚本按`$HOME` /
  `$(id -un)` 定位 rc 文件，而 `sudo bash install.sh`（README 推荐的用法）下两者
  都是 root —— 用户自己的 shell 拿不到，`uninstall.sh`（本来就按 `SUDO_USER`
  清理）也清不掉。现在与 `uninstall.sh` 一致，按 `SUDO_USER` 定位。
- **代理变量写进了错误的 rc 文件**（2026-10-08 真机安装时踩到）。判定 shell 时用了
  继承来的 `$SHELL`，而不是 passwd 里登记的登录 shell：zsh 用户从 IDE 任务 / CI /
  bash 脚本里跑 `sudo bash install.sh`，`$SHELL` 是外层 runner 的 `/bin/bash`，
  于是变量进了 `~/.bashrc`，而用户实际天天用的 zsh **永远读不到**。现在以 passwd
  为准（`${USER_SHELL:-$SHELL}`），`$SHELL` 只作兜底。
- **zsh 改写 `~/.zshenv` 而不是 `~/.zshrc`**。`.zshrc` 只有交互式 shell 读取，
  `zsh -c` 与 zsh 脚本拿不到代理，会出现"交互式能用、脚本里不走代理"的怪现象；
  `.zshenv` 被所有 zsh 读取，一个文件就够。而且它在 `.zshrc` **之前**执行 ——
  用户想给某个项目换别的节点，只要写在 `.zshrc` 里就能覆盖这个默认值。
  `uninstall.sh` 四个 rc 都查（含 `.zshenv` 与历史遗留的 `.zshrc`）。
- **`.bat` 中文 + ASCII 括号导致 cmd 解析崩溃**：改写为标签跳转，清理 14 处。
- **`run-loop.bat` 写 `py` 而非绝对路径**：计划任务下静默死循环（`py` 启动器不在 PATH）。
- **服务模式留下影子实例**：`schtasks /end` 收不掉 run-loop 子进程。
- **指标面板端口从不显式关闭**：补 `_stop_metrics()`。
- 面板计数 `fail` 语义歧义，改为「上游成功 / 上游失败」并加悬停说明；
  字节数自动换算 KB/MB。
- **升级不再静默丢掉用户配置**。`install.sh` / `install-windows.bat` 以前是
  `install`/`copy` **无条件覆盖** `config.json`，于是每次升级都会把用户改过的
  端口、`metrics_token`、上游选择、日志设置等全部还原成默认值。现在改为：
  保留已有配置（另存 `config.json.bak-<时间戳>`）、把新版默认配置写到
  `config.json.new` 并提示 `diff` 合并；想直接用新版可加 `--reset-config`。
  另外保留的旧配置会用**新代码**先校验一遍，不通过就在覆盖任何文件之前中止，
  不会留下"装了一半"的状态。
- **质量门禁 `make lint` 在 v0.1.0 上就是红的**（11 处 flake8 违规），CI 的
  "代码风格" 步骤会失败。已全部修掉，现在 `flake8 hublane.py tests tools bench`
  干净通过。
- 证书生成失败时不再只报 `CalledProcessError`：把 openssl 的 stderr 一并带出，
  否则用户看不到真实原因。
- `proxy_token` 的 `Proxy-Authorization: Basic` 现在也接受**省略用户名的写法**
  （`Basic base64(token)`），此前只有标准的 `Basic base64(user:token)` 能通过。
- **安装脚本与打包工具的中文乱码**：Windows 控制台默认非 UTF-8，已统一 `chcp 65001`，输出流按 utf-8 处理

### Security

- **本地 CA 证书补上 `keyUsage` 等扩展（P0）**。原先 `openssl req -x509` 生成的 CA
  只有 `basicConstraints`、没有 `keyUsage`。OpenSSL ≤ 3.0 容忍，**3.5+ 会直接报
  `CA cert does not include key usage extension` 拒绝校验** —— 也就是说在
  Python 3.13 / OpenSSL 3.6 这类较新运行时上，**所有经hublane 的 HTTPS 都会
  因证书不可信而失败**。现在 CA 与叶证书都显式带
  `basicConstraints` / `keyUsage` / `subjectKeyIdentifier`（叶证书另加
  `keyUsage`），`hublane.py` / `install.sh` / `install-windows.bat` 三处保持一致，
  安装时还会用 `openssl verify` 自校验，失败即中止安装。
  新增 `TestCaExtensions` 守住这条不变量（原先只有 `TestCertSan` 校验 SAN）。

  > **升级必读**：0.1.0 之前装过的实例必须手动换一次 CA —— `--renew-certs` 只换叶
  > 证书、按设计保留旧 CA，而坏的是 CA 本身，所以它修不了这个问题。
  > 操作步骤见 `docs/TROUBLESHOOTING.md` 第 2b 节（`--renew-ca` + 重新安装信任，
  > 或直接 `uninstall.sh --purge` 后重装）。
- CA 自签改用 `CSR + x509 -signkey` 而非 `req -x509`：`req -x509` 不接受
  `-extfile`，只能退回 `-addext`；而 **`-addext` 会让 openssl 载入配置/provider，
  在 PyInstaller 冻结产物里调用会导致 SIGSEGV**（实测），正好卡住"免安装 exe"这条路。
- **带凭证 / git 写操作的请求不再交给第三方镜像（P0）**。此前 hublane 对
  github.com 的所有请求一视同仁地按 `github_upstreams` 逐个试，而该链把公开镜像
  （`ghproxy_com_gh` / `ghfast_gh` / `ghproxy_net_gh`）排在最前。问题在于
  `Authorization` 是端到端头，不在 `_HOP_HEADERS` 里，会被 `flat_headers()` 原样
  保留并转发 —— 只要经 hublane 发过带 token 的请求（`git push`，或任何携带
  `Authorization` 的 api.github.com 调用），**账号凭证就已经明文交给了第三方镜像的
  运营方**。功能上它同样注定失败：公共镜像只能匿名只读，没有权限代表你写 GitHub，
  所以 `git push` 一律拿到 `401 Unauthorized`，最终表现为所有上游失败后返回 502。
  现在新增 `is_sensitive()` 与 `restrict_upstreams()`：识别到凭证或 `git-receive-pack`
  时剔除链里的全部第三方镜像，只走 `direct` / `watt` / `chain`；上游全是第三方时
  兜底 `direct` —— 宁可直连失败也不把凭证交出去。匿名的 `git fetch` / `clone`
  不受影响，仍优先走镜像，加速收益完整保留。新增 `TestCredentialAwareChain` 守住这条。

  > **升级必读**：若你曾通过 hublane 使用过 GitHub 个人访问令牌（PAT），建议到
  > GitHub Settings → Developer settings 里 **revoke 该令牌并重新签发**。旧令牌
  > 可能已被第三方镜像运营方获取；本修复只能阻止后续泄漏，无法追回已发出的凭证。

### Documentation

- README×2 排障章节、`docs/TROUBLESHOOTING.md` 第 9 章（Windows 专项）、
  第 10 章（计数口径）
- 补充说明「`git push` 应直连或改用 SSH」：hublane 的价值在于匿名只读流量的加速，
  写操作不在其设计范围内（理由见上面的 Security 条目）

### Known issues

- 免安装 exe 只在 Linux 上验证过冻结与证书链路（`tools/build_exe.sh --smoke` 通过）；
  **Windows 产物的体积、杀软误报、`--service` 是否可用仍未验证**，需在干净
  Windows 上手工跑一遍。评估见 `docs/EVALUATIONS.md` 第 1 节。
- `make package` 与 `release.yml` 里的 `git archive` 产物
  （`hublane-<ver>.tar.gz`）会被随后 `python -m build` 生成的同名 sdist 覆盖，
  该步骤实际是死代码，待下次发版时统一命名或删除。
- 从源码目录直接运行 `hublane.py` 时，证书会生成在 `hublane.py` 所在目录
  （已被 `.gitignore` 忽略）。正常安装路径是复制到 `/opt/hublane`，不受影响。
- v0.2.0 计划中"文档承诺但代码没做"的条目仍未做：`enable_socks5` 未真正生效、
  `verbose` 是死配置键、安装脚本未透传 `--renew-*`、Firefox `policies.json`
  未实现、覆盖率无 `--fail-under` 门禁。

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
