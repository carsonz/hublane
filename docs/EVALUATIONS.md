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
| 体积 | 预期 8–15 MB（含 Python 解释器与 ssl/ctypes） |
| 杀软误报 | **风险点**：PyInstaller 产物常被误报；需代码签名才能缓解，而签名证书要钱 |
| 证书生成 | Windows 通常没有系统 `openssl`，exe 依赖 Git for Windows 的 `openssl.exe`；已在 `find_openssl()` 里覆盖常见路径，但仍需"用户没装 Git"的兜底（预生成模式或随包附带 openssl） |
| 服务模式 | `--service` 用 ctypes 调 SCM，打包后仍可用；需实测 |
| 升级 | 覆盖 exe 即升级，与 `sc stop/start` 配合即可 |
| 成本 | 小（CI 加一条 Windows job，约半天）；**但验证需要一台干净的 Windows 机器** |

**结论**：方案成立，收益明确（免 Python 安装）。**但不在 0.1.0 内排期**——
本机（Linux）无法验证 exe 的体积/误报/服务模式，交付一个没验证过的产物不符合本项目标准。
排到 **v0.2.0**：先在 Windows 上手工跑通一次 PyInstaller（含证书兜底方案），
再把产物接入 CI。若跑通，**Rust/Go 移植正式关闭**。

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

## 维护提示

- 本文档与 `ROADMAP.md` 的 `[A]` 项一一对应；实施其中任一项时把对应的 `[A]` 改成 `[x]`。
- 评估结论随时间会变（例如杀软对 PyInstaller 的态度、是否有 macOS 需求），
  建议每次发版前扫一眼。
