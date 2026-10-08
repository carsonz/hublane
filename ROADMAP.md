# hublane 路线图 / Roadmap

状态：`[x]` 已完成 · `[ ]` 计划中 · `[A]` 待评估（评估通过才排期）· `[-]` 明确不做

版本节奏：SemVer（0.x 期间 minor 加功能、patch 修 bug）。每个里程碑发布前门禁 =
测试全绿 + flake8（复杂度 ≤15）+ 文档（README×2 / ROADMAP / CHANGELOG）同步 +
版本一致性（`hublane.py` 与 `pyproject.toml` 同时改，`tools/check_version.py` 校验）。

> **v0.1.0 是首个发布版本。** 在此之前的全部改动都只经过本地人工验证、从未打过 tag，
> 因此原先分散记录的 P0–P3 与各里程碑条目合并为 **0.1.0 一次性交付**；
> 没做完的部分与新需求一律排到 0.2.0 及以后。逐条变更见 `CHANGELOG.md`。

---

## v0.1.0 已完成（首个版本，2026-10-07）

主题：**"让 GitHub 在受限网络下稳定可用，并且看得见、管得住"**。

### 转发内核

- [x] **流式转发**：响应与请求体都走流式，大响应与大 POST/PUT 不整体缓冲进内存；
  chunked 请求体逐块解析。
  → `hublane.py: BodyStream / build_body / stream_body / relay_response`
- [x] **单端口双协议**：`127.0.0.1:8899` 上按首字节判别 HTTP 与 SOCKS5；
  受管域名终止 TLS 后走上游链，非受管域名走纯 TCP 隧道（不解密、不改写）。
  → `hublane.py: handle_client / handle_socks5 / tunnel`
- [x] **SSH(22) 转发**：git-over-SSH 场景经 `chain_socks_port` 走节点 SOCKS5。
- [x] **IPv6 与多路径解析**：`enable_ipv6` 控制 A/AAAA 双查；启动后台线程预热 DNS，
  首请求不等 DoH。
  → `hublane.py: _warm_async / pick_ips`
- [x] **PAC 自动代理配置**：`/pac` 输出精确 + 通配规则，浏览器/PAC 客户端零配置。
  → `hublane.py: pac_content`

### 上游选择与健康度

- [x] **四种上游类型**：`direct`（DoH 多路径 + 并发真实 TLS 握手验真，连最快 IP）、
  `watt`（Windows 侧 Watt Toolkit）、`chain`（自有节点 Clash/v2rayN/sing-box）、
  镜像站（前缀式 / 域名替换式 / CDN 三类 `OPENERS`）。
- [x] **raw 镜像库**：15 个前缀式 + 域名替换式 + CDN，默认链自动追加尾部镜像；
  用户可用 `custom_mirrors` 插拔、用 `upstream_list_url` 拉远程清单。
  → `hublane.py: MIRROR_PREFIX / JSDELIVR_HOSTS / _known_upstreams`
- [x] **非 GitHub 站点整站换源**：HuggingFace→hf-mirror、PyPI→清华/阿里、
  npm→npmmirror、Go→goproxy.cn、Conda→清华、Crates→rsproxy、nodejs→npmmirror、
  jsDelivr→fastly/jsd、字体→`fonts.googleapis.cn` + `gstatic.cn`（必须成对替换）。
  联网实测后才入库，记录见 `docs/MIRRORS.md`。
  → `hublane.py: SITE_MIRRORS / SITE_MIRROR_STRIP / BUILTIN_PER_HOST_UPSTREAMS`
- [x] **按域名的上游链**：`per_host_upstreams` 支持精确域名与 `*.example.com`
  （最长后缀优先），出现过的域名自动视为受管域名，PAC 同步输出通配规则。
  → `hublane.py: per_host_chain / host_matches`
- [x] **境外站点分组库**：`extra_host_groups` 内置 11 组（字体 / Pages / 容器 /
  工具链 / Git 托管 / AI 模型 / Python / npm / Go / Rust / Conda），
  `extra_host_groups_enabled` 整组开关（默认只开 `fonts_cdn`）。
- [x] **自适应排序**：EWMA × 成功率（最近 20 次滑动窗口），失败冷却，
  样本不足视为"未证明"；状态持久化到 `state.json`，旧格式（无 `hist`）向后兼容。
  → `hublane.py: success_rate / _u_score`
