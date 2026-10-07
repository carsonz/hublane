# hublane 架构说明

面向改动代码的人：一次请求是怎么走的、有哪些线程与状态、哪些地方最容易踩坑。

## 一句话

`hublane.py` 是**单文件、纯标准库**的本地中继代理：监听 `127.0.0.1:8899`（HTTP 与 SOCKS5
共用端口，按首字节判别），对"受管域名"终止 TLS 后按上游链取回内容，其它一切域名
**纯 TCP 隧道直通**（不解密、不改写）。

## 一次请求的完整时序

```
客户端                     hublane                          上游
  |  CONNECT host:443 或 SOCKS5 握手                          |
  |----------------------->|                                  |
  |                        | 1. 首字节判别(0x05 = SOCKS5)      |
  |                        | 2. 1.1 限流(max_conns/client_timeout)
  |                        | 3. 1.2 访问控制(proxy_token/uid)   |
  |                        | 4. should_intercept(host)?        |
  |        否 --------------> tunnel(): 纯双向搬运, 不解密       |
  |        是 --------------> serve_mitm()                     |
  |                        | 5. srv_context() 本地证书握手      |
  |                        | 6. 读请求头/请求体(read_headers/build_body)
  |                        | 7. build_chain(host, path):        |
  |                        |    per_host_upstreams > 域名分组 > 默认链
  |                        |    order_upstreams(): EWMA ÷ 成功率
  |                        | 8. relay_chain(): 逐个上游尝试      |
  |                        |    open_direct / open_watt /       |
  |                        |    open_chain / open_mirror        |
  |                        |    └─ 连接池: _pool_get(探活) →    |
  |                        |       _exchange → _pool_put        |
  |                        |    └─ relay_response():            |
  |                        |       完整性校验(小响应整体校验,     |
  |                        |       大响应流式+末尾比对)          |
  |                        |    └─ 截断/失败 → 降级该上游 → 下一个
  |                        |-------------------------------->| 
  |                        | 9. 2.1 latency_add + sample_add   |
  |<-----------------------| 10. 全链路失败 → send_502          |
```

关键点：**响应头一旦发给客户端就不能再回落**（`relay_chain` 里的 `committed`），
这时宁可断开让客户端看到"不完整响应"，也不能静默换源——否则用户会拿到半截文件还以为成功。

## 模块地图（单文件内的分区）

| 区域 | 主要函数 | 说明 |
|---|---|---|
| 配置 | `DEFAULTS` / `load_config` / `validate_config` / `reload_config` | 默认值合并、启动校验、热重载 |
| 日志 | `setup_logging` / `JsonFormatter` / `_log_targets` | 控制台+轮转文件；服务模式无控制台时回退 |
| 健康度 | `_u_ok` / `_u_fail` / `success_rate` / `order_upstreams` | EWMA × 滑动窗口成功率，落盘 `state.json` |
| DNS | `doh_resolve` / `_verify_ip` / `pick_ips` / `_doh_query` | DoH 解析 + **真实 TLS 握手验真**（抗污染） |
| 上游 | `OriginConnection` / `open_direct` / `open_watt` / `open_chain` / `open_mirror` | 每类上游一个 opener，统一返回 `(resp, conn, poolkey)` |
| 连接池 | `_pool_get` / `_pool_put` / `sock_alive` / `_pool_trim_locked` | 每 key 多条 + 全局 LRU；复用前探活 |
| 转发 | `relay_response` / `stream_body` / `read_exact` | 流式 + 完整性校验 |
| 链与回落 | `relay_chain` / `send_502` / `split_target` | 上游链遍历、截断判定 |
| 入站 | `handle_client` / `handle_socks5` / `_socks5_auth` / `tunnel` / `serve_mitm` | HTTP/SOCKS5 与隧道 |
| 指标 | `status_json` / `panel_html` / `MetricsHandler` / `diag_text` | 面板、JSON、PAC、样本、诊断包 |
| 可观测 | `latency_add` / `percentile` / `sample_add` | 直方图与请求样本（仅内存） |
| 平台 | `_win_service` / `gen_certs` / `emit` | Windows 服务、证书生命周期 |

