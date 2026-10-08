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

---

## [Unreleased]

（暂无）

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