- [x] **上游连接池**：复用前探活（TLS 连接单独处理，`SSL_read` 不能 PEEK），
  探活仍失败时用新连接透明重试一次；每 key 多条 + 全局 LRU 容量上限。
  → `hublane.py: sock_alive / _pool_get / pool_max_per_key / pool_max_total`
- [x] **DoH 端点可插拔**：支持 URL 模板或 `{"url","name","enabled"}`；按配置顺序取
  前 2 个并发查询；端点级成功/失败/EWMA/冷却持久化并展示，连续失败自动下沉。
  → `hublane.py: doh_endpoints / doh_ordered / _doh_query / doh_probe`
- [x] **全链路主动探测**：探测目标覆盖 raw 镜像、`github.com` 与 extra 站点代表路径，
  与真实流量共用同一份健康度（探测 scope 必须是真实域名，否则探测数据不参与排序）。
  → `hublane.py: _probe_all / probe_extra_max`
- [x] **响应完整性校验**：已知 `Content-Length` 且 ≤ `integrity_buffer_max`（默认 1 MiB）
  的响应先整体校验再转发 → 截断可**透明回落到下一个上游**；大响应保持流式，
  截断时不补 chunked 结束标记并主动断开，避免"残缺即完整"。
  → `hublane.py: relay_response / TruncatedError`

### 健壮性与安全基线

- [x] **客户端连接治理**：`client_timeout`（默认 60s，防 slowloris 占住线程）、
  `max_conns`（默认 256，超限直接关闭并计 `counters.rejected`）；限流按监听实例隔离。
- [x] **本地访问控制**：`proxy_token`（HTTP 侧校验 `Proxy-Authorization`，失败 407；
  SOCKS5 侧启用 method 0x02，失败分别 0xFF / 0x01）；Linux/WSL 另有
  `proxy_uid_whitelist`（`SO_PEERCRED` 校验发起方 uid）。未配置时行为与无鉴权版一致。
- [x] **证书生命周期**：CA 与叶证书在安装时本地生成（**私钥从不入库**）；
  SAN 由 `leaf_san()` 从域名库推导（GitHub 系 + 默认 extra_hosts + 所有分组 +
  内置链，约 90 条），新增站点不会漏证书名；启动与面板显示剩余天数（<90 天告警）；
  `--renew-certs` 只换叶证书（免重装信任）、`--renew-ca` 连 CA 换。
  → `hublane.py: leaf_san / cert_days_left`
- [x] **Windows 真服务**：`--service` 用 ctypes 注册 SCM 处理函数（`sc stop` 优雅退出），
  配 `install-windows-service.bat` / `uninstall-windows-service.bat`
  （`sc create start= auto` + `sc failure` 崩溃自愈 + 停用原计划任务）；
  日志目录不可写时回退临时目录。
- [x] **配置校验**：`hublane.py --check` 只校验配置即退出（部署前门禁，CI 也跑）；
  `validate_config()` 拒绝未知键与非法取值；`--config` 支持自定义配置路径。
- [x] **内部请求不绕回自己**：DoH / 校验 / 镜像请求走内部 opener，不受 `http_proxy`
  影响而绕回本机。
- [x] **私钥权限告警**：POSIX 下启动时提示私钥文件权限过宽（建议 `chmod 600`）。

### 可观测性与运维

- [x] **HTML 面板 + 指标端点**：根路径为面板（上游与 DoH 健康度表、延迟卡片、
  最近请求表、已校验真实 IP、连接池/活动连接/证书剩余/已启用分组等元信息，5 秒自动刷新）；
  另有 `/status`、`/requests`、`/pac`、`/healthz`、`/diag`。
  → `hublane.py: panel_html / MetricsHandler`
- [x] **指标鉴权**：`metrics_token`（请求头 `X-Hublane-Token` 或 `?token=`）、
  `metrics_host` 绑非本机地址时强制要求 token（否则拒绝启动）；
  `metrics_enabled` 可整体关掉指标端点（失败仅告警，不影响代理转发）。
- [x] **延迟分位数与请求样本**：固定桶直方图按 `域名|上游` 统计 P50/P95/P99；
  最近 N 条请求样本（`sample_size`，默认 200，0 = 关闭）经 `/requests` 输出并在面板展示；
  隐私边界：**仅内存、不落盘、不出本机**。
- [x] **结构化日志**：`log_format: json` 单行 JSON（ts/level/logger/msg/…），
  systemd `journalctl -o json` 与 Windows 服务模式都可用；文件日志轮转 2 MiB × 3。
