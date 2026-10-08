# 待评估项结论（ROADMAP 的 `[A]` 项）

按 ROADMAP 约定：标 `[A]` 的项必须先写一页评估结论（成本 / 收益 / 替代方案）再决定是否排期。
本文档就是这些结论 —— 改主意时请同步更新 ROADMAP 与这里。

---

## 1. Windows 免 Python 单文件 exe（PyInstaller）

**要解决的问题**：Windows 上"必须先装 Python"是最大的安装门槛；Rust/Go 移植的唯一
真实收益也是这个，而移植成本极高。

**方案**：构建期用 PyInstaller 打包 `hublane.py` → 单个 `hublane.exe`。
构建期依赖不违反"运行时零依赖"（产物自包含，用户侧零依赖）。

**评估**

| 维度 | 结论 |
|---|---|
| 体积 | 预期 8–15 MB（含 Python 解释器与 ssl/ctypes）。**2026-10-08 实测：Python 3.13 下单文件 24 MB**（conda 的 OpenSSL/libpython 一起被打进去） |
| 杀软误报 | **风险点**：PyInstaller 产物常被误报；需代码签名才能缓解，而签名证书要钱 |
| 证书生成 | Windows 通常没有系统 `openssl`，exe 依赖 Git for Windows 的 `openssl.exe`；已在 `find_openssl()` 里覆盖常见路径，但仍需"用户没装 Git"的兜底（预生成模式或随包附带 openssl） |
| 服务模式 | `--service` 用 ctypes 调 SCM，打包后仍可用；需实测 |
| 升级 | 覆盖 exe 即升级，与 `sc stop/start` 配合即可 |
| 成本 | 小（CI 加一条 Windows job，约半天）；**但验证需要一台干净的 Windows 机器** |

**Linux 上的实测结果（2026-10-08，`tools/build_exe.sh --smoke`）**

方案成立，但"跑通 PyInstaller"不等于能用 —— 冻结形态有两个**必须先解决**的坑，
都是实测撞出来的、文档里原本没有的：

1. **`INSTALL_DIR` 落在临时解压目录**。冻结后 `__file__` 在 `_MEIxxxx` 下，进程退出
   即删除；证书/配置/状态写进去等于每次启动全部丢失，CA 也要重新信任（不可用）。
   已修：冻结形态改用稳定目录（`%LOCALAPPDATA%\hublane`，`HUBLANE_HOME` 可覆盖）。
2. **`openssl req -addext` 会让子进程 SIGSEGV**。`-addext` 强制 openssl 载入
   配置/provider，冻结产物里与被打进去的 `libcrypto` 冲突；不带 `-addext` 的同一
   命令正常。**这不是 Linux 独有现象的证明，但它是 `-addext` 这条路走不通的铁证。**
   已修：CA 改用 `CSR + x509 -signkey` + `-extfile`（与叶证书同一条机制），
   `req -x509` 本来也不接受 `-extfile`，只能退回 `-addext`。

修完后 Linux 上"打包 → 生成证书 → 校验证书链 → 干净退出"全流程通过。
`--service`、杀软误报、体积优化仍需 Windows。

**结论**：方案成立，收益明确（免 Python 安装）。**但不在 0.1.0 内排期**——
本机（Linux）无法验证 exe 的体积/误报/服务模式，交付一个没验证过的产物不符合本项目标准。
排到 **v0.2.0**：先在 Windows 上手工跑通一次 PyInstaller（Linux 侧的两个阻塞点已清，
见上），再把产物接入 CI。若跑通，**Rust/Go 移植正式关闭**。

---

## 5. macOS 支持（ROADMAP v0.3.0 第 5 条）

**要解决的问题**：让 macOS 用户也能用（当前只覆盖 WSL/Linux + Windows）。

**现状**：`hublane.py` 本身是跨平台的（标准库 + ctypes 服务部分有 `IS_WIN` 分支）。
真正的差异只有三处：

