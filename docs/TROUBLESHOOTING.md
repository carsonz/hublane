# 排障手册 / Troubleshooting

先做两件事，多数问题可以直接定位：

```bash
python hublane.py --check          # 配置校验
curl -s http://127.0.0.1:28898/ | head -60     # HTML 面板(健康度 + 延迟 + 最近请求)
curl -s http://127.0.0.1:28898/status          # 机器可读的同一份数据
```

要报 issue，直接生成诊断包（版本 + 配置(**token 已打码**) + 状态 + 日志尾部）：

```bash
curl -s -H "X-Hublane-Token: <token>" http://127.0.0.1:28898/diag
```

改了 `config.json` 不必重启：`kill -HUP $(pgrep -f hublane.py)`；
Windows 用面板的"重载配置"按钮或 `POST /reload`（都受 token 保护）。
写坏的配置会被拒绝（保留旧配置），监听地址/指标端口改动会提示"需重启"。

面板上每个上游的状态含义：

| 状态 | 含义 | 该做什么 |
|---|---|---|
| 正常 | 有 EWMA、成功率达标 | 无需处理 |
| 未证明 | 样本 < `success_min_samples`（默认 3） | 观察即可，会随流量收敛 |
| 成功率过低 | 成功率 ≤ `success_floor`（默认 0.5） | 调低 `success_floor`，或删掉这个上游 |
| 冷却 Xs | 连续失败进入冷却 | 等冷却结束；反复出现说明该上游已不可用 |

---

## 1. `curl -x http://127.0.0.1:8899 ...` 直接 502

1. 看面板"上游健康度"：所有上游是否都在冷却/未证明？
2. `raw.githubusercontent.com` 直连必然 RST，`raw_upstreams` 里必须有可用镜像
   （默认第一个 `gh-proxy.com`）；镜像全挂时可临时换成
   `["jsdelivr_fastly"]`。
3. 日志里搜 `响应不完整`：说明上游在传一半时被掐断，hublane 已回落；
   若所有上游都截断，说明链路被中间设备干扰，考虑改用 `chain`（自己的节点代理）。

## 2. 浏览器报证书错误（`NET::ERR_CERT_AUTHORITY_INVALID`）

- CA 没装或装到了错误的位置：
  - WSL：`/usr/local/share/ca-certificates/hublane-local-ca.crt` + `update-ca-certificates`；
    RPM 系用 `/etc/pki/ca-trust/source/anchors/` + `update-ca-trust`。
  - Windows：`certutil -addstore -user -f Root "%LOCALAPPDATA%\hublane\ca.crt"`。
- **Firefox 用自己的信任库**，需要手动导入 `ca.crt`，
  或在 `about:config` 里把 `security.enterprise_roots.enabled` 设为 `true`。
- 证书重生成过（删除过 `server.crt`/`server.key`）：把系统里的旧 CA 删掉再重新安装。

## 2b. 升级到 0.1.0 之后 HTTPS 全部报证书错误（`CA cert does not include key usage extension`）

**如果你在 0.1.0 之前装过 hublane，必须手动换一次 CA**，原因：

- 早期版本用 `openssl req -x509` 生成 CA，产物**只有 `basicConstraints`、
  没有 `keyUsage`**。OpenSSL ≤ 3.0 容忍这种 CA，**3.5 及以上会直接拒绝校验**
  （Python 3.13 自带的 OpenSSL 3.6 就是如此）。
- **`--renew-certs` 修不了这个问题** —— 它按设计只换叶证书、保留旧 CA，
  而坏的是 CA 本身。

```bash
# 1. 换 CA（会重新生成 ca.crt / ca.key / server.crt / server.key）
python hublane.py --renew-ca

# 2. 把新的 CA 重新装进系统信任库, 并把旧的删掉, 否则会同时信任两个
sudo rm -f /usr/local/share/ca-certificates/hublane-local-ca.crt
sudo cp /opt/hublane/ca.crt /usr/local/share/ca-certificates/hublane-local-ca.crt
sudo update-ca-certificates
sudo systemctl restart hublane
```

