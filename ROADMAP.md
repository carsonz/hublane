# hublane 路线图 / Roadmap

状态：`[x]` 已完成 · `[ ]` 计划中 · `[A]` 待评估（评估通过才排期）· `[-]` 明确不做

版本节奏：SemVer（0.x 期间 minor 加功能、patch 修 bug）。每个里程碑发布前门禁 =
测试全绿 + flake8（复杂度 ≤15）+ 文档（README×2 / ROADMAP / CHANGELOG）同步 +
版本一致性（`hublane.py` 与 `pyproject.toml` 同时改，`tools/check_version.py` 校验）。

> **v0.1.0 是首个发布版本。** 在此之前的全部改动都只经过本地人工验证、从未打过 tag，
> 因此原先分散记录的 P0–P3 与各里程碑条目合并为 **0.1.0 一次性交付**；
> 没做完的部分与新需求一律排到 0.2.0 及以后。逐条变更见 `CHANGELOG.md`。

---

## v0.1.0 已完成

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

## v0.1.1 已完成

主题：**"把 0.1.0 的 Windows 安装/打包在真实管理员会话里跑通，修掉实测才暴露的坑"**。
本版本所有 Windows 相关改动均在 Windows 11 + 管理员权限下实测验证。

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

## v0.2.0 —— 分发补齐与实现收口

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
  - **在 Linux 上已把这条路的两个隐藏阻塞点修掉并固化**
    （见 `CHANGELOG.md` Unreleased）：冻结形态 `INSTALL_DIR` 落到 `_MEI` 临时目录
    导致证书每次重生成；`openssl req -addext` 在冻结产物里 SIGSEGV。
    打包脚本 `tools/build_exe.sh --smoke` 已能在 Linux 上跑通"打包 → 生成证书 →
    校验证书链"全流程。
  - 剩余前置：需在干净 Windows 上手工跑通 —— 体积/杀软误报、证书生成对
    `openssl.exe` 的依赖（用户没装 Git 时的兜底）、`--service` 打包后是否可用。
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

## v0.2.1 —— P0 缺陷修复（补丁版，最高优先）

来源全部是**代码审计**。这一版只修"会导致错误行为或安全问题"的项，不加功能。
之所以单列补丁版而不是塞进 0.3.0：这些都是**已发布代码里的真实缺陷**，拖到下一个
minor 会让 0.2.0 的用户一直踩。

- [ ] **2.1.1. 上游返回 4xx/5xx 被误判为"上游故障"**（`_exchange` hublane.py:1408）
  - 现象：`resp.status >= 400` 就 `raise`，`relay_chain` 随即 `_u_fail(cooldown=600)`。
    于是一次 **404/401**（最常见的是 `git push` 拿到 401、或某个镜像真的没有该文件）
    会让链上**每个**上游 `fails+1`，三次后冷却 600s —— 正常的流量被拖慢，
    而且客户端拿到的是 502，**真实的 404 状态码丢失了**。
  - 修：区分"连不通/超时/截断"（该降级）与"拿到了完整的 4xx/5xx"（直接转发给客户端，
    不记失败、不冷却）。

- [ ] **2.1.2. `Cookie` 等敏感头会原样转发到第三方镜像**（`is_sensitive` :1614）
  - 现状只认 `authorization` 与 `git-receive-pack`；`_HOP_HEADERS` 不含 `cookie`。
    已登录的会话 Cookie 会被送到 gh-proxy.com 一类镜像运营方。
  - 这与 0.1.1 的 Security 承诺（"第三方镜像只承载匿名只读流量"）**直接冲突**，属承诺未兑现。
  - 修：把 `cookie` / `x-csrf-token` / `private-token` 纳入 `is_sensitive`，转发前剔除。

- [ ] **2.1.3. 指标面板可能无鉴权暴露到非回环地址**（`_start_metrics` :3399）
  - `metrics_host` 为空时**回落到 `listen_host`**，而 `validate_config` :663 只在
    `metrics_host` 非空时才要求 token。于是 `listen_host=0.0.0.0` + 默认空 token
    ⇒ `/diag`（配置摘要 + 日志尾部）对整个局域网裸奔。
  - 修：把**回落后的实际绑定地址**纳入鉴权校验，非回环即强制要求 `metrics_token`。

- [ ] **2.1.4. 叶证书 SAN 不含用户在 config.json 里新增的域名**（`leaf_san` :124）
  - `leaf_san` 读的是 `DEFAULTS["extra_hosts"]`，而接管判定 `should_intercept` 用的是 `CONFIG`。
    用户在配置里加一个 `extra_hosts` ⇒ 该域名被 MITM 但**证书里没有对应 SAN** ⇒
    浏览器直接报证书名不匹配。这是"用户按文档做了一次常规配置就中招"的缺陷。
  - 修：改读 `CONFIG`（证书生成前确保已 `load_config`），并补一条 SAN 与受管域名一致的单测。