- [x] **配置热重载**：`SIGHUP` + `POST /reload`（面板有按钮，token 保护）重载
  `config.json`；**先校验再切换**，非法配置保留旧配置并告警；监听端口等结构性参数
  给出"需重启才生效"提示。
- [x] **诊断包**：`/diag` 输出版本/平台/监听/运行时长 + 配置摘要（token 打码）
  + `/status` 全量 + 日志尾部 200 行；issue 模板改为"贴 `/diag` 输出"。
  → `hublane.py: diag_text`

### 安装与分发

- [x] **Linux/WSL 安装器**：systemd 服务 + zsh/bash rc 注入 + deb/rpm 双信任库；
  `-y/--yes`、`--dry-run`、`--skip-verify`、`--help`；WSL 自检（systemd 可用性、
  `resolv.conf` 提示）；systemd 不可用时回退 nohup。
- [x] **Windows 安装器**：Python 探测、本地生成 CA + 叶证书、certutil 装信任、
  系统代理、自愈计划任务（run-loop + 隐藏 vbs）、
  Firefox 需单独导入 CA 的提示（Firefox 不用系统证书库）。
- [x] **卸载器**：`uninstall.sh`（`--purge` + shell rc 备份）、`uninstall-windows.bat`、
  `uninstall-windows-service.bat`。
- [x] **下载即用**：Release 直附 `hublane.py` 单文件 + `config.json` + 安装/卸载脚本，
  另附源码 tar.gz、sdist/wheel、`SHA256SUMS` 与 GitHub artifact attestation。
- [x] **发布流程**：tag 驱动（`.github/workflows/release.yml`）—— 版本一致性门禁 →
  flake8 + 测试 → 打包 → `SHA256SUMS` → 用 `tools/changelog_section.py` 从
  `CHANGELOG.md` 取对应段落作 Release 说明。
- [x] **质量门禁**：CI 矩阵（ubuntu/windows × py3.9–3.13）跑 flake8、配置校验、
  单元 + 集成测试、版本一致性、覆盖率报告与本地压测（压测仅提示不阻断）；
  CodeQL 工作流、dependabot、PR/issue 模板、`setup.cfg`（flake8 规则）、`Makefile`。
- [x] **文档**：`docs/ARCHITECTURE.md`（时序图 / 模块地图 / 线程模型 / 落盘与内存边界 /
  安全模型 / 容易踩的坑）、`docs/MIRRORS.md`（镜像与站点库实测记录与复测方法）、
  `docs/TROUBLESHOOTING.md`、`docs/EVALUATIONS.md`（`[A]` 项结论）。
- [x] **压测基线**：`bench/bench_relay.py`（本地假上游 + 进程内 hublane + 真 TLS 客户端）
  输出 rps/P50/P95/失败数，`--save` 存基线、`--check` 与基线对比
  （P95 超基线 1.5 倍判回归）；已采集 `bench/baseline.json`（约 300 rps / P95 34ms）。
- [x] **测试**：`tests/test_hublane.py`（纯逻辑，含 `TestCertSan` 校验三处 SAN 一致）
  + `tests/test_integration.py`（本地假上游 + 真 TLS 客户端，**不需要外网**，
  缺 `openssl` 时自动跳过）。

---

## v0.1.1 已完成（Windows 实测收口，2026-10-08）

主题：**"把 0.1.0 的 Windows 安装/打包在真实管理员会话里跑通，修掉实测才暴露的坑"**。
本版本所有 Windows 相关改动均在 Windows 11 + 管理员权限下实测验证（含崩溃自愈与
优雅停止），不是"跑通就行"，是真跑出来的问题。

### 新增功能（Added）

- [x] **Windows 依赖引导脚本** `tools/setup-windows-env.ps1`：用 winget 自动补齐
  Python（指定版本）、OpenSSL，并建好 `.venv` + 安装 pyinstaller；幂等、带失败汇总。
- [x] **PyInstaller 单文件 exe 打包** `tools/build-exe.py`：把 hublane 打成免 Python 的
  `dist/hublane.exe`（此前不存在）。关键修复：onefile 下 `__file__` 指向每次运行都被
  清空的 `_MEIPASS`，改为以 `sys.executable` 所在目录为数据根 + `seed_bundled()` 首次
  播种内置 config/证书，证书/配置/状态/日志不再随临时目录消失。