1. 自启：`launchd` plist（`~/Library/LaunchAgents/hublane.plist`）；
2. 信任库：`security add-trusted-cert -d -r trustRoot -k ~/Library/Keychains/login.keychain ca.crt`；
3. 系统代理：`networksetup -setwebproxy Wi-Fi 127.0.0.1 8899`（需 sudo）。

**评估**：工作量约 S–M（新写一个 `install-macos.sh` + 文档），运行时零新增依赖。
但维护面会变成三个平台（安装器/卸载器/信任库各一套），而本项目目标用户的绝大多数在
WSL 与 Windows —— **没有 macOS 机器可供持续验证**，加了也是"纸面支持"。

**结论**：**0.1.0 与 0.2.0 都不做**。保留在 ROADMAP 的 `[A]`（排到 v0.3.0），
一旦出现明确需求（issue 或自用）再起，
届时可复用现有测试（测试不含平台相关的安装逻辑，macOS 上应直接可跑）。

---

## 协议演进：HTTP/2 上游与压缩透传（明确不做）

两项分别评估：

### HTTP/2 上游

- **收益存疑**：当前上游（镜像站、Watt、直连 IP）几乎全是 HTTP/1.1；
  HTTP/2 只在少数 CDN 上有意义，而那些站点多数走隧道直通，不经过我们的 HTTP 客户端。
- **成本很高**：标准库没有 HTTP/2 实现，自己写等于引入新依赖或手写帧解析（违反零依赖约束）。
- **结论：不做**。若将来某个镜像强制 HTTP/2，再加"该上游走隧道"的旁路。

### 压缩（`Accept-Encoding`）透传

- 现在**主动剥离** `Accept-Encoding`，原因有二：① 完整性校验需要按 `Content-Length`
  比对真实字节数，压缩后长度语义不一致；② 上游若返回 gzip，客户端看到的会是压缩流，
  而我们已声明 `Content-Length: 明文长度`。
- 要做就得同时改造：保留 `Content-Encoding` 头 + 按压缩后的实际字节数校验 + 大响应流式
  仍按块比对 —— 复杂度上升，收益只是省一点带宽（本机回环 + 出境带宽由上游决定）。
- **结论：不做**。保持"剥离编码、按明文字节校验"，这条不变量让完整性判断简单可靠。

## 6. 单文件 vs 拆包（ROADMAP v0.3.0 第 6 条）

- 当前 `hublane.py` 约 **2.8k 行**（0.1.0 时点）。ROADMAP 定的阈值是 **2.5k 行触发重新评估**。
- **已超阈值**，但拆分会牺牲"下载单个文件即可运行"这一核心卖点。
- **折中方案（推荐，暂不实施）**：源码拆成 `src/hublane/*.py` 便于维护，
  **构建期用 `tools/build_single.py` 拼回单文件发行版**，测试同时跑两种形态。
- **结论：0.1.0 / 0.2.0 不拆**。先观察 0.1.0 之后的行数增长；若继续增长到 3.5k 行以上，
  或出现"多人在同一文件上冲突"的情况，再执行上面的折中方案。

---

## 7. `--check-update` / `--update`（ROADMAP v0.2.0 第 4 / 12 条）

- **`--check-update` 结论：做。** 它只查询 GitHub Release API 并打印"有没有新版本"，
  不下载、不改动任何文件，与「不做自动更新」这条底线不冲突（底线防的是"替用户做决定"，
  不是"告诉用户有新版本"）。
- **`--update` 结论：做，但明确它的能力边界。** 三条硬约束：
  1. 走内部 opener（绕过 `http_proxy`），**不经过 hublane 自己**，否则会绕回本机形成自引用；
     代价是它享受不到 hublane 的加速 —— 在受限网络里大概率直接失败，这是**预期行为**，
     报错要写清楚"可能是网络受限，请到 Release 页手动下载"。
  2. 写入前做形状校验（必须是含 `def main(` 与 `VERSION` 的 Python，且长度够），
     写完后跑一遍新代码的 `--check`，任一不过就**整体回滚**到 `.bak`。
  3. **`config.json` 不覆盖**，新版另存 `config.json.new` —— 与 `install.sh` 的升级契约一致。