- [ ] **2.1.5. 面板 POST 端点无 CSRF 防护且默认无鉴权**（`MetricsHandler.do_POST`）
  - `/reload` `/pause` `/resume` 是标准表单 POST，`metrics_token` 默认为空 ⇒
    用户访问任意网页即可被静默暂停代理或触发重载（DNS rebinding / CSRF 场景）。
  - 修：至少校验 `Origin`/`Referer` 同源，或要求一个自定义头（浏览器表单发不出自定义头）。

- [ ] **2.1.6. `validate_config` 自身会抛异常**，违背"启动即校验"的初衷
  - `_validate_ports` 末尾 `int(cfg.get("listen_port", 0)) == int(...)` 在 try 之外：
    端口写成 `"8899"` 或非数字时抛 ValueError，表现为 traceback 而不是友好报错；
    `/reload` 也会因此 500。
  - 修：先做一次安全转换再比较。

- [ ] **2.1.7. 热重载时 `CONFIG.clear()` + `update()` 非原子**（`reload_config` :519）
  - 清空与回填之间有窗口，并发读配置（`CONFIG["raw_upstreams"]` 等裸下标）会 KeyError。
  - 修：先构造完整 dict，再整体替换全局绑定。

---

## v0.3.0 —— 稳健性收口 + 能力补齐

前半（3.1–3.11）来自**代码审计 P1**，后半（3.12–3.16）是**外部需求**与**自主判断**的新能力。

### 代码审计遗留（P1）

- [ ] **3.1. `refresh_interval=0` 会让刷新线程 100% CPU 自旋**（`_refresher` :1100）
  - 修：`interval = max(30, ...)`，并在 `--check` 里约束取值。
- [ ] **3.2. `_verify_ip` 握手失败时不关闭 socket，描述符泄漏**（:998）
  - 验真失败是**常态路径**（换网络、IP 失效），泄漏会累积。修：`finally: raw.close()`。
- [ ] **3.3. `_accept_loop` 遇任意 `OSError` 就 break，整个代理停止**（:3375）
  - EMFILE / ECONNABORTED 这类瞬时错误会让监听线程永久退出且无人拉起。
    修：非致命错误计数后 continue，连续失败才退出。
- [ ] **3.4. `refresh_now` 无重入保护**（:2489）
  - 每次 `POST /refresh` 无条件起一个全量线程，连点几次就与后台 `_probe_all`/`_refresher`
    撞车，DoH 与验真流量成倍放大。修：用 in-flight 标志拒绝重入（与 `_warming` 同思路）。
- [ ] **3.5. `client_timeout` 贯穿整个连接生命周期**（:2240）
  - 本意是"防慢连接占住线程"，但设上 60s 后 `serve_mitm`/`tunnel` 都继承它，
    大响应或慢客户端会被硬切。修：读完请求头后恢复 `settimeout(None)`。
- [ ] **3.6. 隧道统计口径失真**（:1894/:1912）
  - `select(...,180)` 与任何配置项都无关；`finally` 里**无条件**记 `result="ok"`，
    中途断流既不记 `tunnel_fail` 也显示成功。修：区分正常结束与异常，超时改为可配。
- [ ] **3.7. 响应头已发出后仍写第二个响应头**（`relay_chain` :2002 + `serve_mitm` :2041）
  - 与注释"主动断开以暴露截断"自相矛盾。修：committed 时直接 return（关闭连接）。
- [ ] **3.8. 请求样本泄漏完整查询串**（`sample_add` :1981 / `/requests` :2979）
  - URL 里的 `?access_token=` 会原样进面板；`_redact_query` 目前只用于日志。
    修：写入样本前同样脱敏。
- [ ] **3.9. `open_mirror` 的 urllib 响应从不关闭**（:1487）
  - 成功与失败路径都缺 `resp.close()`，且不入池。修：`try/finally` 显式关闭。
- [ ] **3.10. 超时语义不一致**（:1418）
  - `direct` 把 `min(timeout, direct_timeout)` 套在**整次请求**（含读响应体）上，
    而 watt/chain/mirror 用完整 60s。修：`direct_timeout` 只管建连+首字节，读体用 `timeout`。
- [ ] **3.11. 后台线程异常被全量吞掉**（`_refresher`/`_warm_async`/`refresh_now`）
  - 全是 `except Exception: pass`，DNS 或验真长期失败在面板上完全不可见 ——
    用户只会看到"没加速效果"，看不出原因。修：至少 `log.debug` 并加一个 `refresh_error` 计数。