- [x] **管理员级实测工具** `tools/verify-windows.ps1`：提权后跑通"计划任务模式 +
  服务模式"全流程，覆盖 `schtasks` 建任务、`sc create/start/stop`、崩溃自愈（强杀后
  SCM 自动拉起）、优雅停止（端口释放 + 无残留进程），共 45 项断言、自带清理。
- [x] **`.gitattributes`**：固定 `.sh`=LF / `.bat`·`.ps1`=CRLF，杜绝 Windows↔WSL 互相
  把脚本存错换行符（曾导致 WSL 下 `bad interpreter: bash^M`）。
- [x] **面板新增隧道维度**：纯 TCP 隧道（非托管域名 CONNECT / 明文 http / SSH）单独计
  `隧道连接 / 隧道失败 / 隧道字节`，不再隐形于 HTTP 计数；每张卡片加 `title` 悬停说明口径。

### 优化（Changed / Performance）

- [x] **`tunnel()` 流量纳入统计**：此前 relay 之外的纯 TCP 隧道完全不计数，面板严重
  低报。新增独立维度，且隧道耗时**不进延迟直方图**（隧道可能活数分钟，会带偏 P95）。
- [x] **`main()` 复杂度收口**：抽 `_start_metrics()`，复杂度由 16 降到 ≤15，CI 的
  flake8 C901 门禁由红转绿。
- [x] **`_win_service()` 可测化**：约 81 行 ctypes 代码原为零覆盖，抽出不依赖 ctypes 的
  `ServiceCore` 状态机（控制分发 / STOP_PENDING 上报 / 等待提示），通过可注入 backend 在
  Linux CI 上单测（含 stop / shutdown / interrogate）。
- [x] **字节计数自动换算**：新增 `_fmt_bytes()`，面板 `字节 / 隧道字节` 显示为 KB/MB。
- [x] **flake8 全面转绿**：补 9 处 E306 空行 + 2 处续行缩进 + C901，CI 此前一直 fail。

### 改 bug（Fixed）

- [x] **CA 缺 `keyUsage` 导致 Python 3.14 握手失败**：`gen_certs()` 生成的 CA 只有
  `Basic Constraints` 没有 `keyUsage`，违反 RFC 5280，OpenSSL 3.5 / Python 3.14 直接
  拒绝（`CA cert does not include key usage extension`）。已在 `hublane.py` /
  `install.sh` / `install-windows.bat` / `test_integration.py` 统一补 `-addext`，
  首次运行 15 个测试挂掉的问题消除，现 148 → **165** 全绿（3.12/3.13 不受影响，一直埋着）。
- [x] **`pip install hublane` 装不上**：`pyproject.toml` 同时写 `license="MIT"` 与
  `License ::` 分类器，违反 PEP 639，被自身要求的 `setuptools>=77` 拒。删掉分类器。
- [x] **`.bat` 在 cmd 下解析崩溃**：`echo` 行同时含中文与 ASCII 括号时，cmd 的 DBCS 解码
  会把 `)` 吞掉、括号块失衡（`xxx was unexpected at this time.`）。`install-windows.bat`
  的 `[4/6]` CA 块、两处安装脚本的错误提示块、卸载脚本的单行 `if/else` 都崩 —— 恰好是
  用户最需要看提示时。全量改写（标签跳转 + 大扫除 14 处）。
- [x] **`run-loop.bat` 写 `py` 而非绝对路径**：计划任务/Run 环境常无 `py`，静默陷入
  3 秒死循环。改为解析绝对解释器路径（服务脚本本就对的，安装脚本漏了）。
- [x] **服务模式留下"影子实例"**：`install-windows-service.bat` 的 `[3/6]` 用
  `schtasks /end` 收不掉 `run-loop` 的子进程，而 `run-loop` 是 `goto loop` 死循环会重启；
  Windows 的 `SO_REUSEADDR` 允许两个套接字绑同端口，于是服务与旧实例并存、请求被分走、
  `sc stop` 后端口看似永不释放。改为按命令行杀整个进程树 + 按端口兜底。`uninstall-windows.bat`
  同步修复。
- [x] **指标面板（28898）从不显式关闭**：`main()` 返回到解释器退出之间端口仍占。补成对
  关闭的 `_stop_metrics()`。
- [x] **安装脚本 / 打包工具中文乱码**：`install-windows.bat`、`install-windows-service.bat`
  加 `chcp 65001`；`build-exe.py` 对 stdout 做 `reconfigure(utf-8)`，重定向输出不再乱码。
- [x] **面板计数口径歧义**：`fail` 数的是"单次上游尝试"，会大于 `requests`，原并列展示
  易被误读成 bug。改为"上游成功 / 上游失败"并加悬停说明。