- 不做的部分：不做签名校验（密钥分发成本高于收益，provenance 已由 GitHub attestation 覆盖），
  因此 `--update` 的可信度上限就是"HTTPS + 仓库归属"，重要用途仍应手动下载核对 `SHA256SUMS`。

## 8. 分发 manifest 的维护成本（scoop / winget / Homebrew）

- 参照系：**dev-sidecar 已进 winget**，装机量的边际收益是真实存在的。
- 成本不在"写 manifest"（每个约 30 行 YAML/Ruby），而在**长期维护**：
  每个渠道都要跟版本、要处理校验和被拒的 PR（winget 对 publisher 验证较严）。
  三个渠道加起来约等于"每发一版多一件事"。
- **结论：先只做 winget**（Windows 用户占比最高，且 dev-sidecar 已验证这条路走得通），
  scoop 与 Homebrew 等出现实际 issue 需求再说。winget manifest 里只放 exe（v0.2.0 第 2 条
  的 Windows 打包跑通之后才有意义），**因此本项排在 exe 之后**。

## 9. ECH（Encrypted Client Hello）—— 不做，标准库约束下不可实现

- 背景：dev-sidecar 2.3.0 加了 ECH；它比早期"改 SNI 伪装"（`sni:'baidu.com'`）正当得多 ——
  ECH 是标准 TLS 扩展，**隐藏 SNI 但照样完整校验证书**，与 hublane「不绕过校验」的底线兼容，
  理论上也优于改 SNI。这是本项值得评估的原因。
- **结论：不做。** 查 Python 3.14.8 的 `ssl` 文档全文，**没有任何 ECH 相关 API**；
  CPython 侧自 2021 年的 issue #89730 起长期停留在 API 设计讨论。
  在「纯标准库、零第三方依赖」硬约束下无法实现。
- 触发重评的条件：CPython 的 `ssl` 模块暴露 ECH 接口（届时可给 `direct` 上游加一个开关）。

## 10. "安全模式"（dev-sidecar 的无证书降级档）—— 对 hublane 不成立

- dev-sidecar 的安全模式 = 不装证书、只做 DNS 优选 + 测速，功能弱但零信任成本。
  它能成立是因为它**最差也只是慢**。
- **hublane 不成立**：核心用例 `raw.githubusercontent.com` 直连必被 TCP RST，
  不 MITM 换源就**一点办法都没有**。照抄等于砍掉 hublane 唯一不可替代的价值。
- **结论：不做。** 仅保留一个低优先级想法：作为"证书安装失败时的降级运行档"
  （只跑 `direct`/`chain`、不换源），让代理不至于完全起不来 —— 排 P3，不为它改架构。

## 11. Gitee 同步：两种做法（ROADMAP v0.2.0 第 8 条）

- 做法 1（Gitee 仓库设为 GitHub 镜像 + 周期同步）：成本近乎为零，但**只同步代码与 tag，
  不含 Release 附件** —— 用户仍然拿不到 `hublane.py` / 安装脚本，最后一公里没解决。
- 做法 2（CI 里主动推）：打 tag 后用 `GITEE_TOKEN` 推 git 镜像，并调 Gitee API
  建 Release、上传附件。**成本是维护一个 `GITEE_TOKEN` secret。**
- **结论：采用做法 2**（已实现为 `.github/workflows/gitee-sync.yml`，挂 `release: published`）。
  其中"推 git 镜像"那步设为 `continue-on-error`：仓库已是 GitHub 镜像时它通常无操作，
  且镜像仓库可能拒收手动推送 —— 失败不应阻断真正有价值的附件同步。

---

## 维护提示

- 本文档与 `ROADMAP.md` 的 `[A]` 项一一对应；实施其中任一项时把对应的 `[A]` 改成 `[x]`。
- 评估结论随时间会变（例如杀软对 PyInstaller 的态度、是否有 macOS 需求），
  建议每次发版前扫一眼。