### 新能力（外部需求 / 自主判断）

- [ ] **3.12. 大文件专项：Git LFS / docker layer / HF 权重**【外部需求】
  - 真实痛点集中在"大而不可断点"的下载：HF 模型走 `cdn-lfs`、docker 走 registry、
    `git lfs pull`。当前只有通用的流式转发与完整性校验，**没有断点续传与 Range 支持**。
  - 范围：支持客户端 `Range` 请求透传；大响应强制流式且不入 `integrity_buffer_max`；
    给这类域名一个`large_file` 标记，默认建议走 `chain`。
  - 依据：多篇 2026 年的国内加速指南把"HF 权重 / Git LFS / docker pull / Ollama"
    并列为首要痛点，而 hublane 目前对这些是"能通但慢且断不了点"。
- [ ] **3.13. 规则远程订阅**【外部需求】
  - `upstream_list_url` 已有雏形，但只拉上游清单。扩展成规则集订阅（域名/路径/上游链），
    与本地 `config.json` 合并、带 ETag 增量更新。
  - 依据：Clash/Mihomo 的 rule-provider 是这类工具的事实标准，用户对"不用改配置就能更新规则"
    有明确预期。
- [ ] **3.14. 包管理器覆盖补全**【外部需求】
  - 现有 `extra_host_groups` 覆盖了 Python/npm/Go/Rust/Conda，缺口是
    **Maven / Gradle / CocoaPods / apt / Homebrew / Ollama**。
  - 做法：先按 `docs/MIRRORS.md` 的既有流程**实测入库**（不实测不写入），再开分组。
- [ ] **3.15. 系统代理的可靠设置与恢复**【自主判断】
  - 现状：`install.sh` 只注 shell 变量；Windows 侧设系统代理但**崩溃/关机后可能残留**
    （dev-sidecar 的用户抱怨里这一条常驻）。
  - 范围：退出/崩溃时恢复系统代理的兜底（Windows 计划任务 + Linux systemd `ExecStopPost`），
    并给一个 `--restore-proxy` 手动恢复入口。
- [ ] **3.16. `hublane --doctor` 一键自检**【自主判断】
  - 把排障手册里的判据自动化：本机是否能直连、CA 是否装好、端口是否被占、
    DoH 是否可用、上游链是否至少有一个能通、代理变量是否生效。
  - 价值：把"用户看不出为什么没效果"变成一条命令给出结论，直接降低 issue 量。

---

## v0.4.0 —— 优化、重构与体验

来源以**代码审计 P2**为主，加上自主判断的体验项。这一版的目标是"让代码更好改"，
用户可感知的变化少，但为后面几个版本减负。

- [ ] **4.1. 证书生成三处实现合一**
  - `hublane.py` 的 `_openssl_ca`/`_openssl_leaf` 与 `install.sh` 各写一份（注释自承
    "测试会校验三处一致"）。修：安装脚本改为直接调 `hublane.py --renew-certs`，
    只留一处实现 —— 与"版本号只有一处来源"是同一类治理。
- [ ] **4.2. 明文 `http://` 绝对 URI 不走接管判定**（:2286）
  - CONNECT 会 `should_intercept`，明文 http 直接 `tunnel`，受管域名的 80 端口被悄悄绕过。
    修：两条路径统一走 `should_intercept`。
- [ ] **4.3. `_read_line` 逐字节 `recv(1)`**（:2059）
  - 请求头解析是每个连接的热路径。修：改用 `makefile("rb")` 缓冲读。
- [ ] **4.4. 状态字典只增不删**（`_ipcache`/`_ustat`/`_latency`）
  - 域名从配置移除后永久留存并写进 `state.json`。修：`_refresher` 里按 `managed_hosts()`
    做一次差集清理。
- [ ] **4.5. `direct_cooldown` 被套用到镜像类上游**（:1993/:2006）
  - 命名与语义不符（对 gh-proxy、jsdelivr 也用 600s 长冷却）。修：按上游类型区分，
    或改名 `upstream_cooldown` 并给镜像类一个短得多的默认值。
- [ ] **4.6. `_socks_connect` 不校验 SOCKS 应答码**（:2157）
  - `recv` 结果全丢弃，节点拒绝时仍返回"成功"的 socket，错误被推迟到第一次读才暴露。
    修：检查 VER/REP。
- [ ] **4.7. `panel_html` 拆分**（:2676，约 240 行 HTML 与逻辑混写）
  - 修：HTML/CSS 抽成模块级模板常量（0.3.0 的复评里已认定这是"收益更高的小事"），
    数据拼装与渲染分离。