### 文档（Documentation）

- [x] **README × 2**：新增"排障"章节（Windows 实测确认的坑：schannel 吊销检查需
  `--ssl-no-revoke`、CA 缺 keyUsage、`.bat` 括号解析、CRLF、run-loop 死循环等）。
- [x] **`docs/TROUBLESHOOTING.md`**：新增第 9 章 Windows 专项（9a–9h）、第 10 章面板
  计数口径；每条均 Windows 实测复现。
- [x] 版本号同步到 `0.1.1`（`hublane.py` + `pyproject.toml`），`tools/check_version.py` 校验通过。

---

## v0.2.0 —— 分发补齐与实现收口（已实现，待 Windows 验证）

主题：**"把承诺过但没兑现的项清干净，再让别人不用折腾就能装上"**。
0.1.0 已把核心做完，剩下的是收口与最后一公里。

- [x] **1. 收口实现与文档不一致**（0.1.0 文档里写了、代码里没兑现的项，优先做）
  - `enable_socks5` 目前只用于面板与 `/status` 展示，`handle_client` 始终嗅探 SOCKS5
    —— 要么让它真正生效，要么从配置与文档中删掉。
  - `verbose` 写在 `DEFAULTS` 里但全代码无读取点 —— 接上或删除。
  - 安装脚本没有透传 `--renew-certs` / `--renew-ca`（此前路线图写的"透传 `--renew``
    未实现，续期只能手敲命令）。
  - Firefox 只 `echo` 提示手动导入 CA，`policies.json`
    （`security.enterprise_roots`）未实现。
  - 覆盖率只出报告，没有 `--fail-under` 门禁，也没有在 PR 上展示。
  - 验收：每条要么有代码与测试，要么从文档中删除；不再出现"文档承诺、代码没有"。

- [x] **1b. 把新增的验证脚本接进门禁**
  - `tools/verify_install_sandbox.sh` 与 `tests/test_runtime_e2e.py` 目前只能手动跑，
    应接进 CI（Windows 上跳过 install.sh 沙箱即可），否则重演"安装脚本从未被测过"。
  - 覆盖率门禁 `--fail-under 70`（当前 72%）随第 1 条一起立起来。

- [ ] **2. Windows 免 Python 单文件 exe**（PyInstaller，`[A]` 评估结论见
  `docs/EVALUATIONS.md`：方案成立）
  - **2026-10-08 在 Linux 上已把这条路的两个隐藏阻塞点修掉并固化**
    （见 `CHANGELOG.md` Unreleased）：冻结形态 `INSTALL_DIR` 落到 `_MEI` 临时目录
    导致证书每次重生成；`openssl req -addext` 在冻结产物里 SIGSEGV。
    打包脚本 `tools/build_exe.sh --smoke` 已能在 Linux 上跑通"打包 → 生成证书 →
    校验证书链"全流程。
  - 剩余前置：已在 Windows 11 实机跑通（见下方"需 Windows 平台验证"第 1 条）。
  - 若跑通：CI 增加 Windows exe 产物，**P4 Rust/Go 移植正式关闭**（见"明确不做"）。

- [ ] **3. 分发渠道与可信标识**
  - [x] OpenSSF Scorecard 工作流 `.github/workflows/scorecard.yml` + README 徽章。
  - [ ] scoop bucket / winget manifest / Homebrew formula（官方 tap）—— 属 `[A]`，
    评估结论见 `docs/EVALUATIONS.md` 第 8 条：**先只做 winget，且排在 exe 之后**
    （manifest 里要放 exe，exe 没跑通就没意义）。
  - minisign 签名（provenance 已由 attestation 覆盖，签名主要成本在密钥分发）。

- [x] **4. `[A]` 评估待办**：`--check-update`（只提示新版本、不下载，与"不做自动更新"
  不冲突）、分发 manifest 的维护成本评估。

- [x] **5. 动态已验真 IP 池（借鉴 dev-sidecar 的 DNS 优选）**
  - 每次运行：先用 DoH 解析目标域名得到 IP 候选，再并发真实 TLS 握手验真，
    **只有验真成功的 IP 才进入可用列表**；历史运行里本地验真成功过的 IP
    （来自 `direct` 握手记录、用户 `preset_ips`）一并纳入候选池。
  - 列表**每次运行动态更新**并落盘 `state.json`，重启即恢复；随网络环境变化而重新评定
    （这正是"换了网络要能看到新可用清单"的核心诉求）。
  - 与「不改系统 hosts」底线不冲突：这是纯进程内候选池，绝不写 `/etc/hosts`。
  - 验收：新增 IP 池模块 + 单测（"验真失败剔除 / 历史可用 IP 复用 / 落盘恢复"）；
    `--check` 可输出当前 IP 池摘要。