确认 CA 已经带上了 `keyUsage`：

```bash
openssl x509 -in /opt/hublane/ca.crt -noout -text | grep -A1 "Key Usage"
# 应看到 X509v3 Key Usage: critical / Digital Signature, Certificate Sign, CRL Sign
```

最省事的做法是直接重装：`sudo bash uninstall.sh --purge && sudo bash install.sh`。

## 3. WSL 里 `apt` / 内网服务变慢或被劫持的错觉

非受管域名走**纯 TCP 隧道**，不会解密。若怀疑被接管，检查：
`should_intercept` 的判定来源是 `GH_HOSTS`、`raw_upstreams` 对应的域名、
`extra_hosts` 与 `per_host_upstreams` —— 尤其 `per_host_upstreams` 里的通配
（如 `*.example.com`）会连带接管整个子域。用 `/status` 的
`managed_extra` / `per_host_upstreams` 字段确认。

## 4. 端口被占用 / 面板打不开

```bash
ss -lntp | grep -E '8899|28898'         # WSL
netstat -ano | findstr ":8899"          # Windows
```

- `metrics_port` 与 `listen_port` 不能相同（配置校验会拦）。
- 面板被显式关掉时检查 `metrics_enabled`；端口起不来只会在日志里 WARN，
  不影响代理本身。

## 4b. `407 Proxy Authentication Required` / SOCKS5 握手失败

设置了 `proxy_token` 之后：

```bash
curl -x http://hublane:<token>@127.0.0.1:8899 https://raw.githubusercontent.com/...
curl -x http://127.0.0.1:8899 --proxy-user hublane:<token> https://...
```

- SOCKS5 客户端必须支持用户名/密码（method 0x02），只报"无鉴权"的客户端会收到 `0xFF`；
  密码错误收到 `0x01`。
- `proxy_uid_whitelist` 非空时，只有白名单 uid 的进程可用（Linux/WSL）。
- 想临时恢复免鉴权：把 `proxy_token` 清空并重启。

## 5. 面板/JSON 返回 401

设置了 `metrics_token`。带上 token：

```bash
curl -H "X-Hublane-Token: <token>" http://127.0.0.1:28898/status
curl "http://127.0.0.1:28898/?token=<token>"
curl http://127.0.0.1:28898/healthz     # 唯一免 token 的端点
```

## 6. Windows：注销后代理就断了

计划任务模式（`install-windows.bat`）只在登录会话内有效。
以**管理员**运行 `install-windows-service.bat` 注册成系统服务；
之后用 `sc query hublane` 查看状态，`net stop hublane` 停止。

服务起不来时：

```bat
sc query hublane
type "%LOCALAPPDATA%\hublane\hublane.log"
sc start hublane
```

服务以 LocalSystem 运行，若安装目录不可写，日志会自动落到
`%TEMP%\hublane.log`（面板底部会显示实际日志路径）。

## 7. 上游总是"未证明"或健康度长期不更新

- `raw` 上游每 120 秒由后台线程探活一轮（启动 10 秒后首轮）。
- 其他链（`github_upstreams` / `extra_upstreams`）**只靠真实流量**学习，
  没有流量就没有健康度。
- `state.json` 在退出时落盘；异常退出会丢失最近的学习结果（会自动重建）。

## 7a. `git ls-remote` 正常，但 `git clone` / `git pull` 卡死或 502

典型报错：

```
error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502
fatal: error reading section header 'acknowledgments'   # 或 'shallow-info'
```

**原因**：`git ls-remote` 只请求 `info/refs`（响应 < 128 KiB），而 `clone/pull` 还要
下载 pack（远大于 128 KiB）。`github.com` 若排在前面的上游是 `direct`，响应传过
**正好 131072 字节（128 KiB）**时就会被链路掐断：

```
WARNING 响应不完整 ... <- direct (传输中断于 131072 字节:
        SSLEOFError(8, 'EOF occurred in violation of protocol'))
```

**确认**：看 hublane 日志里有没有 `传输中断于 131072 字节`。