- [ ] **4.8. 死代码清理**
  - `open_watt` 末尾不可达的 `raise`（:1473）；`leaf_san` 里恒为空操作的集合推导（:129）；
    `EXTRA_LEAF_SAN` 里的 `www.baidu.com` / `opencode.ai`（疑似调试残留）。
- [ ] **4.9. `--update` 的完整性校验加强**（:3526）
  - 现状只做 `len >= 64` 与 `b"def main("` 形状检查，且 tag 未过滤就拼进 URL。
    修：校验 tag 形如 `v\d+\.\d+\.\d+`，并比对 release 资产的 `SHA256SUMS`。
- [ ] **4.10. 并发模式统一**
  - `_warming` 的检查-加入非原子（:1013），与 3.4 的 `refresh_now` 是同一个模式的两处。
    修：统一到一个带锁的 in-flight 集合，别再复制这个易错写法。

---

## v0.5.0 —— 结构治理与长期项（计划中 / 待评估）

- [ ] **5.1. macOS 支持**（`[A]`：无 macOS 机器持续验证前不做）
  - launchd plist + Keychain 信任（`security add-trusted-cert`）+ `networksetup`；
    核心代码已跨平台，差异集中在安装器与信任库。
  - 触发条件：出现明确需求（issue 或自用）时再起，届时可复用现有测试。

- [ ] **5.2. 单文件拆分（源码拆包 + 构建期拼回单文件）**
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

- [ ] **5.3. 覆盖率目标提到 80%**：0.2.0 已立起 70% 门禁，稳定后再抬。
- [ ] **5.4. HTTP/3（QUIC）上游**（`[A]`）：标准库无实现，与"零第三方依赖"冲突；
  仅当出现明确的实测收益（例如某类站点只有 HTTP/3 才能通）才重评，结论进
  `docs/EVALUATIONS.md`。

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

- **TUN / fake-ip 透明代理**。Clash/Mihomo 的 TUN 很强，但那是另一类产品：
  它需要内核态组件（标准库做不到，与"零第三方依赖"冲突），而且会把**全部**流量拉进
  用户态。hublane 坚持"本地代理 + 显式配置"，把影响面控制在可撤销范围内。
- **默认开放局域网共享**。监听地址默认仍是 `127.0.0.1`；用户显式改 `listen_host`
  是支持的，但不做"一键开放给局域网"的引导 —— 那等于把 MITM 能力和自签 CA
  暴露给同网段，而访问控制（`proxy_token`）默认是关的。
- **请求/响应改写脚本引擎**（mitmproxy addon 那种）。灵活，但本质是给用户在
  本机执行任意代码的入口，且会显著膨胀单文件。确有需求时先做**只读的**匹配规则
  —— v0.3.0 的 3.13 规则订阅已覆盖大多数场景。

---

## 优先级判据（下一批先做哪个的依据）

1. **先做 v0.2.1 的 P0**。它们不是"改进"，是**已发布代码里的真实缺陷**：
   其中 2.1.1（4xx 误判）、2.1.3（面板可能裸奔）、2.1.4（加了 extra_hosts 就证书报错）
   是用户**正常使用就会踩**的。缺陷修复属于 patch，不该等 minor。
2. **再做 v0.3.0 前段的 P1**。这一档的共同点是"平时不出事、出事就说不清"：
   fd 泄漏、线程自旋、异常被吞、统计口径失真 —— 它们直接决定排障难度。
3. **v0.3.0 后段的新能力按 issue 需求排**，不为"看起来完整"而做。
   其中 3.12（大文件）与 3.16（`--doctor`）是自主判断里性价比最高的两条：
   一个对准最痛的下载场景，一个直接减少 issue。
4. **v0.4.0 是减负版**：用户几乎感知不到，但它决定后面几个版本改起来有多痛
   （尤其 4.1 证书生成合一，属"同一件事只有一处实现"的治理）。
5. **需要 Windows 验证的项单列**，不阻塞其它版本推进（见 v0.2.0 末尾的清单）。
6. `[A]` 项必须先写一页评估结论（成本 / 收益 / 替代方案）再决定是否排期，
   结论写进 `docs/EVALUATIONS.md`。

## 版本排布一览

| 版本 | 主题 | 主要来源 |
|---|---|---|
| **0.2.1** | P0 缺陷修复（补丁） | 代码审计 |
| **0.3.0** | 稳健性收口 + 能力补齐 | 代码审计 P1 + 外部需求 + 自主判断 |
| **0.4.0** | 优化、重构与体验 | 代码审计 P2 + 自主判断 |
| **0.5.0** | 结构治理与长期项 | 原 v0.3.0（macOS / 单文件拆分 / 覆盖率 / HTTP3） |