- [x] **6. per_host 规则与 `abort` 表达力（借鉴 dev-sidecar 的 intercepts / abort）**
  - 6a. 在 `config.json` 沉淀一批**合适的默认规则**（host 级链、通配、`abort` 伪上游），
    覆盖"被封且无替代 → 快速失败"的典型域名，避免用户逐个试。
  - 6b. `abort` 伪上游：命中即返回 502 + 单独计数、**不消耗任何超时**；
    解决"某些网站装了代理反而卡（N×timeout 逐个撞墙）"的抱怨。
  - 验收：默认规则进 `config.json` + 单测覆盖 `abort` 命中路径；`--check` 校验规则合法性。

- [x] **7. `GET /hosts` 只读端点**
  - 输出已验真 IP（hosts 格式，受 `metrics_token` 保护），并**明确标注
    "hublane 不写入系统 hosts"**；给用户"只想用 IP"的低门槛出口。
  - 验收：端点单测（格式正确、token 保护、只读）。

- [ ] **8. 分发：补齐 Gitee 镜像仓库的 CI 与 Releases**
  - 现状：`https://gitee.com/carsonz_admin/hublane` 已建库，但**无 CI、无 Releases**；
    GitHub 打 tag **不会**自动同步到 Gitee（GitHub 无法直推 Gitee；Gitee 的"仓库镜像"
    仅周期性同步代码与 tag，**不含 Release 附件**）。
  - 两种做法评估后选用其一（结论写进 `docs/EVALUATIONS.md`）：
    1. Gitee 仓库设为 GitHub 镜像 + 周期同步：成本低，但 Release 附件不在 Gitee；
    2. 扩展发布流程：打 tag 后用 `GITEE_TOKEN` 把 git 推到 Gitee 镜像，
       并调 Gitee API 建 Release、上传 `hublane.py` / `config.json` / 安装脚本等附件。
  - **推荐做法 2**（保住 Release 附件这一"最后一公里"），新增
    `.github/workflows/gitee-sync.yml`，secret 用 `GITEE_TOKEN` / `GITEE_REPO`。
  - 验收：模拟 tag 事件能产出 Gitee Release + 附件；CI 矩阵不退化。
  - 实现（2026-10-08）：已新增 `.github/workflows/gitee-sync.yml`，挂 `release: published`
    事件，下载 GitHub Release 附件后调 Gitee API 建 Release 并上传；推 git 镜像那步因
    Gitee 已设为 GitHub 镜像而设为 `continue-on-error`。待一次真实 tag 验证附件同步。

- [x] **9. 面板"粘贴 URL → 等价命令"小工具（T2-6 低成本版）**
  - 面板加一个输入框（纯字符串变换、不联网）：输入任意 URL，输出
    `curl` / `git clone` / `npm config` / `pip config` 的等价一行命令。
  - 动机：`install.sh` 只给 shell 注入 `http_proxy`，**覆盖不到 Windows 服务模式与
    不走 shell 的 GUI 程序**；给可复制的一行命令是更低成本的覆盖。
  - 验收：变换逻辑单测（不依赖网络）；面板可交互。

- [x] **10. 面板顶部「刷新」（应对切换网络）、「暂停/恢复」按钮**
  - 页面顶部加手动刷新按钮：切换网络后，主动重跑 IP 池评估 + 上游探测 +
    面板数据刷新，而不必等后台 120s 周期或重启进程。
  -「暂停/恢复」可以在不关闭服务的前提下，停止代理或者继续执行代理
  - 验收：按钮触发一次全量重评估；面板 5s 自刷不受影响。

- [x] **11. PAC 有效性验证（避免非代理网址进代理）**
  - 现状：`/pac` 输出精确 + 通配规则；需补**自动化校验**：PAC 命中测试
    （给定一组 URL，断言哪些走代理、哪些直连），确保非受管域名绝不进代理。
  - 验收：新增 PAC 单测（覆盖精确 / 通配 / 负例）；README 补 PAC 命中说明。