```bash
journalctl -u hublane -n 200 | grep '响应不完整'
```

**处理**：确认 `github_upstreams` 里镜像排在 `direct` **之前**（0.1.0 起已是默认）：

```json
"github_upstreams": ["ghproxy_com_gh", "ghfast_gh", "ghproxy_net_gh", "direct", "watt"]
```

改完 `sudo systemctl restart hublane`（或 `SIGHUP`）。失败的上游会被自动降权冷却
（`direct_cooldown`，默认 600 秒），所以**排错顺序比删掉某个上游更重要**。

想临时绕开验证：

```bash
git clone https://gh-proxy.com/https://github.com/<owner>/<repo>.git
```

## 7b. 证书快到期 / 证书突然不受信

面板顶部会显示"证书剩余 N 天"，低于 `cert_expire_warn_days`（默认 90 天）会在日志里告警。

```bash
python hublane.py --renew-certs     # 只换叶证书: 系统里已信任的 CA 不用重装
python hublane.py --renew-ca        # 连 CA 一起换: 必须重新安装信任
```

续期用的是本机 `openssl`（Windows 上会找 Git for Windows 自带的那个）。

## 7c. 连接被拒 / 大量 `rejected` 计数

`max_conns`（默认 256）是并发客户端连接上限，超限的连接会被直接关闭并计入
`counters.rejected`。调大 `max_conns`，或调小 `client_timeout`（默认 60s）让
"只连不发"的连接更快被回收。

## 7d. 升级后想确认自己的配置没被覆盖

`install.sh` / `install-windows.bat` **不会覆盖**你已有的 `config.json`。
升级时它会：

- 原样保留 `/opt/hublane/config.json`（旧值再存一份 `config.json.bak-<时间戳>`）；
- 把发行包里的新版默认配置写到 `config.json.new`；
- 打印 `diff` 命令提示你自己合并。

所以升级后请自己看一眼差异，把新增的键补进去：

```bash
diff /opt/hublane/config.json /opt/hublane/config.json.new
```

合并完可以删掉 `.new` 与 `.bak-*`。想直接丢弃自己的配置、用发行版默认值：

```bash
sudo bash install.sh --reset-config
```

**若保留的旧配置用新版 `hublane.py` 校验不通过，安装会在覆盖任何东西之前中止**，
并提示你合并 `.new` —— 不会留下装了一半的状态。

## 7e. `git push` 失败：502，或 `Permission denied (publickey)`

两件完全不同的事会表现成同一句话，先分清是哪一种。

**A. HTTPS 远程 + 本机 hublane 代理 → 502（`all upstreams failed`）**

`git push https://github.com/...` 带着你的凭证（`Authorization`），而 hublane 的
设计范围是**匿名只读**加速：公共镜像拿不到授权、无法代表你写 GitHub，于是逐个
401，最后 502。这是设计使然而非故障 —— **push 请走 SSH**，或临时摘掉代理：

```bash
env -u https_proxy -u HTTPS_PROXY git push origin main
```

**B. 已经用 SSH 却仍 `Permission denied (publickey)`**

先核对密钥是否真的登记在 GitHub 上：

```bash
curl -s https://github.com/<你的用户名>.keys    # 列出该账号登记的公钥
ssh-keygen -lf ~/.ssh/id_rsa.pub                # 本地公钥指纹
```

两边对得上还被拒，多半是 **DNS 被劫持**：某些 Windows 侧的代理/加速工具会把
`github.com` 解析到 `127.0.0.1`，于是 ssh 连的是**本机 sshd**，当然被拒 ——
`ssh -v` 里能看到决定性的一行：

```
debug1: Connecting to github.com [127.0.0.1] port 22.
```

另外受限网络常封 22 端口。两者都能绕开：改用 GitHub 官方的 `ssh.github.com:443`
入口（既绕开被劫持的域名，又走通常放行的 443，是官方支持的做法）：

```bash
bash tools/setup-git-ssh.sh            # 自动检测并写入, 幂等
bash tools/setup-git-ssh.sh --mode never   # 只想看判定结果、不写入
```