## 线程模型

| 线程 | 数量 | 职责 |
|---|---|---|
| 主线程 | 1 | `main()`：监听、`_accept_loop`（1 秒超时轮询 `_stop`） |
| 客户端 | 每连接 1 | `handle_client` → `serve_mitm` 或 `tunnel`；受 `max_conns` 限制 |
| 指标面板 | 1 | `ThreadingHTTPServer`（内部每个请求再开线程） |
| `_refresher` | 1 | 每 `refresh_interval` 刷新已验证 IP |
| `_probe_all` | 1 | 主动探测各链（raw 镜像 / github / extra） |
| `_warm_async` | 临时 | DNS 预热，防首请求冷启动 |
| 探测线程池 | 临时 | `pick_ips` 里并行验真 IP（`ThreadPoolExecutor`） |

## 状态与持久化

| 数据 | 位置 | 是否落盘 |
|---|---|---|
| 上游健康度 `_ustat` | 内存 + `state.json` | 是（`save_state`/`load_state`） |
| DoH 端点健康度 `_doh` | 内存 + `state.json` | 是 |
| 已验证 IP `_ipcache` | 内存 | 否（重启即重新解析） |
| 连接池 `_pool` | 内存 | 否 |
| 计数器 `_counters` | 内存 | 否 |
| 延迟直方图 `_latency` | 内存 | 否 |
| 请求样本 `_samples` | 内存 | 否（`sample_size=0` 可关闭） |
| 证书/私钥 | 安装目录 | 是（`server.crt`/`server.key`/`ca.*`） |

**隐私边界**：请求样本与延迟数据只在内存里，不落盘、不联网、只经本机回环的指标端点暴露
（且受 `metrics_token` 保护）。

## 安全模型

1. 对受管域名做 TLS 中间人，CA 与叶证书**安装时本地生成**（`openssl`），私钥不出本机。
2. `direct` 上游坚持**完整证书校验**——这是区分真实 IP 与污染 IP 的唯一可靠手段，
   任何时候都不要"为了方便"关掉它。
3. 面板/JSON/样本/诊断包受 `metrics_token` 保护（绑非本机地址时强制要求）。
4. 代理侧可选 `proxy_token`（HTTP `407` / SOCKS5 用户名密码）与 uid 白名单。
5. 默认只监听 `127.0.0.1`；**在没有出境节点的前提下无法访问 Google / YouTube**，
   本项目不提供也不推荐任何"免节点翻墙"方案。

## 容易踩的坑

- **探活不能用 `MSG_PEEK`**：`SSLSocket.recv(flags)` 会抛 `ValueError`，
  所以 `sock_alive()` 对 TLS 走"可读即视为不可复用"。
- **复用连接可能被 `http.client` 悄悄关掉**：上游回 `Connection: close` 时
  `getresponse()` 之后 `conn.sock` 会变 `None`，`_pool_put` 因此直接丢弃。
- **探测与真实流量必须用同一个键**：健康度键是 `真实域名|上游名`，写错成 `"raw"`
  会让探测数据完全不参与排序（0.1.0 修的就是这个）。
- **镜像名不能与 raw 镜像重名**：raw 镜像会把路径重写成 `/gh/{owner}/{repo}@{ref}/…`，
  通用 CDN 反代要用别的名字（0.1.0 修的就是 `jsdelivr_fastly`）。
- **响应完整性 vs 流式**：`integrity_buffer_max`（默认 1 MiB）以内的响应整体校验
  以支持"截断即回落"，超过则流式转发，截断时只能断开（不能补 chunked 结束标记）。

## 测试分层

- `tests/test_hublane.py`：纯逻辑（配置校验、健康度、镜像 URL、限流、访问控制、直方图…）。
- `tests/test_integration.py`：起本地假上游（明文镜像 + TLS 假 Watt）+ 进程内 `main()`，
  用真 TLS 客户端跑端到端；**不需要外网**，`openssl` 缺失时自动跳过。
- `bench/bench_relay.py`：本地压测（吞吐/延迟），可与 `bench/baseline.json` 对比。