- [ ] **12. `update` 功能（除 `--check-update` 提示外，真正能更新）**
  - `--check-update` 只提示新版本（不下载，与"不做自动更新"不冲突）；
  - 新增 `--update`：拉取最新 `hublane.py` / `config.json` / 安装脚本（含校验与回滚），
    更新后提示是否重跑安装器。属 `[A]`：需先评估"从哪拉 / 完整性校验 / 失败回滚"。
  - 验收：dry-run 与真实更新均有单测；校验或签名缺失时拒绝更新。

- [x] **13. 上游健康度表列头可点击排序**
  - 面板「上游健康度」表列名可点击切换升序 / 降序（延迟 / 成功率 / 最近样本等）。
  - 验收：前端排序逻辑单测（或至少手动验证三种排序）。

---

## 需 Windows 平台验证（单独挑出）

以下几项**逻辑已在 Linux 侧实现并有单测覆盖，但必须在真实 Windows 上跑过才算完成** ——
Linux 无法替代验证。验证条件：Windows 11 + 管理员会话。
**2026-10-09 已在 Windows 11 实机（非管理员会话）完成大部分验证**，
实测发现的问题见 `CHANGELOG.md` 0.2.0 的 Fixed 段（面板 JS 语法错误 / 表头被排序挪走 /
ps1 缺 BOM / timeout 落空），全部修复。

- [x] **1. 第 2 条：Windows 免 Python 单文件 exe**（管理员项除外，见下）
  - 实测（Python 3.14 + PyInstaller 6.22）：`tools/build-exe.py` 产出
    `dist/hublane.exe` **9.4 MB**；Defender 自定义扫描无检出（SmartScreen 对未签名
    新 exe 的拦截无法离线模拟，发布后仍需观察）；干净目录首次运行自动播种
    config + 证书，`--check` 通过；`--renew-certs` 在冻结产物里正常（不再复现
    Linux 上的 SIGSEGV）；`--service` 不经 SCM 启动时给出正确提示，证明
    SCM 分发器在冻结形态可用。
  - openssl 兜底：exe 内置构建期证书，首次运行**不需要** openssl；
    无 Git / 无 OpenSSL 时 `find_openssl()` 返回 None、`gen_certs()` 明确报
    "未找到 openssl"（已实测），不静默失败。
  - [ ] 剩余：管理员会话跑 `tools/verify-windows.ps1` 把 exe 注册成服务做
    `sc create/start/stop` 与崩溃自愈全流程。
- [x] **2. Windows 安装脚本缺的两项能力**
  - `install-windows.bat` / `install-windows-service.bat` 已补
    `--renew-certs` / `--renew-ca` 透传（服务脚本在续期后重装信任）与
    Firefox `policies.json`（新脚本 `tools/setup-firefox-policy.ps1`，写入
    `%ProgramFiles%\Mozilla Firefox\distribution\policies.json`，合并已有策略、
    无 Firefox 自动跳过、缺管理员权限时明确提示）。
  - 实测：干跑装置（系统命令换成 exe 桩、`LOCALAPPDATA` 指向沙箱）跑通全部
    7 步 + `[2.5/7]` 续期两档 + 完成横幅，cmd 下无"中文 + ASCII 括号"解析错；
    本机未装 Firefox，验证了正确跳过分支；`--help` 在非管理员会话可用。
    真实写 Program Files / certutil 的分支需管理员会话复核。
- [x] **3. 面板三项 + 暂停/恢复的浏览器实测**
  - Edge（Chromium）无头渲染 + CDP 驱动点击，8 项断言全过：无 JS 错误、
    列头排序升/降序且表头不动、URL 工具生成 curl/git clone、暂停后
    `/status.paused=true` 且出现"恢复"、恢复后 `false`、立即刷新可提交。
    Chrome 未在本机安装（与 Edge 同引擎），如需严格验证可在装有 Chrome 的
    机器上重复。实测暴露并修复了两个渲染层 bug（见 CHANGELOG）。
- [x] **4. `--update` 在 Windows 服务模式下的替换**
  - 实测：Python 启动后不持有 `hublane.py` 的句柄 —— 常驻进程运行中
    覆盖 / 删除 / move-over 替换全部成功，`%LOCALAPPDATA%\hublane\hublane.py`
    在真服务（SYSTEM，pid 存活）运行中也可写。结论：**替换可行，
    但运行中的进程仍是旧代码，必须重启服务才生效**；`--update` 成功消息
    已补充该提示。替换后 `--check` 失败的回滚路径仍由单测覆盖。