它往 `~/.ssh/config` **追加**一段带标记的配置（`Host github.com` →
`ssh.github.com:443`），`git@github.com:...` 这种远程随即生效，你原有的配置不会
被动。删掉 `# >>> hublane: GitHub SSH over 443 >>>` 两个标记之间的内容即可恢复
默认。安装脚本会自动做这一步（默认只在检测到异常时写；`--no-git-ssh` 跳过，
`--git-ssh=always` / `--git-ssh-always` 强制写）。

验证：

```bash
ssh -T git@github.com    # 应看到 Hi <用户名>! You've successfully authenticated
```

**C. `GH013 ... Cannot force-push to this branch`**

这是 GitHub 仓库的**分支保护规则**在拦，和 hublane、和网络都无关。改用普通合并
提交推送即可（别用 `--force`）。

## 8. 想彻底重来

```bash
# WSL
sudo bash uninstall.sh --purge && sudo bash install.sh && exec zsh

# Windows
uninstall-windows.bat        # 或 uninstall-windows-service.bat
install-windows.bat
```

`uninstall.sh` 会备份被修改过的 shell 配置为 `~/.zshenv.hublane.bak`（zsh）/
`~/.bashrc.hublane.bak`（bash）等；`.zshenv`、`.zshrc`、`.bashrc`、`.profile` 四个文件
都会被检查一遍，所以旧版本写进 `.zshrc` 的残留也能清掉。

---

## 9. Windows 专项（Windows-specific）

以下每条都在 Windows 上实测复现过。

### 9a. `curl: (60) schannel: the revocation status is unknown`

代理本身工作正常，是 **Windows 自带 curl（schannel 后端）** 对本地自签 CA 做了
OCSP 吊销检查，而 hublane 的 CA 没有吊销响应者。加 `--ssl-no-revoke` 即可：

```bat
curl --ssl-no-revoke --cacert %LOCALAPPDATA%\hublane\ca.crt -x http://127.0.0.1:8899 https://api.github.com/
```

浏览器（Chrome/Edge/Firefox）不受影响，它们不做这个检查。
WSL 里的 curl 用 OpenSSL 后端，也没有这个问题。

### 9b. `SSL: CA cert does not include key usage extension`

Python 3.14 / OpenSSL 3.5 按 RFC 5280 §4.2.1.3 要求 CA 证书必须带 `keyUsage` 扩展，
缺失会直接拒绝握手（3.12/3.13 不报这个错，所以只在装了新 Python 的机器上出现）。

```bat
python hublane.py --renew-ca      # 重新生成 CA(带 keyUsage)与叶证书
:: 然后把新的 ca.crt 重新装进受信任根证书颁发机构, 浏览器要重启
```

### 9c. `.bat` 报 `xxx was unexpected at this time.`

cmd.exe 在解析 `if (...)` / `for (...)` 块时，会跟踪括号配对。如果某条 `echo`
**同时包含非 ASCII 中文和 ASCII 括号**，cmd 的 DBCS 解码会把 `)` 吞掉，导致
整个块失衡——而且偏偏是"报错提示"所在的块最先崩，提示反而看不到。

规避方式（写 .bat 时）：

- `.bat` 存为 UTF-8（`@echo off` 前不要加 BOM）；
- **不要在 `echo` 文本里写 `(` `)`**，改用中文顿号或直接去掉；
- 需要说明的话放到 `rem` 注释里。

### 9d. WSL 下 `./install.sh: bad interpreter: /usr/bin/env bash^M`

`.sh` 被以 CRLF 检出。仓库已带 `.gitattributes` 固定 `.sh` 为 LF、`.bat` 为 CRLF。
旧检出修一下：

```bash
git config core.autocrlf input && git add --renormalize .
git rm --cached -r . && git reset --hard
```

### 9e. 登录后代理几秒就没了，日志还是空的

老版本的 `run-loop.bat` 里写的是 `py`，而计划任务/Run 项的环境里没有 `py`，
于是每 3 秒静默失败一次（`goto loop` 死循环）。现在写入的是解释器绝对路径。
重新跑一次 `install-windows.bat` 即可。

