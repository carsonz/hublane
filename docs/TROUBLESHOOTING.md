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

## 8. 想彻底重来

```bash
# WSL
sudo bash uninstall.sh --purge && sudo bash install.sh && exec zsh

# Windows
uninstall-windows.bat        # 或 uninstall-windows-service.bat
install-windows.bat
```

`uninstall.sh` 会备份被修改过的 shell 配置为 `~/.zshrc.hublane.bak` 等。

---

## 采集信息用于报 issue

请附上：

1. `python hublane.py --version` 与平台（WSL / Windows / 服务模式）；
2. `curl -H "X-Hublane-Token: <token>" http://127.0.0.1:28898/status` 的输出；
3. 复现命令与完整输出（例如 `curl -v -x http://127.0.0.1:8899 <url>`）；
4. 相关日志行（WSL：`journalctl -u hublane -n 200`；Windows：安装目录下 `hublane.log`）。