- [ ] **5. Gitee 同步工作流的一次真实 tag 验证**
  - `.github/workflows/gitee-sync.yml` 已复核并加固（release id 取不到时
    显式失败并提示排查镜像同步，避免"绿了但没附件"的假成功）；
    仍需打一个真实 tag 走一遍：GitHub Release 附件 → Gitee 同名 Release。
    注意打 tag 会先过 release.yml 的 flake8 + 测试门禁（现已恢复绿）。

---

## v0.3.0 —— 跨平台与结构治理（计划中 / 待评估）

- [ ] **14. macOS 支持**（`[A]`：无 macOS 机器持续验证前不做）
  - launchd plist + Keychain 信任（`security add-trusted-cert`）+ `networksetup`；
    核心代码已跨平台，差异集中在安装器与信任库。
  - 触发条件：出现明确需求（issue 或自用）时再起，届时可复用现有测试。

- [ ] **15. 单文件拆分（源码拆包 + 构建期拼回单文件）**
  - **2026-10-08 复评结论：暂不拆分。** 实测数据（`hublane.py` 2898 行 / 2345 纯代码行
    / 149 个函数 / 130 个顶层定义）：
    - 函数长度分布已经很平 —— ≤10 行 73 个、11–30 行 61 个、31–80 行 12 个、
      81–200 行仅 3 个、**>200 行 0 个**。没有需要拆的"巨型函数"；
    - 模块已有 17 个 `# ---` 分段标记（配置/日志/健康度/DNS/上游/转发/请求解析/
      连接处理/指标/Windows 服务/main），130 个顶层定义命名无冲突；
    - 真正的成本是行数，而行数不等于可维护性 —— 拆包会引入 import 层级，
      并直接威胁"单文件发行"这个产品硬约束（部署形态不变是承诺）。
  - 因此触发条件按实测收紧为：**出现 ≥2 个 200 行以上的函数，或行数到 3.5k，
    或多人同时改这个文件开始冲突**。当前 2898 行、单人维护，未触发。
  - 若将来仍要拆，折中方案：源码拆成 `src/hublane/*.py` 便于维护，
    `tools/build_single.py` 在构建期拼回单文件发行版，测试同时跑两种形态 ——
    **部署形态不变**。
  - 与其等拆包，收益更高的两件小事（可随时做）：
    `panel_html()` 的 HTML/CSS 抽成模块级模板常量（139 行 → 约 15 行），
    以及把 Windows 专属的 `_win_service()`（83 行）单独成文件。

- [ ] **16. 覆盖率目标提到 80%**：0.2.0 先把 70% 门禁立起来，稳定后再抬。

---

## 明确不做

- **内置"免费节点 / 免翻墙"方案**。没有出境节点时 Google / YouTube 无解，
  塞入来路不明的公共镜像既不稳定也不可信。
- **自动修改用户的 hosts 文件**。hosts 是全局且敏感的系统配置，
  本项目坚持"本地代理 + 显式配置"，把影响面控制在可撤销范围内。
- **绕过证书校验**。`direct` 上游坚持做完整证书校验，
  这是区分真实 IP 与污染 IP 的唯一可靠手段。
- **遥测 / 埋点 / 自动更新**。个人代理工具不联网上报任何使用数据；
  更新靠 Release + 用户手动升级（`--check-update` 若需要见 v0.2.0 第 4 条，默认不做）。
- **GUI 客户端（Electron/Qt 等）**。与"零依赖、单文件"冲突；
  HTML 面板 + `/diag` 已覆盖绝大多数运维需求。
- **HTTP/2 上游与压缩透传**（结论见 `docs/EVALUATIONS.md`）：HTTP/2 收益存疑且标准库
  无实现；压缩透传要连带改造完整性校验，收益只是省一点本机回环带宽。
- **Rust / Go 移植**。唯一真实收益是"Windows 免 Python"，已由 v0.2.0 的
  PyInstaller 方案覆盖（评估见 `docs/EVALUATIONS.md`）。

---

## 优先级判据（下一批先做哪个的依据）

1. **先清 v0.2.0 第 1 条**（实现与文档不一致）—— 承诺过却没兑现最伤信任；
2. 免 Python exe：直接决定新用户能否一次装成，但需要 Windows 验证资源，排期受它约束；
3. 其余分发渠道按实际 issue 需求排，不为"看起来完整"而做；
4. `[A]` 项必须先写一页评估结论（成本 / 收益 / 替代方案）再决定是否排期，
   结论写进 `docs/EVALUATIONS.md`。