### 9f. 找不到 `openssl`

Windows 不自带 OpenSSL，通常靠 Git for Windows 附带的那份（hublane 会自动去找）。
没有 Git 就用 winget 装：

```powershell
pwsh -File tools/setup-windows-env.ps1     # 装 Python + OpenSSL，并建 .venv
```

`--renew-certs` / `--renew-ca` 依赖 openssl；证书已存在且未过期时用不到。

### 9g. `ERROR: Input redirection is not supported`

`wscript.exe` 在 stdin 被重定向时启动会打印这句。出现在 CI / agent / SSH 会话里，
双击或正常桌面登录时不会出现。无害，可忽略。

### 9h. 停止服务后进程还要过一会儿才消失

`sc stop hublane` 之后服务状态会立刻变成 `STOPPED`，端口也会很快释放，但
`pythonw.exe` 可能还要再活 10~15 秒才退出。这是设计内的，不是残留：

- hublane 用 `concurrent.futures.ThreadPoolExecutor` 并发查 DoH 和验真 IP；
- Python 解释器的 atexit 钩子会 `join()` 这些线程池线程，池里如果有在途的
  网络请求，退出就要等到它们超时返回；
- hublane 给 SCM 报的 `STOP_PENDING` 等待提示本来就是 **30 秒**
  （`ServiceCore.report(SVC_STOP_PENDING, hint=30000)`），实测最长约 15 秒，
  在这个预算之内，SCM 不会判定服务无响应。

判断是不是真异常，看有没有"影子实例"：**超过 30 秒还能憋着回收端口的进程**
一般是从计划任务模式升级过来、没被收掉的旧
`run-loop` 进程（`install-windows-service.bat` 的 `[3/6]` 已经在处理它了）。

### 9i. 服务模式相关

```bat
sc query hublane          :: 状态
sc qfailure hublane       :: 崩溃自动重启配置(安装脚本设的是 3 秒后重启)
net stop hublane          :: 停止(会走 _stop 事件优雅退出, 等 1~2 秒)
```

- 装了服务又想要计划任务模式：先跑 `uninstall-windows-service.bat`，再
  `install-windows.bat`（两者互斥，否则会起两个实例抢端口）。
- 服务以 LocalSystem 运行，`hublane.log` 写在安装目录；若目录不可写会自动
  回退到 `%TEMP%\hublane.log`。

---

## 10. 面板计数怎么读

| 卡片 | 口径 |
|---|---|
| HTTP 请求 | 经 MITM 解密后处理的请求数，**不含**纯 TCP 隧道 |
| 上游成功 / 上游失败 | **单次上游尝试**。一个请求依次试 N 个上游，失败就加 N，所以"上游失败"可能大于"HTTP 请求" |
| 响应截断 | 完整性校验发现响应被截断，已降级上游或主动断开 |
| 池命中/未命中/复用重试 | 连接池复用情况 |
| 拒绝连接 | 鉴权失败、并发超限、SOCKS5 口令错 |
| 回源字节 | 经上游回源并转发给客户端的响应体字节 |
| 隧道连接/失败/字节 | 纯 TCP 隧道：非托管域名 CONNECT、明文 `http://`、SSH |

延迟 P50/P95/P99 **只统计 MITM 请求**，隧道耗时不计入 —— 隧道可能存活数分钟，
混进直方图会把分位数彻底带偏。隧道耗时会作为一条 `TCP` 样本出现在"最近请求"里。

---

## 采集信息用于报 issue

请附上：

1. `python hublane.py --version` 与平台（WSL / Windows / 服务模式）；
2. `curl -H "X-Hublane-Token: <token>" http://127.0.0.1:28898/status` 的输出；
3. 复现命令与完整输出（例如 `curl -v -x http://127.0.0.1:8899 <url>`）；
4. 相关日志行（WSL：`journalctl -u hublane -n 200`；Windows：安装目录下 `hublane.log`）。
