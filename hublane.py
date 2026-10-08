#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hublane - 让 WSL 与 Windows 稳定访问 GitHub 的本地中继代理

按 v0.1.0 已实现的能力:
  转发   流式转发(响应/请求体) / SOCKS5 入站(与 HTTP 共用端口) / 非受管域名纯 TCP 隧道 /
         SSH(22) 经节点转发 / IPv6 + 多路径 DoH 解析 / 启动后台线程预热 DNS
  选路   DoH + 真实 TLS 验真直连 / Watt / 自有节点链 / raw 镜像库(前缀式 + 域名替换式 + CDN) /
         按域名上游链(per_host_upstreams) / 非 GitHub 站点整站换源(SITE_MIRRORS) /
         可插拔自定义镜像与远程上游清单 / PAC 自动代理配置 /
         健康度排序(EWMA x 成功率) / 全链路主动探测 / 响应完整性校验与截断回落
  健壮   上游连接池(探活 + 透明重试 + 容量 LRU) /
         客户端连接治理(client_timeout / max_conns) / 本地访问控制(proxy_token + uid 白名单) /
         证书生命周期(--renew-certs) / Windows 真服务(--service) 与计划任务自愈
  可观测 HTML 面板(上游与 DoH 健康度、延迟 P50/P95/P99、最近请求、
         HTTP 与纯 TCP 隧道分开计数) /
         /status /requests /diag /healthz /pac + 指标鉴权(metrics_token) /
         JSON 日志 / 配置热重载(SIGHUP 与 POST /reload)

纯标准库, 无第三方依赖; Windows 与 Linux(WSL) 同一份代码。
"""
import argparse
import base64
import calendar
import collections
import hmac
import html
import json
import logging
import os
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures
from http import client as httpclient
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

APP = "hublane"
VERSION = "0.1.1"
IS_WIN = os.name == "nt"
# PyInstaller onefile 模式下 __file__ 指向临时解包目录(_MEIPASS), 每次运行都会被清空。
# 若继续用它当数据目录, 证书/配置/状态/日志会跟着临时目录一起消失, 所以冻结后
# 一律以 exe 所在目录为准。
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = getattr(sys, "_MEIPASS", "") or ""   # onefile 解包目录, 源码运行时为空
INSTALL_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
               else os.path.dirname(os.path.abspath(__file__)))


def seed_bundled(*names):
    """把 exe 内置的默认文件播种到 INSTALL_DIR(仅当目标不存在时)。

    仅在 PyInstaller 打包后有意义: 让单文件 exe 首次运行时能自动落地
    config.json 与预生成的证书, 而不用让用户手工准备。
    """
    if not BUNDLE_DIR or BUNDLE_DIR == INSTALL_DIR:
        return
    for name in names:
        src = os.path.join(BUNDLE_DIR, name)
        dst = os.path.join(INSTALL_DIR, name)
        if os.path.exists(dst) or not os.path.isfile(src):
            continue
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass


CERT = os.path.join(INSTALL_DIR, "server.crt")
KEY = os.path.join(INSTALL_DIR, "server.key")
CONF = os.path.join(INSTALL_DIR, "config.json")
STATE = os.path.join(INSTALL_DIR, "state.json")
LOG_FILE = os.path.join(INSTALL_DIR, "%s.log" % APP)
SERVICE_NAME = "hublane"
DOH_PARALLEL = 2                      # 每轮并发查询的 DoH 端点数
CA_CRT = os.path.join(INSTALL_DIR, "ca.crt")
CA_KEY = os.path.join(INSTALL_DIR, "ca.key")
CSR = os.path.join(INSTALL_DIR, "server.csr")
CERT_DAYS = 3650                      # 与安装脚本保持一致
CA_SUBJECT = "/O=hublane/OU=Local Relay/CN=hublane Local Relay CA"
LEAF_SUBJECT = "/O=hublane/OU=Local Relay/CN=hublane Relay Leaf"
# CA 扩展: RFC 5280 §4.2.1.3 要求 CA 证书带 keyUsage, 且必须含 keyCertSign。
# OpenSSL 3.5+(Python 3.14 自带)/部分浏览器会校验这一项, 缺失会直接
# 报 "CA cert does not include key usage extension" 而拒绝握手。
CA_ADDEXT = ["-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,digitalSignature,keyCertSign,cRLSign"]
# 与 install.sh / install-windows.bat 保持一致(测试会校验三处一致)。
# 覆盖: GitHub 系 + 默认 extra_hosts + 所有分组主机 + 内置按域名链的域名,
# 否则"启用分组后浏览器报证书名不匹配"。
EXTRA_LEAF_SAN = ("DNS:opencode.ai,DNS:www.baidu.com,DNS:localhost")


def leaf_san():
    """叶证书 SAN: 由库里的域名推导, 新增站点不会漏掉证书"""
    hosts = set(GH_HOSTS) | set(RAW_HOSTS)
    hosts |= {str(h).strip().lower() for h in DEFAULTS.get("extra_hosts", [])}
    for names in DEFAULTS.get("extra_host_groups", {}).values():
        hosts |= {str(h).strip().lower() for h in names or []}
    hosts |= set(BUILTIN_PER_HOST_UPSTREAMS)
    hosts |= {h for h in hosts if h.startswith("*.")}     # 保留通配写法
    hosts = {h for h in hosts if h}
    return ",".join(["DNS:" + h for h in sorted(hosts)] + [EXTRA_LEAF_SAN])


RAW_HOST = "raw.githubusercontent.com"
RAW_HOSTS = {RAW_HOST}

GH_HOSTS = {
    "github.com", "www.github.com", "api.github.com", "gist.github.com",
    "codeload.github.com", "objects.githubusercontent.com",
    "camo.githubusercontent.com", "cloud.githubusercontent.com",
    "avatars.githubusercontent.com",
    "private-user-images.githubusercontent.com",
    "user-images.githubusercontent.com", "github.githubassets.com",
    "githubusercontent.com", "github.dev",
}

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8899,
    "enable_socks5": True,
    "metrics_port": 28898,
    "metrics_host": "",
    "metrics_enabled": True,
    "metrics_token": "",
    # 1.2: 本地访问控制 —— 空表示不鉴权(仅靠 127.0.0.1 隔离)
    "proxy_token": "",
    "proxy_uid_whitelist": [],        # 仅 Linux/WSL: 允许使用代理的 uid 列表
    "raw_upstreams": ["ghproxy_com", "jsdelivr_fastly", "ghproxy",
                      "jsdelivr", "jsdelivr_gcore", "jsdelivr_cf"],
    "github_upstreams": ["direct", "watt"],
    "extra_hosts": [
        "hcaptcha.com", "assets.hcaptcha.com", "imgs.hcaptcha.com",
        "www.hcaptcha.com", "js.hcaptcha.com", "newassets.hcaptcha.com",
        "imgs3.hcaptcha.com",
        "client-api.arkoselabs.com", "epic-games-api.arkoselabs.com",
        "cdn.arkoselabs.com", "prod-ireland.arkoselabs.com",
        "onedrive.live.com", "onedrive.live",
        "dropbox.com", "www.dropbox.com", "dl.dropboxusercontent.com",
        "mega.nz", "www.mega.nz", "mega.co.nz", "mega.io",
        "gravatar.com", "secure.gravatar.com", "www.gravatar.com",
        "fonts.googleapis.com", "ajax.googleapis.com",
        "vercel.app",
    ],
    "extra_upstreams": ["watt", "direct", "chain"],
    # 境外站点分组库: 按需整组启用(写入 extra_host_groups_enabled 即可),
    # 默认只开 fonts_cdn(字体文件域名), 其余保持关闭以免无谓接管
    "extra_host_groups": {
        "fonts_cdn": ["fonts.gstatic.com", "themes.googleusercontent.com",
                      "unpkg.com", "esm.sh", "cdnjs.cloudflare.com",
                      "cdn.jsdelivr.net"],
        "pages": ["netlify.app", "netlify.com", "workers.dev", "pages.dev",
                  "railway.app", "fly.dev"],
        "container": ["ghcr.io", "registry-1.docker.io", "auth.docker.io",
                      "production.cloudflare.docker.com", "quay.io",
                      "gcr.io", "k8s.gcr.io", "registry.k8s.io"],
        "toolchain": ["nodejs.org", "golang.org", "go.dev",
                      "proxy.golang.org", "static.rust-lang.org",
                      "static.crates.io", "crates.io",
                      "releases.hashicorp.com"],
        "git_hosting": ["gitlab.com", "bitbucket.org", "codeberg.org",
                        "sourceforge.net", "downloads.sourceforge.net"],
        # 下面几组是"有国内镜像可换"的站点(镜像见 SITE_MIRRORS, 按域名链见
        # BUILTIN_PER_HOST_UPSTREAMS): 开组 = 接管, 换源由内置链自动完成
        "ai_models": ["huggingface.co", "cdn-lfs.huggingface.co", "hf.co"],
        "python": ["pypi.org", "files.pythonhosted.org"],
        "npm": ["registry.npmjs.org", "www.npmjs.com"],
        "go": ["proxy.golang.org", "sum.golang.org", "golang.google.cn"],
        "rust": ["index.crates.io", "static.crates.io", "crates.io", "docs.rs"],
        "conda": ["repo.anaconda.com", "conda.anaconda.org"],
    },
    "extra_host_groups_enabled": ["fonts_cdn"],
    # 按域名的上游链: {"github.com": ["watt", "direct"], "*.example.com": [...]}
    # 出现过的域名自动视为"接管域名", 无需再写入 extra_hosts
    "per_host_upstreams": {},
    # 用户可插拔自定义镜像: {"名字": "https://host/https://raw.githubusercontent.com"}
    "custom_mirrors": {},
    "upstream_list_url": "",
    "chain_host": "127.0.0.1",
    "chain_port": 7890,
    "chain_socks_port": 0,
    # 按配置顺序取前 2 个并发查询; 连续失败者自动排到队尾, 因此"备用"放后面即可
    "doh_endpoints": [
        "https://dns.alidns.com/resolve?name={host}&type={type}",
        "https://doh.pub/dns-query?name={host}&type={type}",
        "https://dns.pub/dns-query?name={host}&type={type}",
        "https://120.53.53.53/dns-query?name={host}&type={type}",
        "https://doh.360.cn/dns-query?name={host}&type={type}",
        "https://doh-pure.onedns.net/dns-query?name={host}&type={type}",
        "https://dns.adguard.com/dns-query?name={host}&type={type}",
        "https://dns.quad9.net/dns-query?name={host}&type={type}",
        "https://doh.dns.sb/dns-query?name={host}&type={type}",
        "https://cloudflare-dns.com/dns-query?name={host}&type={type}",
        "https://dns.google/resolve?name={host}&type={type}",
    ],
    "enable_ipv6": True,
    "refresh_interval": 300,
    # 1.5: 主动探测(raw 镜像 + github 链 + extra 站点), 让每条链都有冷启动数据
    "probe_enabled": True,
    "probe_delay": 10,                # 启动后多久跑第一轮
    "probe_interval": 0,              # 0 = 跟随 refresh_interval(最小 120s)
    "probe_extra_max": 3,             # 每轮最多探测几个 extra 站点(按配置顺序)
    # 1.4: 证书生命周期(剩余天数低于该值启动告警, 可用 --renew-certs 续期)
    "cert_expire_warn_days": 90,
    # 2.1 可观测性: 最近请求样本的条数(0 = 不记录样本, 直方图仍统计)
    "sample_size": 200,
    "direct_timeout": 6,
    "direct_fail_max": 3,
    "direct_cooldown": 600,
    "verify_timeout": 4,
    "doh_timeout": 6,
    "watt_host": "127.0.0.1",
    "watt_port": 443,
    "timeout": 60,
    # 1.1 客户端连接治理: 慢连接(slowloris)上限与并发上限
    "client_timeout": 60,             # 单个客户端连接的空闲上限(秒); 0 = 不限制
    "max_conns": 256,                 # 并发客户端连接上限; 0 = 不限制
    "pool_enabled": True,
    "pool_max_idle": 30,
    "pool_probe": True,               # 复用前探活(内核已收到 FIN/RST 则丢弃)
    "pool_max_per_key": 4,            # 1.3: 每个 域名|上游 最多缓存几条连接
    "pool_max_total": 64,             # 1.3: 全局容量上限(超出按 LRU 淘汰)
    "integrity_check": True,          # 校验 Content-Length 是否与实际字节数一致
    "integrity_buffer_max": 1 << 20,  # 小响整体校验后再转发(可透明回落); 0 = 直接流式
    "success_window": 20,             # 成功率滑动窗口长度
    "success_min_samples": 3,         # 样本数不足时视为"未证明"
    "success_floor": 0.5,             # 成功率低于该值直接降级
    "log_level": "INFO",
    "log_format": "text",             # 2.2: text 或 json(单行结构化日志)
    "verbose": True,
}

_HOP_HEADERS = {
    "proxy-connection", "proxy-authorization", "connection", "keep-alive",
    "transfer-encoding", "upgrade", "te", "trailer", "expect",
    "accept-encoding", "host", "content-length",
}
_RESP_SKIP = {"transfer-encoding", "connection", "content-encoding",
              "content-length", "keep-alive"}
_STREAM_THRESHOLD = 1 << 20          # 请求体超过 1MB 才走流式上传

CONFIG = dict(DEFAULTS)
_srvctx = None
_ctxlock = threading.Lock()
_ipcache = {}
_ipcache_lock = threading.Lock()
_warming = set()
_ustat = {}
_ustat_lock = threading.Lock()
_doh = {}
_doh_lock = threading.Lock()
_pool = {}
_pool_lock = threading.Lock()
_stop = threading.Event()
_started_at = time.time()
_counters = {"requests": 0, "ok": 0, "fail": 0, "truncated": 0,
             "pool_retry": 0, "pool_hit": 0, "pool_miss": 0,
             "bytes": 0, "rejected": 0,
             # 纯 TCP 隧道(非托管域名 CONNECT / 明文 http:// / SSH)单独统计。
             # 不并入 requests/ok/fail: 那三项的口径是"HTTP 请求 / 单次上游尝试",
             # 隧道既不是 HTTP 请求也没有"上游", 混在一起会让面板口径失真。
             "tunnel_conns": 0, "tunnel_fail": 0, "tunnel_bytes": 0}
_counters_lock = threading.Lock()
# 2.1 可观测性: 固定桶延迟直方图 + 最近请求样本(仅内存, 不落盘, 不出本机)
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)   # 秒(桶上界)
_latency = {}                      # "域名|上游" -> [各桶计数..., 溢出桶]
_latency_lock = threading.Lock()
_samples = collections.deque()     # 最近 N 条请求(见 sample_size)
_samples_lock = threading.Lock()
# 1.1: 并发客户端连接限流(超过 max_conns 直接拒绝, 防止线程被慢连接耗尽)
_limiter = None
# 内部请求(DoH / 校验 / 镜像)必须绕开环境变量里的代理, 否则会绕回自己
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
log = logging.getLogger(APP)


def _count(key, delta=1):
    with _counters_lock:
        _counters[key] = _counters.get(key, 0) + delta


def counters_snapshot():
    with _counters_lock:
        return dict(_counters)


def _fmt_bytes(value):
    """字节计数换算成 KB/MB, 纯展示用"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


# ------------------------------------------------------------ 2.1 延迟与样本
def _bucket_index(seconds):
    for i, edge in enumerate(LATENCY_BUCKETS):
        if seconds <= edge:
            return i
    return len(LATENCY_BUCKETS)


def latency_add(scope, upstream, seconds):
    """记一次成功请求的耗时(秒)"""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return
    key = "%s|%s" % (scope, upstream)
    with _latency_lock:
        for store_key in (key, "*|*"):
            buckets = _latency.setdefault(store_key,
                                          [0] * (len(LATENCY_BUCKETS) + 1))
            buckets[_bucket_index(seconds)] += 1


def percentile(buckets, fraction):
    """直方图分位数: 返回包含该分位的桶上界(近似值, 不会低估)"""
    total = sum(buckets)
    if not total:
        return None
    target = fraction * total
    acc = 0
    for i, count in enumerate(buckets):
        acc += count
        if acc >= target:
            return LATENCY_BUCKETS[i] if i < len(LATENCY_BUCKETS) else None
    return None


def latency_stats(limit=12):
    """{"global": {...}, "by_upstream": [...]} —— 供 /status 与面板使用"""
    with _latency_lock:
        snapshot = {k: list(v) for k, v in _latency.items()}

    def summarize(buckets):
        return {"count": sum(buckets),
                "p50": percentile(buckets, 0.5),
                "p95": percentile(buckets, 0.95),
                "p99": percentile(buckets, 0.99)}
    rows = []
    for key in sorted(snapshot, key=lambda k: -sum(snapshot[k])):
        if key == "*|*":
            continue
        host, _, upstream = key.partition("|")
        item = {"host": host, "upstream": upstream}
        item.update(summarize(snapshot[key]))
        rows.append(item)
    return {"global": summarize(snapshot.get("*|*", [0])),
            "by_upstream": rows[:limit]}


def sample_add(**fields):
    """记录一条请求样本(环形缓冲, 容量 sample_size; 0 = 关闭)"""
    global _samples
    try:
        size = int(CONFIG.get("sample_size", 200) or 0)
    except (TypeError, ValueError):
        size = 200
    if size <= 0:
        return
    fields.setdefault("ts", time.time())
    with _samples_lock:
        if _samples.maxlen != size:
            _samples = collections.deque(_samples, maxlen=size)
        _samples.append(fields)


def samples(limit=50):
    with _samples_lock:
        items = list(_samples)
    items.reverse()                      # 最新的在前
    return items[:limit]


class ConnLimiter:
    """1.1: 并发客户端连接限流

    每个监听实例一个实例(而非模块级计数), 这样重启/多实例时
    旧连接的延迟释放不会把新实例的计数带歪。max_conns=0 表示不限。
    """

    def __init__(self, limit=0):
        try:
            self.limit = int(limit or 0)
        except (TypeError, ValueError):
            self.limit = 0
        self._lock = threading.Lock()
        self.active = 0

    def acquire(self):
        with self._lock:
            if self.limit and self.active >= self.limit:
                return False
            self.active += 1
            return True

    def release(self):
        with self._lock:
            self.active = max(0, self.active - 1)

    def count(self):
        with self._lock:
            return self.active


def active_conns():
    return _limiter.count() if _limiter else 0


# ------------------------------------------------------------ 配置
def _load_merged():
    """读 CONF 并与默认值合并, 返回新字典(不改动全局 CONFIG)"""
    merged = dict(DEFAULTS)
    try:
        with open(CONF, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            merged.update(data)
    except Exception as exc:
        return None, "读取配置失败: %s" % exc
    url = merged.get("upstream_list_url", "")
    if url:
        try:
            with _OPENER.open(urllib.request.Request(
                    url, headers={"User-Agent": "%s/%s" % (APP, VERSION)}),
                    timeout=8) as resp:
                remote = json.loads(resp.read().decode("utf-8", "replace"))
            for key in ("raw_upstreams", "custom_mirrors"):
                if isinstance(remote.get(key), (list, dict)):
                    merged[key] = remote[key]
        except Exception as exc:
            return None, "远程上游清单获取失败: %s" % exc
    return merged, None


def load_config():
    merged, err = _load_merged()
    if merged is None:
        log.warning("%s, 使用内置默认值", err)
        return
    CONFIG.update(merged)


def reload_config():
    """2.3 配置热重载: 返回 (是否成功, 说明, 是否需要重启)

    - 新配置非法时保留旧配置, 只告警;
    - 监听/指标端口这类结构性参数改不了, 明确提示"需重启"。
    """
    global _samples
    merged, err = _load_merged()
    if merged is None:
        log.warning("热重载失败: %s", err)
        return False, err, False
    errs = validate_config(merged)
    if errs:
        msg = "新配置不合法, 已保留旧配置: %s" % "; ".join(errs)
        log.warning("热重载失败: %s", msg)
        return False, msg, False
    old = dict(CONFIG)
    structural = [k for k in ("listen_host", "listen_port", "metrics_port",
                              "metrics_host", "enable_socks5")
                  if old.get(k) != merged.get(k)]
    changed = sorted(k for k in merged if old.get(k) != merged.get(k))
    CONFIG.clear()
    CONFIG.update(merged)
    if any(k in changed for k in ("log_level", "log_file", "log_format")):
        setup_logging()
    if any(k in changed for k in ("pool_enabled", "pool_max_per_key",
                                  "pool_max_total", "raw_upstreams",
                                  "github_upstreams", "extra_upstreams")):
        with _pool_lock:
            pooled = [conn for bucket in _pool.values() for conn, _ts in bucket]
            _pool.clear()
        for conn in pooled:
            _close(conn)
    try:
        size = int(merged.get("sample_size", 200) or 0)
    except (TypeError, ValueError):
        size = 200
    with _samples_lock:
        if _samples.maxlen != size:
            _samples = collections.deque(_samples, maxlen=size)
    msg = "已重载 %d 项: %s" % (len(changed), ", ".join(changed[:8]) or "无变化")
    if structural:
        msg += " (需重启才生效: %s)" % ", ".join(structural)
    log.info("热重载: %s", msg)
    return True, msg, bool(structural)


def _known_upstreams(cfg=None):
    cfg = CONFIG if cfg is None else cfg
    known = {"direct", "watt", "chain"}
    return known | set(MIRROR_PREFIX) | set(JSDELIVR_HOSTS) | set(SITE_MIRRORS) \
        | set((cfg.get("custom_mirrors") or {}).keys())


def _validate_upstreams(cfg, errs):
    known = _known_upstreams(cfg)
    for key in ("raw_upstreams", "github_upstreams", "extra_upstreams"):
        for name in cfg.get(key) or []:
            if name not in known:
                errs.append("%s 含未知上游: %s" % (key, name))
    per_host = cfg.get("per_host_upstreams") or {}
    if not isinstance(per_host, dict):
        errs.append("per_host_upstreams 必须是字典: {\"域名\": [上游...]}")
        return
    for host, names in per_host.items():
        if not str(host).strip():
            errs.append("per_host_upstreams 含空域名")
        if not isinstance(names, (list, tuple)) or not names:
            errs.append("per_host_upstreams[%s] 必须是非空列表" % host)
            continue
        for name in names:
            if name not in known:
                errs.append("per_host_upstreams[%s] 含未知上游: %s" % (host, name))


def _validate_ports(cfg, errs):
    for key in ("listen_port", "metrics_port", "chain_port", "watt_port"):
        try:
            val = int(cfg.get(key, 0))
        except (TypeError, ValueError):
            errs.append("%s 不是整数: %r" % (key, cfg.get(key)))
            continue
        if not (0 < val < 65536):
            errs.append("%s 端口号非法: %s" % (key, val))
    if int(cfg.get("listen_port", 0)) == int(cfg.get("metrics_port", 0)):
        errs.append("listen_port 与 metrics_port 不能相同")


def _validate_doh(cfg, errs):
    for idx, ep in enumerate(cfg.get("doh_endpoints") or []):
        item = ep.get("url") if isinstance(ep, dict) else ep
        if not isinstance(item, str) or "{host}" not in item:
            errs.append("doh_endpoints[%d] 缺少 {host} 占位符: %r" % (idx, ep))
        elif cfg.get("enable_ipv6", True) and "{type}" not in item:
            errs.append("doh_endpoints[%d] 缺少 {type} 占位符: %r" % (idx, ep))


def _validate_host_groups(cfg, errs):
    groups = cfg.get("extra_host_groups") or {}
    enabled = cfg.get("extra_host_groups_enabled") or []
    if not isinstance(groups, dict):
        errs.append("extra_host_groups 必须是字典: {\"组名\": [域名...]}")
        return
    if not isinstance(enabled, (list, tuple)):
        errs.append("extra_host_groups_enabled 必须是列表")
        return
    for name in enabled:
        if name not in groups:
            errs.append("extra_host_groups_enabled 含未知分组: %s (可用: %s)"
                        % (name, ", ".join(sorted(groups)) or "无"))


def validate_config(cfg=None):
    """P2: 启动即校验, 给出可读报错而不是运行时才炸

    传入 cfg 可校验一份还没生效的配置(2.3 热重载用它先验证再切换)。
    """
    cfg = CONFIG if cfg is None else cfg
    errs = []
    _validate_upstreams(cfg, errs)
    _validate_ports(cfg, errs)
    _validate_doh(cfg, errs)
    _validate_host_groups(cfg, errs)
    if str(cfg.get("metrics_host") or "").strip() and \
            not cfg.get("metrics_token"):
        errs.append("metrics_host 绑定到非本机地址时必须设置 metrics_token")
    return errs


# ------------------------------------------------------------ 日志
def _log_targets():
    """服务(LocalSystem)可能没有安装目录写权限, 依次回退到临时目录"""
    yield LOG_FILE
    fallback = os.path.join(tempfile.gettempdir(), "%s.log" % APP)
    if fallback != LOG_FILE:
        yield fallback


class JsonFormatter(logging.Formatter):
    """2.2 结构化日志: 单行 JSON, 便于 journald/文件采集与告警"""

    def format(self, record):
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(record.created)),
                   "level": record.levelname,
                   "logger": record.name,
                   "msg": record.getMessage()}
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("host", "upstream", "ms", "bytes"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return record.getMessage()


def setup_logging():
    global LOG_FILE
    level = getattr(logging, str(CONFIG.get("log_level", "INFO")).upper(), logging.INFO)
    log.setLevel(level)
    if str(CONFIG.get("log_format", "text")).lower() == "json":
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S")
    for handler in log.handlers[:]:      # 幂等: 重复调用(测试/服务重启)不叠加
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    stream = sys.stdout or sys.stderr
    if stream is None:                   # pythonw.exe / 无控制台的服务
        log.addHandler(logging.NullHandler())
    else:
        sh = logging.StreamHandler(stream)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    for path in _log_targets():
        try:
            fh = RotatingFileHandler(path, maxBytes=2 << 20, backupCount=3,
                                     encoding="utf-8")
        except Exception:
            continue
        fh.setFormatter(fmt)
        log.addHandler(fh)
        LOG_FILE = path
        break


# ------------------------------------------------------------ 健康度(按域名分组 + 持久化)
def _u_key(scope, name):
    return "%s|%s" % (scope, name)


def _u_entry():
    return {"ewma": None, "fails": 0, "cool_until": 0.0,
            "hist": [], "ok": 0, "fail": 0}


def _hist_add(state, ok):
    hist = state.setdefault("hist", [])
    hist.append(1 if ok else 0)
    window = max(1, int(CONFIG.get("success_window", 20)))
    if len(hist) > window * 2:
        del hist[:len(hist) - window * 2]


def success_rate(state):
    """P1: 滑动窗口成功率 -> (rate, samples)

    hist 缺失(旧 state.json 或测试注入)时视为"已证明且 100%",
    保证向后兼容与手工构造的健康度记录仍可参与排序。
    """
    hist = state.get("hist")
    if hist is None:
        if state.get("ewma") is None:
            return (None, 0)
        return (1.0, int(CONFIG.get("success_min_samples", 3)))
    if not hist:
        return (None, 0)
    window = max(1, int(CONFIG.get("success_window", 20)))
    recent = hist[-window:]
    return (sum(recent) / float(len(recent)), len(recent))


def _u_ok(scope, name, elapsed):
    with _ustat_lock:
        s = _ustat.setdefault(_u_key(scope, name), _u_entry())
        s["ewma"] = elapsed if s.get("ewma") is None else (0.7 * s["ewma"] + 0.3 * elapsed)
        s["fails"] = 0
        s["cool_until"] = 0.0
        s["ok"] = int(s.get("ok", 0)) + 1
        _hist_add(s, True)


def _u_fail(scope, name, cooldown=None):
    """cooldown: 连续失败达到 direct_fail_max 后使用的长冷却(如 direct_cooldown)"""
    with _ustat_lock:
        s = _ustat.setdefault(_u_key(scope, name), _u_entry())
        s["fails"] = int(s.get("fails", 0)) + 1
        s["fail"] = int(s.get("fail", 0)) + 1
        hard = float(cooldown or 0)
        if hard > 0 and s["fails"] >= int(CONFIG.get("direct_fail_max", 3)):
            s["cool_until"] = time.time() + hard
        else:
            s["cool_until"] = time.time() + min(30 * s["fails"], 300)
        _hist_add(s, False)


def _u_score(state, now):
    """排序键 (档位, 分数): 0 正常 / 1 未证明 / 2 成功率过低 / 3 冷却中"""
    cool_until = state.get("cool_until", 0.0)
    if now < cool_until:
        return (3, max(cool_until - now, 0.0))
    rate, samples = success_rate(state)
    floor = float(CONFIG.get("success_floor", 0.5))
    if state.get("ewma") is None or rate is None or \
            samples < int(CONFIG.get("success_min_samples", 3)):
        return (1, 0.0)
    if rate <= floor:
        return (2, -rate)          # 同为低成功率时, 好的排前面
    # 期望耗时: 慢但稳 与 快但常超时 的差别在于失败要多付一次回落代价
    return (0, state["ewma"] / max(rate, floor))


def order_upstreams(scope, names):
    now = time.time()

    def key(name):
        state = _ustat.get(_u_key(scope, name))
        if not state:
            return (1, 0.0)
        return _u_score(state, now)
    return sorted(names, key=key)


def save_state():
    try:
        with _ipcache_lock, _ustat_lock, _doh_lock:
            data = {"ipcache": _ipcache, "ustat": _ustat, "doh": _doh,
                    "ts": time.time()}
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE)
    except Exception as exc:
        log.debug("保存状态失败: %s", exc)


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        with _ipcache_lock, _ustat_lock, _doh_lock:
            _ipcache.update(data.get("ipcache", {}))
            _ustat.update(data.get("ustat", {}))
            _doh.update(data.get("doh", {}))
        log.info("已恢复上游健康度状态")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.debug("读取状态失败: %s", exc)


# ------------------------------------------------------------ DNS(P2: IPv6, P3: 端点健康度)
def doh_endpoints():
    """P3: 上游 DoH 端点可插拔

    支持两种写法, 统一成 {"url","name","enabled"}:
      1. "https://dns.alidns.com/resolve?name={host}&type={type}"
      2. {"url": "...", "name": "alidns", "enabled": false}
    """
    raw = CONFIG.get("doh_endpoints") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for idx, item in enumerate(raw):
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            name = str(item.get("name") or "").strip()
            enabled = bool(item.get("enabled", True))
        else:
            url = str(item or "").strip()
            name = ""
            enabled = True
        if not url:
            continue
        if not name:
            name = urllib.parse.urlsplit(url).hostname or ("doh%d" % idx)
        out.append({"url": url, "name": name, "enabled": enabled})
    return out


def _doh_stat(name):
    return _doh.setdefault(name, {"ok": 0, "fail": 0,
                                  "ewma": None, "cool_until": 0.0})


def _doh_ok(name, elapsed):
    with _doh_lock:
        s = _doh_stat(name)
        s["ok"] = int(s.get("ok", 0)) + 1
        s["ewma"] = elapsed if s.get("ewma") is None else (0.7 * s["ewma"] + 0.3 * elapsed)
        s["cool_until"] = 0.0


def _doh_fail(name):
    with _doh_lock:
        s = _doh_stat(name)
        s["fail"] = int(s.get("fail", 0)) + 1
        s["cool_until"] = time.time() + min(30 * s["fail"], 300)


def doh_ordered():
    """P3: 信任度优先于速度 —— 保持配置顺序, 只把连续失败的端点挪到末尾

    (DoH 结果直接决定"真实 IP", 让"快但可能被污染"的端点抢到首位是不划算的)
    """
    eps = [e for e in doh_endpoints() if e["enabled"]]
    now = time.time()

    def rank(ep):
        with _doh_lock:
            s = _doh.get(ep["name"]) or {}
        cool_until = s.get("cool_until", 0.0)
        if cool_until and now < cool_until:
            return (1, cool_until - now)
        return (0, 0.0)
    return sorted(eps, key=rank)


def _doh_once(url):
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-json",
                      "User-Agent": "%s/%s" % (APP, VERSION)})
    with _OPENER.open(req, timeout=CONFIG.get("doh_timeout", 6)) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out = []
    for ans in data.get("Answer", []):
        try:
            rtype = int(ans.get("type", 0))
        except (TypeError, ValueError):
            continue
        val = str(ans.get("data", ""))
        if rtype == 1 and val and val[0].isdigit():
            out.append((val, socket.AF_INET))
        elif rtype == 28 and ":" in val:
            out.append((val, socket.AF_INET6))
    return out


def _doh_query(endpoint, rtype, host):
    """查一次并记账; 无结果(被污染/被劫持)也算失败"""
    try:
        url = endpoint["url"].format(host=host, type=rtype)
    except Exception:
        _doh_fail(endpoint["name"])
        return []
    start = time.time()
    try:
        found = _doh_once(url)
    except Exception:
        _doh_fail(endpoint["name"])
        return []
    if found:
        _doh_ok(endpoint["name"], time.time() - start)
    else:
        _doh_fail(endpoint["name"])
    return found


def doh_resolve(host):
    types = ["A"]
    if CONFIG.get("enable_ipv6", True):
        types.append("AAAA")
    eps = doh_ordered()
    if not eps:
        return []
    groups = [eps[:DOH_PARALLEL], eps[DOH_PARALLEL:]]
    found = []
    for group in groups:
        if not group:
            continue
        tasks = [(ep, rtype) for ep in group for rtype in types]
        seen_types = set()
        with futures.ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
            for (ep, rtype), res in zip(tasks, pool.map(
                    lambda a: _doh_query(a[0], a[1], host), tasks)):
                if res:
                    seen_types.add(rtype)
                    found.extend(res)
        if seen_types >= set(types):      # 首选组已覆盖全部记录类型, 不再打扰后面的端点
            break
    # 去重保序
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def doh_probe(host="github.com"):
    """P3: 周期性探活, 让端点健康度在空闲时也能收敛"""
    for ep in doh_ordered():
        _doh_query(ep, "A", host)


def _verify_ip(host, ip, family, timeout):
    ctx = ssl.create_default_context()
    start = time.time()
    try:
        raw = socket.create_connection((ip, 443), timeout=timeout)
    except Exception:
        return None
    try:
        tls = ctx.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
        tls.settimeout(timeout)
        tls.do_handshake()
        tls.close()
        return time.time() - start
    except Exception:
        return None


def _warm_async(host):
    if host in _warming:
        return
    _warming.add(host)

    def run():
        try:
            pick_ips(host, force=True)
        except Exception:
            pass
        finally:
            _warming.discard(host)
    threading.Thread(target=run, daemon=True).start()


def pick_ips(host, force=False):
    with _ipcache_lock:
        cached = _ipcache.get(host)
    ttl = CONFIG.get("refresh_interval", 300)
    if not force:
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]
        _warm_async(host)
        return cached[1] if cached else []
    pairs = doh_resolve(host)
    if not pairs:
        log.info("resolve %s: DoH 无结果", host)
        return cached[1] if cached else []

    def work(item):
        ip, fam = item
        return ip, fam, _verify_ip(host, ip, fam, CONFIG.get("verify_timeout", 4))
    good = []
    with futures.ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
        for ip, fam, el in pool.map(work, pairs):
            if el is not None:
                good.append((el, ip, fam))
    good.sort()
    ranked = [[ip, fam] for _el, ip, fam in good]
    if ranked:
        with _ipcache_lock:
            _ipcache[host] = (time.time(), ranked)
        log.info("verified %s -> %s", host, ",".join(i[0] for i in ranked[:3]))
    elif cached:
        return cached[1]
    return ranked


def _refresher():
    while True:
        time.sleep(CONFIG.get("refresh_interval", 300))
        try:
            for host in sorted(refresh_hosts()):
                try:
                    pick_ips(host, force=True)
                except Exception:
                    pass
            doh_probe()
            save_state()
        except Exception:
            pass


# ------------------------------------------------------------ 上游
JSDELIVR_HOSTS = {
    "jsdelivr": "cdn.jsdelivr.net",
    "jsdelivr_fastly": "fastly.jsdelivr.net",
    "jsdelivr_gcore": "gcore.jsdelivr.net",
    "jsdelivr_cf": "testingcf.jsdelivr.net",
}

# raw 文件加速镜像库: 前缀式 = 把完整原链接拼在后面; 域名替换式 = 直接换掉 raw 域名
# (镜像站存活状态会变, 健康度排序会自动把失效的沉到队尾, 无需手动维护顺序)
MIRROR_PREFIX = {
    "ghproxy_com": "https://gh-proxy.com/https://raw.githubusercontent.com",
    "ghproxy": "https://ghproxy.net/https://raw.githubusercontent.com",
    "ghproxy_homeboyc": "https://ghproxy.homeboyc.cn/https://raw.githubusercontent.com",
    "mirror_ghproxy": "https://mirror.ghproxy.com/https://raw.githubusercontent.com",
    "ghfast": "https://ghfast.top/https://raw.githubusercontent.com",
    "ghp_ci": "https://ghp.ci/https://raw.githubusercontent.com",
    "gitdl": "https://gitdl.cn/https://raw.githubusercontent.com",
    "moeyy": "https://github.moeyy.xyz/https://raw.githubusercontent.com",
    "llkk": "https://gh.llkk.cc/https://raw.githubusercontent.com",
    "akams": "https://github.akams.cn/https://raw.githubusercontent.com",
    "jiasu": "https://gh.jiasu.in/https://raw.githubusercontent.com",
    "mirror7ed": "https://7ed.net/https://raw.githubusercontent.com",
    "wget_la": "https://wget.la/https://raw.githubusercontent.com",
    # 域名替换式
    "gitmirror": "https://raw.gitmirror.com",
    "kkgithub": "https://raw.kkgithub.com",
}

# ---------------------------------------------------------------- 通用镜像库
# 非 GitHub 站点: 同样是前缀式, 但路径是"目标站点自身的路径",
# 因此只能搭配 per_host_upstreams 使用(见 BUILTIN_PER_HOST_UPSTREAMS)。
# 均为 2026-10 在受限网络下实测(HTTPS 可取到 200/206)后入库:
#   huggingface.co 直连证书校验失败 -> hf-mirror.com 可用;
#   registry.npmjs.org / proxy.golang.org / repo.anaconda.com / nodejs.org
#   握手超时或极慢 -> 对应国内镜像可用。
SITE_MIRRORS = {
    # AI 模型 / 数据集
    "hf_mirror": "https://hf-mirror.com",              # huggingface.co(API/页面/resolve)
    # Python
    "pypi_tuna": "https://pypi.tuna.tsinghua.edu.cn",   # pypi.org/simple
    "pypi_aliyun": "https://mirrors.aliyun.com/pypi",   # pypi.org/simple
    # JS
    "npmmirror": "https://registry.npmmirror.com",      # registry.npmjs.org
    # 注意: jsdelivr_fastly 已用于 raw 镜像(会重写成 /gh/{owner}/{repo}@{ref}/...),
    # 这里换一个名字, 表示"原样透传路径"的 CDN 反代
    "jsdelivr_fastly_cdn": "https://fastly.jsdelivr.net",
    "jsd_ms": "https://jsd.onmicrosoft.cn",             # cdn.jsdelivr.net 国内反代
    # Go / Rust
    "goproxy_cn": "https://goproxy.cn",                 # proxy.golang.org
    "goproxy_ali": "https://mirrors.aliyun.com/goproxy",
    "crates_rsproxy": "https://rsproxy.cn/index",       # index.crates.io
    # Conda
    "conda_tuna": "https://mirrors.tuna.tsinghua.edu.cn/anaconda",        # repo.anaconda.com
    "conda_cloud_tuna": "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud",  # conda.anaconda.org
    # 运行时发行版
    "nodejs_npmmirror": "https://registry.npmmirror.com/-/binary/node",   # nodejs.org/dist
    # 字体(两组成对: CSS 与字体文件必须由同一镜像提供, 否则 CSS 里的 gstatic 链接仍被墙)
    "fonts_cn": "https://fonts.googleapis.cn",          # fonts.googleapis.com
    "gstatic_cn": "https://fonts.gstatic.cn",           # fonts.gstatic.com
    "fonts_loli": "https://fonts.loli.net",
    "gstatic_loli": "https://gstatic.loli.net",
}

# 少数镜像的路径前缀与官方不一致, 拼接前先剥掉官方前缀:
#   nodejs.org/dist/v20/x  ->  registry.npmmirror.com/-/binary/node/v20/x
SITE_MIRROR_STRIP = {"nodejs_npmmirror": "/dist"}

# 内置按域名上游链: 只有在用户没显式配置该域名时才生效(用户配置优先)
BUILTIN_PER_HOST_UPSTREAMS = {
    "huggingface.co": ["hf_mirror", "direct"],
    "cdn-lfs.huggingface.co": ["direct"],      # LFS 路径与镜像不一致, 只能直连/走 chain
    "pypi.org": ["pypi_tuna", "pypi_aliyun", "direct"],
    "files.pythonhosted.org": ["direct"],      # 实测可直连(清华未镜像 /packages)
    "registry.npmjs.org": ["npmmirror", "direct"],
    "cdn.jsdelivr.net": ["jsdelivr_fastly_cdn", "jsd_ms", "direct"],
    "proxy.golang.org": ["goproxy_cn", "goproxy_ali", "direct"],
    "index.crates.io": ["crates_rsproxy", "direct"],
    "static.crates.io": ["direct"],
    "repo.anaconda.com": ["conda_tuna", "direct"],
    "conda.anaconda.org": ["conda_cloud_tuna", "direct"],
    "nodejs.org": ["nodejs_npmmirror", "direct"],
    "fonts.googleapis.com": ["fonts_cn", "fonts_loli", "direct"],
    "fonts.gstatic.com": ["gstatic_cn", "gstatic_loli", "direct"],
}


def mirror_url(name, path):
    custom = CONFIG.get("custom_mirrors", {}) or {}
    if name in custom:
        return str(custom[name]).replace("{path}", path)
    if name in SITE_MIRRORS:                    # 通用镜像: 路径需按官方前缀剥离
        strip = SITE_MIRROR_STRIP.get(name)
        if strip and path.startswith(strip):
            path = path[len(strip):]
        return SITE_MIRRORS[name] + path
    prefix = MIRROR_PREFIX.get(name)
    if prefix:
        return prefix + path
    if name in JSDELIVR_HOSTS:
        parts = path.lstrip("/").split("/")
        if len(parts) < 4:
            return None
        owner, repo = urllib.parse.quote(parts[0]), urllib.parse.quote(parts[1])
        rest = parts[2:]
        if rest and rest[0] == "refs" and len(rest) >= 3:
            rest = rest[2:]
        ref, tail = urllib.parse.quote(rest[0]), "/".join(rest[1:])
        if not tail:
            return None
        return "https://%s/gh/%s/%s@%s/%s" % (JSDELIVR_HOSTS[name], owner, repo, ref, tail)
    return None


class OriginConnection(httpclient.HTTPSConnection):
    """拨号到任意地址, Host/SNI 恒为真实域名; 可选先经代理 CONNECT"""

    def __init__(self, host, addr, timeout, ctx, tunnel_target=None, family=socket.AF_INET):
        super().__init__(host, port=addr[1], timeout=timeout, context=ctx)
        self._target_addr = addr
        self._tunnel_target = tunnel_target
        self._family = family
        self._used_at = time.time()

    def connect(self):
        raw = socket.create_connection(self._target_addr, timeout=self.timeout)
        if self._tunnel_target:
            th, tp = self._tunnel_target
            raw.sendall(b"CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                        % (th.encode(), tp, th.encode(), tp))
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                ch = raw.recv(1)
                if not ch:
                    break
                buf += ch
            first = buf.split(b"\r\n")[0] if buf else b""
            if b" 200 " not in first:
                raise RuntimeError("CONNECT via chain failed: %s" % first[:60])
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _ssl_ctx(verify=True):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _close(conn):
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def sock_alive(conn):
    """P1: 轻量探活 —— 上游单方面关闭后内核已收到 FIN/RST, 这里不会阻塞

    - 明文连接: 直接 MSG_PEEK, 读到 b"" 即已被对端关闭;
    - TLS 连接: SSL_read 不能 PEEK, 因此"可读"即认为不可安全复用
      (空闲的健康连接必然不可读; 可读意味着对端发了 close_notify 或多余数据)。
    """
    sock = getattr(conn, "sock", None)
    if sock is None:                 # http.client 已因 Connection: close 关闭
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except Exception:
        return False
    if not readable:
        return True
    if isinstance(sock, ssl.SSLSocket):
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except Exception:
        return False


def pool_size():
    """池中连接总数(1.3 起每个 key 可缓存多条)"""
    with _pool_lock:
        total = 0
        for item in _pool.values():
            if isinstance(item, list):
                total += len(item)
            elif item:
                total += 1
        return total


def _pool_limits():
    try:
        per_key = int(CONFIG.get("pool_max_per_key", 4) or 0)
    except (TypeError, ValueError):
        per_key = 4
    try:
        total = int(CONFIG.get("pool_max_total", 64) or 0)
    except (TypeError, ValueError):
        total = 64
    return per_key, total


def _pool_trim_locked(total):
    """1.3: 全局容量超限 -> 淘汰最久未用的连接(调用方持有 _pool_lock)"""
    if not total:
        return
    while True:
        size = sum(len(bucket) for bucket in _pool.values())
        if size <= total:
            return
        oldest_key, oldest_ts = None, None
        for key, bucket in _pool.items():
            for _conn, ts in bucket:
                if oldest_ts is None or ts < oldest_ts:
                    oldest_key, oldest_ts = key, ts
        if oldest_key is None:
            return
        bucket = _pool[oldest_key]
        victim, _ts = bucket.pop(0)
        if not bucket:
            _pool.pop(oldest_key, None)
        _close(victim)


def _pool_get(key):
    """1.3: 每个 key 可缓存多条连接, 取最新的一条; 坏连接逐个丢弃"""
    if not CONFIG.get("pool_enabled", True):
        return None
    with _pool_lock:
        bucket = _pool.pop(key, None) or []
    if not bucket:
        _count("pool_miss")
        return None
    max_idle = CONFIG.get("pool_max_idle", 30)
    probe = CONFIG.get("pool_probe", True)
    conn = None
    while bucket:
        candidate, ts = bucket.pop()          # 最新的一条
        if time.time() - ts > max_idle or (probe and not sock_alive(candidate)):
            _close(candidate)
            continue
        conn = candidate
        break
    for leftover, _ts in bucket:              # 剩下的放回(或超时丢弃)
        if time.time() - _ts > max_idle:
            _close(leftover)
            continue
        with _pool_lock:
            _pool.setdefault(key, []).append((leftover, _ts))
    _count("pool_hit" if conn is not None else "pool_miss")
    if conn is None:
        log.debug("复用未命中: %s", key)
    return conn


def _pool_put(key, conn):
    if not CONFIG.get("pool_enabled", True) or getattr(conn, "sock", None) is None:
        _close(conn)
        return
    per_key, total = _pool_limits()
    with _pool_lock:
        bucket = _pool.setdefault(key, [])
        bucket.append((conn, time.time()))
        while per_key and len(bucket) > per_key:     # 每 key 上限: 淘汰最旧的
            _close(bucket.pop(0)[0])
        if not bucket:
            _pool.pop(key, None)
        _pool_trim_locked(total)                     # 全局上限: LRU


def _exchange(conn, path, method, headers, body, key, host):
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    if resp.status >= 400:
        try:
            resp.read()
        except Exception:
            pass
        raise RuntimeError("http %s" % resp.status)
    return resp, conn, key


def open_direct(host, path, method, headers, body, timeout):
    to = min(timeout, CONFIG.get("direct_timeout", 6))
    state = _ustat.get(_u_key(host, "direct"))
    if state and state.get("fails", 0) >= CONFIG.get("direct_fail_max", 3) and \
            time.time() < state.get("cool_until", 0.0):
        raise RuntimeError("direct on cooldown")
    ips = pick_ips(host)
    if not ips:
        raise RuntimeError("no verified ip yet (warming)")
    errs = []
    for entry in ips[:3]:
        ip = entry[0]
        fam = entry[1] if len(entry) > 1 else socket.AF_INET
        key = ("direct", host, ip)
        stale_last = False
        for attempt in (0, 1):
            conn = _pool_get(key) if attempt == 0 else None
            stale_last = conn is not None
            if conn is None:
                conn = OriginConnection(host, (ip, 443), to, _ssl_ctx(True), family=fam)
            try:
                return _exchange(conn, path, method, headers, body, key, host)
            except Exception as exc:
                _close(conn)
                if stale_last:
                    # P1: 复用连接已失效 -> 同一 IP 换新连接透明重试一次
                    _count("pool_retry")
                    log.info("复用连接失效 %s:%s (%s), 换新连接重试", host, ip,
                             repr(exc)[:40])
                    continue
                errs.append("%s:%s" % (ip, repr(exc)[:40]))
                break
        if not stale_last:
            break                      # 真实失败: 结束 dash, 尽快回落下一个上游
    raise RuntimeError("direct fail: %s" % "; ".join(errs))


def open_watt(host, path, method, headers, body, timeout):
    addr = (CONFIG.get("watt_host", "127.0.0.1"), int(CONFIG.get("watt_port", 443)))
    key = ("watt", host, "")
    last = None
    for attempt in (0, 1):
        conn = _pool_get(key) if attempt == 0 else None
        reused = conn is not None
        if conn is None:
            conn = OriginConnection(host, addr, timeout, _ssl_ctx(False))
        try:
            return _exchange(conn, path, method, headers, body, key, host)
        except Exception as exc:
            _close(conn)
            last = exc
            if reused:
                _count("pool_retry")
                log.info("复用连接失效 watt/%s (%s), 换新连接重试", host, repr(exc)[:40])
                continue
            raise
    raise RuntimeError("watt fail: %r" % (last,))


def open_chain(host, path, method, headers, body, timeout):
    addr = (CONFIG.get("chain_host", "127.0.0.1"), int(CONFIG.get("chain_port", 7890)))
    conn = OriginConnection(host, addr, timeout, _ssl_ctx(True), tunnel_target=(host, 443))
    return _exchange(conn, path, method, headers, body, None, host)


def open_mirror(name, path, method, headers, body, timeout):
    url = mirror_url(name, path)
    if not url:
        raise RuntimeError("mirror %s 不支持该路径" % name)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    resp = _OPENER.open(req, timeout=timeout)
    if getattr(resp, "status", 200) >= 400:
        resp.read()
        raise RuntimeError("http %s" % resp.status)
    return resp, None, None


OPENERS = {"direct": open_direct, "watt": open_watt, "chain": open_chain}


def group_hosts():
    """已启用的境外站点分组 -> 主机集合(整组开关, 见 extra_host_groups)"""
    groups = CONFIG.get("extra_host_groups") or {}
    enabled = CONFIG.get("extra_host_groups_enabled") or []
    if not isinstance(groups, dict) or not isinstance(enabled, (list, tuple)):
        return set()
    hosts = set()
    for name in enabled:
        for host in groups.get(name) or []:
            if str(host).strip():
                hosts.add(str(host).strip().lower())
    return hosts


def extra_hosts():
    return {str(h).lower() for h in CONFIG.get("extra_hosts", [])} | group_hosts()


def per_host_config():
    """用户的 per_host_upstreams + 内置链

    内置链只对"已被接管的域名"生效(启用分组或写入 extra_hosts), 避免悄悄接管
    用户没要求的站点; 用户显式配置的域名/通配优先级最高。
    """
    cfg = CONFIG.get("per_host_upstreams") or {}
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    managed = extra_hosts()
    for host, names in BUILTIN_PER_HOST_UPSTREAMS.items():
        if host in managed and not any(host_matches(host, pat) for pat in cfg):
            cfg.setdefault(host, list(names))
    return cfg


def _strip_wildcard(pattern):
    pat = str(pattern).strip().lower()
    if pat.startswith("*."):
        pat = pat[2:]
    return pat.lstrip(".")


def host_matches(host, pattern):
    """P2: 域名匹配 —— github.com 精确匹配, *.example.com / .example.com 后缀匹配"""
    if not pattern:
        return False
    base = _strip_wildcard(pattern)
    if not base:
        return False
    host = str(host).strip().lower()
    return host == base or host.endswith("." + base)


def per_host_chain(host):
    """P2: 精确域名优先, 其次最长后缀匹配"""
    hits = [(pattern, names) for pattern, names in per_host_config().items()
            if isinstance(names, (list, tuple)) and names and host_matches(host, pattern)]
    if not hits:
        return None
    hits.sort(key=lambda item: len(_strip_wildcard(item[0])))
    return [str(n) for n in hits[-1][1]]


def refresh_hosts():
    """需要预热/刷新真实 IP 的域名(通配配置无法直接解析)"""
    hosts = set(GH_HOSTS) | extra_hosts()
    for pattern in per_host_config():
        if "*" not in str(pattern):
            hosts.add(_strip_wildcard(pattern))
    return {h for h in hosts if h}


def managed_hosts():
    return GH_HOSTS | RAW_HOSTS | extra_hosts() | refresh_hosts()


def should_intercept(host):
    base = str(host).lower()
    return base in RAW_HOSTS or base in GH_HOSTS or base in extra_hosts() \
        or per_host_chain(base) is not None


def build_chain(host, path):
    per_host = per_host_chain(host)        # P2: 按域名的上游链优先级最高
    if per_host:
        return order_upstreams(host, per_host)
    if host in RAW_HOSTS:
        names = list(CONFIG["raw_upstreams"])
        names += [n for n in CONFIG.get("custom_mirrors", {}) if n not in names]
        return order_upstreams(host, names)
    if host in GH_HOSTS:
        return order_upstreams(host, CONFIG["github_upstreams"])
    if host in extra_hosts():
        return order_upstreams(host, CONFIG["extra_upstreams"])
    return []


# ------------------------------------------------------------ P0: 流式转发
def send_head(writer, status, headers, chunked, length=None):
    lines = ["HTTP/1.1 %d %s" % (status, httpclient.responses.get(status, "OK"))]
    for key, val in headers.items():
        if key.lower() in _RESP_SKIP:
            continue
        lines.append("%s: %s" % (key, val))
    lines.append("Transfer-Encoding: chunked" if chunked
                 else "Content-Length: %d" % (length or 0))
    lines.append("Connection: close")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


class TruncatedError(RuntimeError):
    """P2: 响应体长度与 Content-Length 不符 —— 疑似被中途掐断"""


def _flush(writer):
    flush = getattr(writer, "flush", None)
    if callable(flush):
        try:
            flush()
        except Exception:
            pass


def resp_headers(resp):
    headers = {}
    try:
        for key, val in resp.getheaders():
            headers.setdefault(key, val)
    except Exception:
        pass
    return headers


def expected_length(headers):
    """仅当长度可信(无 Content-Encoding, 未走 chunked)时返回期望字节数"""
    if (headers.get("Content-Encoding") or "").lower():
        return None
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def read_exact(resp, expected):
    """按 Content-Length 读满; 提前 EOF 返回 None"""
    body = bytearray()
    while len(body) < expected:
        chunk = resp.read(min(65536, expected - len(body)))
        if not chunk:
            return None
        body += chunk
    return bytes(body)


def stream_body(writer, resp, prefix, chunked):
    """边收边发; 中途异常不补 chunked 结束标记, 让客户端察觉截断"""
    total = 0
    complete = False
    try:
        chunk = prefix
        while chunk:
            total += len(chunk)
            if chunked:
                writer.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            else:
                writer.write(chunk)
            chunk = resp.read(65536)
        complete = True
    except Exception as exc:
        raise TruncatedError("传输中断于 %d 字节: %r" % (total, exc))
    finally:
        if chunked and complete:
            writer.write(b"0\r\n\r\n")
        _flush(writer)
    return total


def relay_response(writer, resp, method):
    """P0 + P2: 转发响应, 返回 (是否已发响应头, 实际字节数)

    - 已知 Content-Length 且不超过 integrity_buffer_max: 先整体读入并校验长度,
      确认完整才写给客户端 —— 这样"被中途掐断"的响应能透明回落到下一个上游;
    - 大响应/未知长度: 保持流式(先预读第一块, 上游立刻断开仍可回落);
    - 字节数不符时抛 TruncatedError, 且不补上 chunked 结束标记。
    """
    headers = resp_headers(resp)
    expected = expected_length(headers)
    is_head = method.upper() == "HEAD"
    limit = 0
    if CONFIG.get("integrity_check", True):
        try:
            limit = max(0, int(CONFIG.get("integrity_buffer_max", 1 << 20)))
        except (TypeError, ValueError):
            limit = 1 << 20
    if not is_head and expected is not None and 0 < expected <= limit:
        body = read_exact(resp, expected)
        if body is None:
            raise TruncatedError("响应体被截断(Content-Length=%d)" % expected)
        send_head(writer, resp.status, headers, False, len(body))
        if body:
            writer.write(body)
        _flush(writer)
        return True, len(body)
    chunked = expected is None
    prefix = b""
    if not is_head:
        prefix = resp.read(65536)
        if not prefix and expected:
            raise TruncatedError("响应体为空(Content-Length=%d)" % expected)
    send_head(writer, resp.status, headers, chunked,
              None if chunked else expected)
    if is_head:
        if chunked:
            writer.write(b"0\r\n\r\n")
        _flush(writer)
        return True, 0
    total = stream_body(writer, resp, prefix, chunked)
    if expected is not None and total != expected:
        raise TruncatedError("响应体被截断(%d/%d 字节)" % (total, expected))
    return True, total


class BodyStream:
    """把客户端请求体包装成 file-like, 供 http.client 流式上传"""

    def __init__(self, rfile, remaining):
        self._rfile = rfile
        self._remaining = remaining

    def read(self, n=-1):
        if self._remaining <= 0:
            return b""
        if n is None or n < 0 or n > self._remaining:
            n = self._remaining
        data = self._rfile.read(n)
        self._remaining -= len(data)
        return data


# ------------------------------------------------------------ 请求解析
def read_headers(rfile):
    first = rfile.readline()
    if not first:
        return None
    parts = first.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) < 2:
        return None
    headers = {}
    while True:
        line = rfile.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        key, _, val = line.decode("latin-1").rstrip("\r\n").partition(":")
        if key.strip():
            headers.setdefault(key.strip(), []).append(val.strip())
    return parts[0], parts[1], headers


def build_body(rfile, headers):
    for key, vals in headers.items():
        if key.lower() == "content-length":
            try:
                size = int(vals[0])
            except ValueError:
                size = 0
            if size <= 0:
                return b""
            if size <= _STREAM_THRESHOLD:
                return rfile.read(size)
            return BodyStream(rfile, size)     # P0: 大请求体流式上传
    for key, vals in headers.items():
        if key.lower() == "transfer-encoding" and "chunked" in vals[0].lower():
            chunks = []
            while True:
                line = rfile.readline().decode("latin-1").strip()
                if not line:
                    break
                try:
                    size = int(line.split(";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    rfile.readline()
                    break
                chunks.append(rfile.read(size))
                rfile.readline()
            return b"".join(chunks)
    return b""


def flat_headers(headers):
    return {k: v[0] for k, v in headers.items() if k.lower() not in _HOP_HEADERS}


# ------------------------------------------------------------ 连接处理
def tunnel(sock, host, port):
    """纯 TCP 转发(非托管域名的 CONNECT、明文 http://、SSH 等)。

    与 relay_chain 分开计数: 这里没有 HTTP 语义, 也就没有"上游"和响应延迟,
    因此只统计连接数/失败数/字节数, 且**不写入延迟直方图** ——
    隧道可以存活数分钟, 混进 p50/p95 会把 HTTP 请求的延迟分位数彻底带偏。
    """
    remote = None
    started = time.time()
    moved = 0
    _count("tunnel_conns")
    try:
        # SSH(22) 等非 HTTP 端口: 若配置了节点代理的 SOCKS 端口则走它
        if port == 22 and CONFIG.get("chain_socks_port"):
            remote = _socks_connect(CONFIG["chain_host"],
                                    int(CONFIG["chain_socks_port"]), host, port)
        else:
            remote = socket.create_connection((host, port), timeout=20)
    except Exception as exc:
        log.info("tunnel fail %s:%s %s", host, port, exc)
        _count("tunnel_fail")
        sample_add(host=host, upstream="tunnel", method="TCP",
                   path="%s:%d" % (host, port), result="fail",
                   ms=round((time.time() - started) * 1000), bytes=0,
                   detail=repr(exc)[:60])
        try:
            sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except Exception:
            pass
        return
    socks = [sock, remote]
    try:
        while True:
            readable, _, errored = select.select(socks, [], socks, 180)
            if errored or not readable:
                break
            closed = False
            for one in readable:
                other = remote if one is sock else sock
                try:
                    data = one.recv(65536)
                except Exception:
                    closed = True
                    break
                if not data:
                    closed = True
                    break
                other.sendall(data)
                moved += len(data)
            if closed:
                break
    finally:
        _count("tunnel_bytes", moved)
        sample_add(host=host, upstream="tunnel", method="TCP",
                   path="%s:%d" % (host, port), result="ok",
                   ms=round((time.time() - started) * 1000), bytes=moved)
        for one in (remote, sock):
            try:
                one.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                one.close()
            except Exception:
                pass


def split_target(target, host):
    """绝对 URL(HTTP 代理写法)时从 URL 里取域名与路径"""
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urllib.parse.urlsplit(target)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return (parsed.hostname or host), path
    return host, target


def send_502(writer, errors):
    msg = "hublane: all upstreams failed\n%s\n" % "\n".join(errors)
    send_head(writer, 502, {"Content-Type": "text/plain"}, False, len(msg))
    writer.sendall(msg.encode("utf-8", "replace"))


def relay_chain(writer, host, path, method, headers, body, chain):
    """按顺序尝试上游链; 返回 (是否成功, 错误列表)

    响应头一旦发给客户端就不能再回落到下一个上游, 此时直接断开,
    让客户端看到"不完整响应"而不是被静默换源。
    """
    timeout = CONFIG.get("timeout", 60)
    errors = []
    for name in chain:
        t0 = time.time()
        resp = conn = poolkey = None
        committed = False
        try:
            opener = OPENERS.get(name)
            if opener:
                resp, conn, poolkey = opener(host, path, method, headers,
                                             body, timeout)
            else:
                resp, conn, poolkey = open_mirror(name, path, method, headers,
                                                  body, timeout)
            committed, size = relay_response(writer, resp, method)
            elapsed = time.time() - t0
            _u_ok(host, name, elapsed)
            _count("ok")
            _count("bytes", size)
            latency_add(host, name, elapsed)       # 2.1
            sample_add(host=host, upstream=name, method=method,
                       path=path[:80], result="ok", ms=round(elapsed * 1000),
                       bytes=size)
            log.info("ok %s%s <- %s (%.2fs, %dB)", host, path[:36], name,
                     elapsed, size)
            if conn is not None and poolkey:
                _pool_put(poolkey, conn)
            else:
                _close(conn)
            return True, errors
        except TruncatedError as exc:
            # P2: 完整性校验失败 -> 降级该上游并回落到下一个
            _u_fail(host, name, cooldown=CONFIG.get("direct_cooldown", 600))
            _count("fail")
            _count("truncated")
            sample_add(host=host, upstream=name, method=method, path=path[:80],
                       result="truncated", ms=round((time.time() - t0) * 1000),
                       bytes=0, detail=str(exc)[:60])
            errors.append("%s:%s" % (name, exc))
            _close(conn)
            log.warning("响应不完整 %s%s <- %s (%s)", host, path[:36], name, exc)
            if committed:
                log.warning("响应头已发出, 无法透明回落, 主动断开以暴露截断")
                return False, errors
        except Exception as exc:
            _u_fail(host, name, cooldown=CONFIG.get("direct_cooldown", 600))
            _count("fail")
            sample_add(host=host, upstream=name, method=method, path=path[:80],
                       result="fail", ms=round((time.time() - t0) * 1000),
                       bytes=0, detail=repr(exc)[:60])
            errors.append("%s:%s" % (name, repr(exc)[:50]))
            _close(conn)
            if committed:
                return False, errors
    return False, errors


def serve_mitm(sock, host):
    try:
        tls = srv_context().wrap_socket(sock, server_side=True)
    except Exception as exc:
        log.info("tls fail %s: %s", host, exc)
        try:
            sock.close()
        except Exception:
            pass
        return
    try:
        rfile = tls.makefile("rb")
        req = read_headers(rfile)
        if not req:
            return
        method, target, raw_headers = req
        headers = flat_headers(raw_headers)
        body = build_body(rfile, raw_headers)
        host, path = split_target(target, host)

        _count("requests")
        ok, errors = relay_chain(tls, host, path, method, headers, body,
                                 build_chain(host, path))
        if not ok:
            send_502(tls, errors)
    except (ssl.SSLError, OSError, BrokenPipeError, ConnectionResetError) as exc:
        # 客户端提前断开/收尾竞态, 属正常现象, 不刷 ERROR
        log.debug("mitm teardown %s: %s", host, exc)
    except Exception as exc:
        log.info("mitm error %s: %s", host, exc)
    finally:
        try:
            tls.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            tls.close()
        except Exception:
            pass


def _read_line(sock):
    buf = b""
    while len(buf) < 8192:
        try:
            ch = sock.recv(1)
        except Exception:
            return None
        if not ch:
            return None
        buf += ch
        if buf.endswith(b"\r\n"):
            return buf[:-2]
    return None


def _read_head(sock):
    """返回 (请求行, {小写头名: [值...]}) —— 1.2 需要 Proxy-Authorization"""
    first = _read_line(sock)
    if first is None:
        return None
    headers = {}
    while True:
        line = _read_line(sock)
        if line is None or line == b"":
            break
        key, _, val = line.decode("latin-1").partition(":")
        if key.strip():
            headers.setdefault(key.strip().lower(), []).append(val.strip())
    return first.decode("latin-1"), headers


def _basic_password(value):
    """解析 Proxy-Authorization: Basic base64(user:pass) -> password"""
    if not value:
        return ""
    raw = value.strip()
    if raw.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(raw[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return ""
        return decoded.partition(":")[2]
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def proxy_token_ok(headers):
    """1.2: 未设置 proxy_token 时不鉴权(与旧行为一致)"""
    token = str(CONFIG.get("proxy_token") or "")
    if not token:
        return True
    for key, values in (headers or {}).items():
        if key.lower() != "proxy-authorization":
            continue
        for value in values:
            if hmac.compare_digest(_basic_password(value), token):
                return True
    return False


def peer_uid(sock):
    """1.2: 对端进程 uid(SO_PEERCRED, 仅 Linux/WSL); 不支持时返回 None"""
    if IS_WIN or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    except Exception:
        return None


def peer_allowed(sock):
    """1.2: uid 白名单(空 = 不限制)"""
    uids = CONFIG.get("proxy_uid_whitelist") or []
    if not uids:
        return True
    uid = peer_uid(sock)
    if uid is None:                 # 取不到凭据时不做放行判断, 交给 token
        return False
    return uid in {int(u) for u in uids if str(u).lstrip("-").isdigit()}


def send_407(sock):
    body = b"hublane: proxy authentication required\n"
    sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                 b'Proxy-Authenticate: Basic realm="hublane"\r\n'
                 b"Content-Length: %d\r\n\r\n" % len(body) + body)


def _socks_connect(proxy_host, proxy_port, host, port):
    """经 SOCKS5 建立连接(用于 SSH 22 端口转发)"""
    raw = socket.create_connection((proxy_host, proxy_port), timeout=10)
    raw.sendall(b"\x05\x01\x00")
    raw.recv(2)
    hb = host.encode()
    raw.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    raw.recv(4)
    raw.recv(6)
    return raw


def _socks5_auth(sock, methods):
    """1.2: 方法协商 —— 未设 token 走免鉴权, 否则要求用户名/密码(密码即 token)"""
    token = str(CONFIG.get("proxy_token") or "")
    if not token:
        sock.sendall(b"\x05\x00")
        return True
    if 2 not in methods:                 # 客户端不支持用户名/密码
        sock.sendall(b"\x05\xff")
        return False
    sock.sendall(b"\x05\x02")
    if sock.recv(1) != b"\x01":
        return False
    ulen = sock.recv(1)
    if not ulen:
        return False
    sock.recv(ulen[0])                   # 用户名(忽略, 只用密码)
    plen = sock.recv(1)
    if not plen:
        return False
    passwd = sock.recv(plen[0]).decode("utf-8", "replace")
    if not hmac.compare_digest(passwd, token):
        _count("rejected")
        sock.sendall(b"\x01\x01")
        return False
    sock.sendall(b"\x01\x00")
    return True


def handle_socks5(sock):
    """P1: SOCKS5 入站"""
    try:
        ver = sock.recv(1)
        if ver != b"\x05":
            return
        nmethods = sock.recv(1)[0]
        if not _socks5_auth(sock, sock.recv(nmethods)):
            return
        head = sock.recv(4)
        if len(head) < 4:
            return
        _v, cmd, _rsv, atyp = head
        if atyp == 1:
            host = socket.inet_ntoa(sock.recv(4))
        elif atyp == 3:
            ln = sock.recv(1)[0]
            host = sock.recv(ln).decode("latin-1")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, sock.recv(16))
        else:
            return
        port = struct.unpack(">H", sock.recv(2))[0]
        sock.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0")
                     + struct.pack(">H", 0))
        if cmd != 1:
            return
        if port == 443 and should_intercept(host):
            serve_mitm(sock, host)
        else:
            tunnel(sock, host, port)
    except Exception as exc:
        log.debug("socks5 error: %s", exc)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def handle_client(sock, _addr):
    try:
        # 1.1: 客户端侧空闲上限 —— 防止"只连不发"的慢连接长期占住线程
        try:
            idle = float(CONFIG.get("client_timeout", 0) or 0)
            sock.settimeout(idle if idle > 0 else None)
        except (TypeError, ValueError):
            pass
        first_byte = sock.recv(1, socket.MSG_PEEK)
        if first_byte == b"\x05":
            if not peer_allowed(sock):      # 1.2: SOCKS5 侧同样受 uid 白名单约束
                _count("rejected")
                return
            handle_socks5(sock)
            return
        head = _read_head(sock)
        if not head:
            return
        line, req_headers = head
        if not peer_allowed(sock) or not proxy_token_ok(req_headers):
            _count("rejected")              # 1.2: 407 让客户端知道需要凭据
            send_407(sock)
            return
        parts = line.split(" ")
        if parts and parts[0].upper() == "CONNECT":
            netloc = parts[1]
            if netloc.startswith("["):        # [::1]:443
                host, _, rest = netloc[1:].partition("]")
                port_s = rest.lstrip(":")
            else:
                host, _, port_s = netloc.partition(":")
            try:
                port = int(port_s or 443)
            except ValueError:
                sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            intercept = should_intercept(host)
            sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if intercept:
                serve_mitm(sock, host)
            else:
                tunnel(sock, host, port)
            return
        if len(parts) >= 2 and parts[1].startswith("http://"):
            parsed = urllib.parse.urlsplit(parts[1])
            sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunnel(sock, parsed.hostname, parsed.port or 80)
            return
        sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    except Exception as exc:
        log.debug("client error: %s", exc)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def srv_context():
    global _srvctx
    if _srvctx is None:
        with _ctxlock:
            if _srvctx is None:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(CERT, KEY)
                _srvctx = ctx
    return _srvctx


PROBE_RAW_PATH = "/octocat/Hello-World/master/README"


def _probe_one(name, host, path, timeout):
    """用指定上游打开目标并读完响应; 任何失败都抛异常(由调用方记账)"""
    opener = OPENERS.get(name)
    if opener:
        resp, conn, poolkey = opener(host, path, "GET", {}, b"", timeout)
    else:
        resp, conn, poolkey = open_mirror(name, path, "GET", {}, b"", timeout)
    try:
        if resp.status >= 400:
            raise RuntimeError("HTTP %d" % resp.status)
        size = 0
        while True:                      # 读完再放池: 避免残留响应体污染下次复用
            chunk = resp.read(65536)
            if not chunk:
                break
            size += len(chunk)
        return size
    finally:
        if conn is not None and poolkey:
            _pool_put(poolkey, conn)
        else:
            _close(conn)


def probe_specs():
    """1.5: 主动探测目标

    - raw 镜像: 逐个拉一个小文件(与真实流量的健康度同 scope, 可直接参与排序)
    - github.com 与 extra 站点: 走各自的上游链探测, 让"只靠真实流量学习"的链
      也有冷启动数据(顺便预热真实 IP)
    """
    if not CONFIG.get("probe_enabled", True):
        return []
    specs = []
    for name in CONFIG.get("raw_upstreams", []):
        specs.append((RAW_HOST, PROBE_RAW_PATH, [name]))
    specs.append(("github.com", "/", list(CONFIG.get("github_upstreams", []))))
    try:
        limit = int(CONFIG.get("probe_extra_max", 3) or 0)
    except (TypeError, ValueError):
        limit = 0
    for host in list(CONFIG.get("extra_hosts", []))[:limit]:
        specs.append((str(host), "/", list(CONFIG.get("extra_upstreams", []))))
    return specs


def _probe_all():
    try:
        time.sleep(max(0, float(CONFIG.get("probe_delay", 10) or 0)))
    except (TypeError, ValueError):
        time.sleep(10)
    while not _stop.is_set():
        for host, path, names in probe_specs():
            for name in names:
                t0 = time.time()
                try:
                    _probe_one(name, host, path, int(CONFIG.get("timeout", 60)))
                    _u_ok(host, name, time.time() - t0)
                    log.info("probe %s <- %s ok %.2fs", host, name, time.time() - t0)
                except Exception as exc:
                    _u_fail(host, name)
                    log.info("probe %s <- %s fail %s", host, name, repr(exc)[:40])
        save_state()
        try:
            interval = int(CONFIG.get("probe_interval", 0) or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            interval = max(120, int(CONFIG.get("refresh_interval", 300) or 300))
        if _stop.wait(interval):
            break


# ------------------------------------------------------------ P2/P3: 指标 + PAC
def pac_content():
    managed = sorted(GH_HOSTS | RAW_HOSTS | extra_hosts())
    wildcards = sorted({str(p).strip().lower() for p in per_host_config()
                        if "*" in str(p) or str(p).strip().startswith(".")})
    rules = ",\n".join('  "%s": 1' % h for h in managed)
    wild = ",\n".join('    "%s"' % w.lstrip("*").lstrip(".") for w in wildcards)
    return (
        "function FindProxyForURL(url, host) {\n"
        "  var managed = {\n%s\n  };\n"
        "  var managedWild = [\n%s\n  ];\n"
        "  if (managed[host] === 1) return \"PROXY 127.0.0.1:%d\";\n"
        "  for (var i = 0; i < managedWild.length; i++) {\n"
        "    var d = managedWild[i];\n"
        "    if (host === d || dnsDomainIs(host, \".\" + d)) "
        "return \"PROXY 127.0.0.1:%d\";\n"
        "  }\n"
        "  return \"DIRECT\";\n}\n"
    ) % (rules, wild, int(CONFIG.get("listen_port", 8899)),
         int(CONFIG.get("listen_port", 8899)))


def _rel_time(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%ds" % (seconds // 60, seconds % 60)
    return "%dh%dm" % (seconds // 3600, (seconds % 3600) // 60)


def health_rows():
    """上游健康度的可读视图(JSON 与 HTML 面板共用)"""
    now = time.time()
    with _ustat_lock:
        items = [(key, dict(state)) for key, state in _ustat.items()]
    min_samples = int(CONFIG.get("success_min_samples", 3))
    floor = float(CONFIG.get("success_floor", 0.5))
    rows = []
    for key, state in sorted(items):
        scope, _, name = key.partition("|")
        rate, samples = success_rate(state)
        cool_until = state.get("cool_until", 0.0)
        if now < cool_until:
            status, cls = "冷却 %s" % _rel_time(cool_until - now), "bad"
        elif rate is not None and samples >= min_samples and rate <= floor:
            status, cls = "成功率过低", "warn"
        elif state.get("ewma") is None:
            status, cls = "未证明", "dim"
        else:
            status, cls = "正常", "good"
        rows.append({
            "scope": scope, "name": name, "status": status, "cls": cls,
            "ewma": state.get("ewma"),
            "success_rate": rate,
            "samples": samples if state.get("hist") is not None else None,
            "fails": state.get("fails", 0),
            "ok": state.get("ok", 0), "fail": state.get("fail", 0),
        })
    return rows


def doh_rows():
    with _doh_lock:
        items = {k: dict(v) for k, v in _doh.items()}
    now = time.time()
    rows = []
    for ep in doh_endpoints():
        state = items.get(ep["name"], {})
        cool_until = state.get("cool_until", 0.0)
        if not ep["enabled"]:
            status, cls = "已禁用", "dim"
        elif now < cool_until:
            status, cls = "冷却 %s" % _rel_time(cool_until - now), "bad"
        elif state.get("ok"):
            status, cls = "正常", "good"
        elif state.get("fail"):
            status, cls = "不可用", "warn"
        else:
            status, cls = "未验证", "dim"
        rows.append({
            "name": ep["name"], "url": ep["url"], "status": status, "cls": cls,
            "ok": state.get("ok", 0), "fail": state.get("fail", 0),
            "ewma": state.get("ewma"),
        })
    return rows


def status_json():
    with _ipcache_lock:
        ips = {k: v[1][:3] for k, v in _ipcache.items()}
    return json.dumps({
        "app": APP, "version": VERSION,
        "platform": "windows" if IS_WIN else "linux",
        "uptime": _rel_time(time.time() - _started_at),
        "listen": "%s:%s" % (CONFIG.get("listen_host"), CONFIG.get("listen_port")),
        "socks5": CONFIG.get("enable_socks5", True),
        "managed_github": sorted(GH_HOSTS),
        "managed_raw": sorted(RAW_HOSTS),
        "managed_extra": sorted(extra_hosts()),
        "raw_upstreams": CONFIG.get("raw_upstreams"),
        "github_upstreams": CONFIG.get("github_upstreams"),
        "per_host_upstreams": per_host_config(),
        "extra_host_groups_enabled": list(
            CONFIG.get("extra_host_groups_enabled") or []),
        "extra_host_groups_available": sorted(
            (CONFIG.get("extra_host_groups") or {}).keys()),
        "upstream_health": health_rows(),
        "doh_health": doh_rows(),
        "verified_ips": ips,
        "cert_days_left": {label: days for label, _p, days in cert_expiry()},
        "pool_size": pool_size(),
        "counters": counters_snapshot(),
        "latency": latency_stats(),              # 2.1
        "sample_count": len(_samples),
        "integrity_check": bool(CONFIG.get("integrity_check", True)),
        "pool_probe": bool(CONFIG.get("pool_probe", True)),
        "proxy_token": bool(CONFIG.get("proxy_token")),
        "log_file": LOG_FILE,
    }, ensure_ascii=False, indent=2)


_SECRET_KEYS = {"metrics_token", "proxy_token"}


def config_report():
    """2.4: 配置摘要(敏感字段打码), 供 /diag 与 /status 复用"""
    out = {}
    for key in sorted(CONFIG):
        if key in _SECRET_KEYS and CONFIG.get(key):
            out[key] = "***"
        elif key in ("extra_host_groups", "doh_endpoints"):
            value = CONFIG.get(key)
            out[key] = sorted(value) if isinstance(value, dict) \
                else "%d 条" % len(value or [])
        else:
            out[key] = CONFIG.get(key)
    return out


def log_tail(lines=200):
    """2.4: 日志尾部(找不到就说明日志落到了临时目录或仅控制台)"""
    path = LOG_FILE
    if not path or not os.path.exists(path):
        return ["(日志文件不可用: %s)" % (path or "无")]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
    except OSError as exc:
        return ["(读取日志失败: %s)" % exc]
    return [line.rstrip("\n") for line in data[-lines:]]


def diag_text():
    """2.4: 一键诊断包 —— 报障时贴这一份就够了"""
    out = ["hublane 诊断包", "=" * 60,
           "版本: %s" % VERSION,
           "平台: %s / Python %s" % ("windows" if IS_WIN else "linux",
                                   sys.version.split()[0]),
           "监听: %s:%s" % (CONFIG.get("listen_host"), CONFIG.get("listen_port")),
           "运行: %s (启动于 %s)" % (
               _rel_time(time.time() - _started_at),
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_started_at))),
           "配置: %s" % CONF,
           "证书: %s / %s" % (CERT, KEY),
           "", "---- 配置(敏感字段已打码) ----",
           json.dumps(config_report(), ensure_ascii=False, indent=2),
           "", "---- 状态 ----",
           status_json(),
           "", "---- 日志尾部 ----"]
    out.extend(log_tail())
    return "\n".join(out)


def metrics_authorized(given, path):
    """P2: metrics_token 非空时, 所有数据端点都需要 token(请求头或 ?token=)"""
    token = str(CONFIG.get("metrics_token") or "")
    if not token:
        return True
    if not given:
        query = urllib.parse.urlsplit(path).query
        given = (urllib.parse.parse_qs(query).get("token") or [""])[0]
    return hmac.compare_digest(str(given), token)


def panel_html():
    counters = counters_snapshot()

    def cells(row):
        return "".join("<td>%s</td>" % html.escape(str(item)) for item in row)
    health = "\n".join(
        '<tr class="%s">%s</tr>' % (
            r["cls"], cells([r["scope"], r["name"], r["status"],
                             "%.2fs" % r["ewma"] if r["ewma"] is not None else "-",
                             "%.0f%%" % (r["success_rate"] * 100)
                             if r["success_rate"] is not None else "-",
                             r["samples"] if r["samples"] is not None else "-",
                             r["ok"], r["fail"], r["fails"]]))
        for r in health_rows()) or '<tr><td colspan="9">暂无数据</td></tr>'
    doh = "\n".join(
        '<tr class="%s">%s</tr>' % (
            r["cls"], cells([r["name"], r["status"], r["ok"], r["fail"],
                             "%.2fs" % r["ewma"] if r["ewma"] is not None else "-",
                             r["url"]]))
        for r in doh_rows()) or '<tr><td colspan="6">暂无数据</td></tr>'
    with _ipcache_lock:
        ips = sorted((host, [entry[0] for entry in val[1][:3]])
                     for host, val in _ipcache.items())
    ip_rows = "\n".join(
        "<tr>%s</tr>" % cells([host, ", ".join(addrs) or "-"])
        for host, addrs in ips) or '<tr><td colspan="2">暂无数据</td></tr>'
    # 口径说明直接写进 title, 避免"失败数 > 请求数"被误读成 bug:
    #   requests/ok/fail 是 HTTP 层面的口径, fail 数的是"单次上游尝试";
    #   纯 TCP 隧道没有上游与响应延迟, 单独一组 tunnel_* 统计。
    card_specs = (
        ("requests", "HTTP 请求", "经 MITM 解密后处理的 HTTP 请求数(不含纯 TCP 隧道)"),
        ("ok", "上游成功", "某个上游成功回源并返回完整响应"),
        ("fail", "上游失败", "单次上游尝试失败数(一个请求可有多次失败)"),
        ("truncated", "响应截断", "完整性校验发现响应被截断, 已降级上游或主动断开"),
        ("pool_retry", "复用重试", "连接池里的复用连接失效, 换新连接透明重试"),
        ("pool_hit", "池命中", "从连接池复用到可用连接"),
        ("pool_miss", "池未命中", "连接池无空闲连接, 新建直连"),
        ("rejected", "拒绝连接", "鉴权失败 / 并发超限 / SOCKS5 口令错"),
        ("bytes", "回源字节", "经上游回源并转发给客户端的响应体字节"),
        ("tunnel_conns", "隧道连接", "纯 TCP 隧道数: 非托管域名 CONNECT、明文 http://、SSH"),
        ("tunnel_fail", "隧道失败", "隧道建连失败(目标不可达/超时)"),
        ("tunnel_bytes", "隧道字节", "纯 TCP 隧道转发的字节数"),
    )

    def card_value(key):
        raw = counters.get(key, 0)
        return _fmt_bytes(raw) if key.endswith("bytes") else str(raw)

    cards = "".join(
        '<div class="card" title="%s"><div class="k">%s</div>'
        '<div class="v">%s</div></div>'
        % (html.escape(tip), html.escape(name), html.escape(card_value(key)))
        for key, name, tip in card_specs)
    # 2.1: 延迟分位数(直方图桶上界) + 最近请求样本
    stats = latency_stats()

    def fmt_p(value):
        return "-" if value is None else "%.2fs" % value
    global_p = stats["global"]
    cards += "".join(
        '<div class="card"><div class="k">延迟 %s</div><div class="v">%s</div></div>'
        % (label, html.escape(fmt_p(global_p.get(key))))
        for key, label in (("p50", "P50"), ("p95", "P95"), ("p99", "P99")))
    latency_rows = "\n".join(
        "<tr>%s</tr>" % cells([r["host"], r["upstream"], fmt_p(r["p50"]),
                               fmt_p(r["p95"]), fmt_p(r["p99"]), r["count"]])
        for r in stats["by_upstream"]) or '<tr><td colspan="6">暂无数据</td></tr>'
    sample_rows = "\n".join(
        "<tr>%s</tr>" % cells([
            time.strftime("%H:%M:%S", time.localtime(s.get("ts", 0))),
            s.get("host", ""), s.get("upstream", ""), s.get("method", ""),
            s.get("path", ""), s.get("result", ""),
            s.get("ms") if s.get("ms") is not None else "-",
            s.get("bytes", 0)])
        for s in samples(30)) or '<tr><td colspan="8">暂无数据</td></tr>'
    token_hint = ""
    if CONFIG.get("metrics_token"):
        token_hint = ('<p class="dim">已启用 metrics_token: 面板/JSON/PAC 需要 '
                      '?token=... 或 X-Hublane-Token 请求头</p>')
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%(app)s %(ver)s</title>
<meta http-equiv="refresh" content="5">
<style>
 body{background:#12151b;color:#dfe4ec;margin:24px;
      font:13px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif}
 h1{font-size:18px;margin:0 0 4px} h2{font-size:14px;margin:26px 0 8px;color:#8fa4c0}
 .dim{color:#7b8798} table{border-collapse:collapse;width:100%%;margin-top:6px}
 th,td{padding:6px 10px;border-bottom:1px solid #232a36;text-align:left}
 th{color:#8fa4c0;font-weight:500}
 tr.good td:first-child{border-left:3px solid #3ddc84}
 tr.warn td:first-child{border-left:3px solid #ffc857}
 tr.bad  td:first-child{border-left:3px solid #ff6b6b}
 tr.dim  td:first-child{border-left:3px solid #4a5365}
 .cards{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}
 .card{background:#181d26;border:1px solid #232a36;border-radius:8px;
       padding:10px 14px;min-width:96px}
 .card .k{color:#8fa4c0;font-size:12px} .card .v{font-size:19px;margin-top:2px}
 code{background:#181d26;padding:1px 5px;border-radius:4px}
 a{color:#5aa9ff} footer{margin-top:26px;color:#7b8798}
 .inline{display:inline;margin:0}
 .inline button,.inline{font:inherit;color:#5aa9ff;background:none;border:none;
   padding:0;cursor:pointer}
</style></head><body>
<h1>%(app)s <span class="dim">%(ver)s</span></h1>
<p class="dim">平台 %(plat)s · 监听 <code>%(listen)s</code> · SOCKS5 %(socks)s ·
 连接池 %(pool)s · 活动连接 %(conns)s · 运行 %(uptime)s ·
 日志 <code>%(logfile)s</code></p>
<p class="dim">证书剩余: %(certs)s · 访问控制: %(auth)s · 已启用境外站点分组: %(groups)s</p>
%(token)s
<div class="cards">%(cards)s</div>
<h2>上游健康度</h2>
<table><tr><th>域名</th><th>上游</th><th>状态</th><th>EWMA</th><th>成功率</th>
<th>样本</th><th>成功</th><th>失败</th><th>连续失败</th></tr>
%(health)s</table>
<h2>DoH 端点</h2>
<table><tr><th>名称</th><th>状态</th><th>成功</th><th>失败</th><th>EWMA</th>
<th>URL</th></tr>
%(doh)s</table>
<h2>延迟分位数（按上游）</h2>
<table><tr><th>域名</th><th>上游</th><th>P50</th><th>P95</th><th>P99</th>
<th>样本</th></tr>
%(latency)s</table>
<h2>最近请求</h2>
<table><tr><th>时间</th><th>域名</th><th>上游</th><th>方法</th><th>路径</th>
<th>结果</th><th>耗时(ms)</th><th>字节</th></tr>
%(samples)s</table>
<h2>已校验真实 IP</h2>
<table><tr><th>域名</th><th>IP</th></tr>%(ips)s</table>
<footer>JSON: <a href="/status">/status</a> · 样本: <a href="/requests">/requests</a> ·
 诊断包: <a href="/diag">/diag</a> · PAC: <a href="/pac">/pac</a> ·
 存活: <a href="/healthz">/healthz</a> ·
 <form class="inline" method="post" action="/reload"><button>重载配置</button></form>
 · 5 秒自动刷新</footer>
</body></html>
""" % {
        "app": html.escape(APP), "ver": html.escape(VERSION),
        "plat": "windows" if IS_WIN else "linux",
        "listen": html.escape("%s:%s" % (CONFIG.get("listen_host"),
                                         CONFIG.get("listen_port"))),
        "socks": html.escape(str(CONFIG.get("enable_socks5", True))),
        "pool": html.escape(str(pool_size())),
        "conns": html.escape("%s/%s" % (
            active_conns(),
            CONFIG.get("max_conns") or "∞")),
        "latency": latency_rows,
        "samples": sample_rows,
        "groups": html.escape(", ".join(
            CONFIG.get("extra_host_groups_enabled") or []) or "无"),
        "auth": html.escape("已启用 proxy_token" if CONFIG.get("proxy_token")
                            else "未鉴权(仅本机回环)"),
        "certs": html.escape(" · ".join(
            "%s %s天" % (label, days) if days is not None else "%s 未知" % label
            for label, _p, days in cert_expiry())),
        "uptime": html.escape(_rel_time(time.time() - _started_at)),
        "logfile": html.escape(LOG_FILE),
        "token": token_hint, "cards": cards, "health": health, "doh": doh,
        "ips": ip_rows,
    }


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (APP, VERSION)

    def _send(self, code, ctype, body):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.rstrip("/") == "/healthz":
            self._send(200, "text/plain; charset=utf-8", "ok\n")
            return
        if not metrics_authorized(self.headers.get("X-Hublane-Token", ""), self.path):
            self._send(401, "text/plain; charset=utf-8",
                       "401 unauthorized: 需要 metrics_token (请求头 X-Hublane-Token "
                       "或 ?token=)\n")
            return
        if path.startswith("/pac"):
            self._send(200, "application/x-ns-proxy-autoconfig", pac_content())
        elif path.startswith("/status"):
            self._send(200, "application/json; charset=utf-8", status_json())
        elif path.startswith("/requests"):          # 2.1
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(samples(), ensure_ascii=False, indent=2))
        elif path.startswith("/diag"):              # 2.4
            self._send(200, "text/plain; charset=utf-8", diag_text())
        else:
            self._send(200, "text/html; charset=utf-8", panel_html())

    def do_POST(self):
        """2.3: 只有 /reload(受 token 保护), 其余 405"""
        path = urllib.parse.urlsplit(self.path).path
        if not metrics_authorized(self.headers.get("X-Hublane-Token", ""), self.path):
            self._send(401, "text/plain; charset=utf-8", "401 unauthorized\n")
            return
        if not path.startswith("/reload"):
            self._send(405, "text/plain; charset=utf-8", "405 method not allowed\n")
            return
        ok, msg, need_restart = reload_config()
        self._send(200 if ok else 400, "application/json; charset=utf-8",
                   json.dumps({"ok": ok, "message": msg,
                               "restart_required": need_restart},
                              ensure_ascii=False))

    def log_message(self, fmt, *args):
        log.debug("metrics: " + fmt, *args)


# ------------------------------------------------------------ P1: Windows 真服务
# SCM 常量提到模块级: 状态机本身不依赖 ctypes, 这样在 Linux CI 与本地都能直接单测。
SVC_WIN32_OWN_PROCESS = 0x10
SVC_ACCEPT_STOP = 0x01
SVC_STOPPED = 0x01
SVC_STOP_PENDING = 0x03
SVC_RUNNING = 0x04
CTRL_STOP, CTRL_SHUTDOWN, CTRL_INTERROGATE = 0x01, 0x05, 0x04
ERROR_SERVICE_DISABLED = 1063      # 非 SCM 进程启动服务程序


class ServiceCore(object):
    """Windows 服务状态机: 只做状态上报与控制请求分发。

    不 import ctypes, 通过 backend 抽象与 advapi32 通信, 因此可以在任意平台
    用假 backend 驱动测试(见 tests/test_windows_service.py)。
    """

    def __init__(self, backend, service_name=SERVICE_NAME, stop_event=None):
        self.backend = backend
        self.service_name = service_name
        self.stop_event = _stop if stop_event is None else stop_event
        self.handle = None
        self.last = None       # 最近一次上报的字段, 供断言/回读当前状态

    def report(self, state, hint=0, checkpoint=0):
        """上报服务状态。只有拿到控制句柄后才真正通知 SCM。"""
        fields = {"service_type": SVC_WIN32_OWN_PROCESS,
                  "state": state,
                  # 仅 RUNNING 时才接受停止/关闭, 其余状态先拒绝,
                  # 避免还没 RUNNING 就被 stop 打断导致 SCM 判定异常。
                  "controls_accepted": SVC_ACCEPT_STOP if state == SVC_RUNNING else 0,
                  "exit_code": 0,
                  "specific_exit_code": 0,
                  "checkpoint": checkpoint,
                  "wait_hint": hint}
        self.last = fields
        if self.handle:
            self.backend.set_status(self.handle, fields)

    def on_control(self, control):
        """SCM 控制请求回调。返回 0 表示已处理。"""
        if control in (CTRL_STOP, CTRL_SHUTDOWN):
            self.report(SVC_STOP_PENDING, hint=30000, checkpoint=1)
            log.info("收到服务停止请求, 正在退出")
            self.stop_event.set()
        elif control == CTRL_INTERROGATE:
            self.report(self.last["state"] if self.last else SVC_STOPPED)
        return 0


def _win_service(argv):
    """以 Windows 服务方式运行(由 SCM 启动), 支持 sc stop 优雅退出"""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class ServiceStatus(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD),
                    ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD),
                    ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD),
                    ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    handler_t = ctypes.WINFUNCTYPE(None, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, ctypes.c_void_p)
    main_t = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

    class ServiceTableEntry(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR),
                    ("lpServiceProc", main_t)]

    advapi.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    advapi.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, handler_t,
                                                     ctypes.c_void_p]
    advapi.SetServiceStatus.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ServiceStatus)]
    advapi.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ServiceTableEntry)]
    advapi.StartServiceCtrlDispatcherW.restype = wintypes.BOOL

    class AdvapiBackend(object):
        """把 ServiceCore 的字典字段翻译成 advapi32 调用"""

        def register(self, handler):
            return advapi.RegisterServiceCtrlHandlerExW(
                SERVICE_NAME, handler, None)

        def set_status(self, handle, fields):
            status = ServiceStatus()
            status.dwServiceType = fields["service_type"]
            status.dwCurrentState = fields["state"]
            status.dwControlsAccepted = fields["controls_accepted"]
            status.dwWin32ExitCode = fields["exit_code"]
            status.dwServiceSpecificExitCode = fields["specific_exit_code"]
            status.dwCheckPoint = fields["checkpoint"]
            status.dwWaitHint = fields["wait_hint"]
            advapi.SetServiceStatus(handle, ctypes.byref(status))

    core = ServiceCore(AdvapiBackend())
    handler = handler_t(lambda c, _e, _d, _x: core.on_control(c))

    def on_start(_argc, _argv):
        core.handle = core.backend.register(handler)
        core.report(SVC_RUNNING)
        try:
            main([a for a in argv if a != "--service"])
        finally:
            core.report(SVC_STOPPED)

    table = (ServiceTableEntry * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = main_t(on_start)
    if not advapi.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_SERVICE_DISABLED:
            emit("--service 只能由服务控制管理器启动 (sc start %s); "
                 "前台运行请去掉该参数" % SERVICE_NAME, err=True)
        else:
            emit("StartServiceCtrlDispatcher 失败: %d" % err, err=True)
        return 1
    return 0


def find_openssl():
    """定位 openssl(Windows 常见来源是 Git for Windows 或独立安装的 OpenSSL)"""
    candidates = ["openssl",
                  r"C:\Program Files\Git\usr\bin\openssl.exe",
                  r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\openssl.exe"),
                  # 独立安装的 OpenSSL(winget: ShiningLight.OpenSSL.LTS.Light),
                  # 见 tools/setup-windows-env.ps1
                  r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
                  r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe",
                  r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
        if candidate != "openssl" and os.path.exists(candidate):
            return candidate
    return None


def _cert_not_after(path):
    """取证书的 notAfter 字符串; 优先用 ssl 内建解析, 失败回退 openssl"""
    try:
        info = ssl._ssl._test_decode_cert(path)
        if info and info.get("notAfter"):
            return str(info["notAfter"])
    except Exception:
        pass
    exe = find_openssl()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "x509", "-enddate", "-noout", "-in", path],
                             capture_output=True, timeout=15)
        text = out.stdout.decode("utf-8", "replace").strip()
        if text.startswith("notAfter="):
            return text[len("notAfter="):].strip()
    except Exception:
        pass
    return None


def cert_days_left(path):
    """1.4: 证书剩余天数; 解析失败返回 None"""
    not_after = _cert_not_after(path)
    if not not_after:
        return None
    try:
        expires = calendar.timegm(time.strptime(not_after, "%b %d %H:%M:%S %Y %Z"))
    except Exception:
        return None
    return int((expires - time.time()) // 86400)


def cert_expiry():
    """[(标签, 路径, 剩余天数)]"""
    return [("叶证书", CERT, cert_days_left(CERT)),
            ("CA", CA_CRT, cert_days_left(CA_CRT))]


def _warn_cert_expiry():
    try:
        warn_days = int(CONFIG.get("cert_expire_warn_days", 90) or 0)
    except (TypeError, ValueError):
        warn_days = 90
    for label, path, days in cert_expiry():
        if days is None:
            continue
        if days <= 0:
            log.error("%s 已过期(%s), 请执行 --renew-certs", label, path)
        elif days <= warn_days:
            log.warning("%s 将在 %d 天后过期(%s), 可用 --renew-certs%s 续期",
                        label, days, path, "/--renew-ca")


def gen_certs(renew_ca=True, days=CERT_DAYS):
    """1.4: 用 openssl 生成/续期证书; 返回 (是否成功, 说明)

    renew_ca=False 时只换叶证书(保留 CA, 系统里已信任的 CA 无需重装)。
    """
    exe = find_openssl()
    if not exe:
        return False, "未找到 openssl, 无法生成证书"
    try:
        if renew_ca or not (os.path.exists(CA_CRT) and os.path.exists(CA_KEY)):
            subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", CA_KEY, "-out", CA_CRT, "-days", str(days),
                            "-subj", CA_SUBJECT] + CA_ADDEXT,
                           check=True, capture_output=True)
        tmpdir = tempfile.mkdtemp(prefix="hublane-cert-")
        try:
            ext = os.path.join(tmpdir, "san.ext")
            with open(ext, "w", encoding="utf-8") as fh:
                fh.write("subjectAltName=%s\nbasicConstraints=CA:FALSE\n"
                         "extendedKeyUsage=serverAuth\n" % leaf_san())
            subprocess.run([exe, "req", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", KEY, "-out", CSR, "-subj", LEAF_SUBJECT],
                           check=True, capture_output=True)
            subprocess.run([exe, "x509", "-req", "-in", CSR, "-CA", CA_CRT,
                            "-CAkey", CA_KEY, "-CAcreateserial", "-out", CERT,
                            "-days", str(days), "-extfile", ext],
                           check=True, capture_output=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        for path in (CSR, os.path.join(INSTALL_DIR, "ca.srl")):
            try:
                os.remove(path)
            except OSError:
                pass
        for path in (CA_KEY, KEY):
            if os.path.exists(path) and not IS_WIN:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        made = [os.path.basename(CERT), os.path.basename(KEY)]
        if renew_ca:
            made.append(os.path.basename(CA_CRT))
        return True, "已生成 %s (有效期 %d 天)" % (", ".join(made), days)
    except Exception as exc:
        return False, "证书生成失败: %s" % (repr(exc)[:120])


def emit(message, err=False):
    """输出到 stdout/stderr; pythonw 与服务模式下没有控制台, 退化为写日志"""
    stream = sys.stderr if err else sys.stdout
    if stream is not None:
        try:
            stream.write(message + "\n")
            stream.flush()
            return
        except Exception:
            pass
    (log.error if err else log.info)("%s", message)


def _warn_key_perms():
    if IS_WIN:
        return
    try:
        mode = os.stat(KEY).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        log.warning("私钥权限过宽(%o): %s, 建议 chmod 600", mode, KEY)


def _client_worker(conn, addr, limiter):
    try:
        handle_client(conn, addr)
    finally:
        limiter.release()
        try:
            conn.close()
        except Exception:
            pass


def _install_reload_handler():
    """2.3: POSIX 下 SIGHUP 触发热重载(Windows 没有信号, 用 /reload 或面板按钮)"""
    if IS_WIN or not hasattr(signal, "SIGHUP"):
        return False

    def handler(_signum, _frame):
        def run():
            ok, msg, _restart = reload_config()
            log.info("SIGHUP 热重载: %s", msg if ok else "失败 " + msg)
        threading.Thread(target=run, daemon=True).start()
    try:
        signal.signal(signal.SIGHUP, handler)
        return True
    except Exception as exc:            # 非主线程注册会失败, 忽略即可
        log.debug("SIGHUP 处理注册失败: %s", exc)
        return False


def _accept_loop(server):
    global _limiter
    limiter = _limiter = ConnLimiter(CONFIG.get("max_conns", 0))
    server.settimeout(1.0)
    while not _stop.is_set():
        try:
            conn, addr = server.accept()
        except socket.timeout:          # 3.10+ 即 TimeoutError
            continue
        except OSError:
            break
        if not limiter.acquire():       # 1.1: 超过 max_conns 直接拒绝
            _count("rejected")
            log.warning("并发连接已达上限(%s), 拒绝新连接 %s",
                        limiter.limit, addr[0] if addr else "?")
            try:
                conn.close()
            except Exception:
                pass
            continue
        threading.Thread(target=_client_worker, args=(conn, addr, limiter),
                         daemon=True).start()


_metrics_server = None


def _start_metrics():
    """启动指标/PAC 面板(独立守护线程); 失败只告警, 不影响代理主链路"""
    global _metrics_server
    if not CONFIG.get("metrics_enabled", True):
        return
    # metrics_host 为空时回落到 listen_host, 默认只监听回环
    metrics_host = str(CONFIG.get("metrics_host") or "").strip() or \
        CONFIG.get("listen_host", "127.0.0.1")
    try:
        metrics = ThreadingHTTPServer((metrics_host,
                                       int(CONFIG.get("metrics_port", 28898))),
                                      MetricsHandler)
        _metrics_server = metrics
        threading.Thread(target=metrics.serve_forever, daemon=True).start()
        log.info("指标面板 http://%s:%s/ (/status /pac /healthz)",
                 metrics_host, CONFIG.get("metrics_port"))
        if CONFIG.get("metrics_token"):
            log.info("metrics_token 已启用: 面板/JSON/PAC 需要带 token")
    except Exception as exc:
        log.warning("指标端点未启动: %s", exc)


def _stop_metrics():
    """停止指标面板并关闭监听套接字。

    不能只依赖进程退出时的隐式回收: 服务模式里 main() 返回后到解释器真正退出
    之间还有一段窗口, 期间 28898 仍被占用, 会让 sc stop / 重新安装看起来
    "端口没释放"。显式关闭后这段窗口基本消失。
    """
    global _metrics_server
    server = _metrics_server
    _metrics_server = None
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


# ------------------------------------------------------------ main
def main(argv=None):
    global CONF
    parser = argparse.ArgumentParser(prog=APP, description="hublane relay proxy")
    parser.add_argument("--config", default=CONF)
    parser.add_argument("--check", action="store_true", help="仅校验配置后退出")
    parser.add_argument("--service", action="store_true",
                        help="以 Windows 服务方式运行(由 SCM 启动, Windows 专用)")
    parser.add_argument("--renew-certs", action="store_true",
                        help="续期叶证书(保留 CA, 系统里已信任的 CA 无需重装)")
    parser.add_argument("--renew-ca", action="store_true",
                        help="连同 CA 一起续期(需重新安装信任)")
    parser.add_argument("--version", action="version", version="%s %s" % (APP, VERSION))
    args = parser.parse_args(argv)
    if args.config == CONF:      # 未显式指定 --config 时才播种 exe 内置默认值
        seed_bundled("config.json", "ca.crt", "ca.key", "server.crt", "server.key")
    CONF = args.config
    load_config()
    setup_logging()

    if args.service:
        if not IS_WIN:
            emit("--service 仅支持 Windows", err=True)
            return 2
        return _win_service(list(argv if argv is not None else sys.argv[1:]))

    if args.renew_certs or args.renew_ca:
        renew_ca = args.renew_ca
        ok, msg = gen_certs(renew_ca=renew_ca)
        emit(msg, err=not ok)
        if ok and renew_ca:
            emit("CA 已更换: 请重新安装信任 (%s)" % CA_CRT)
        return 0 if ok else 1

    errs = validate_config()
    if errs:
        for e in errs:
            emit("配置错误: %s" % e, err=True)
        return 2
    if args.check:
        emit("配置校验通过")
        return 0
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        emit("缺少证书: %s / %s" % (CERT, KEY), err=True)
        return 1
    _warn_key_perms()
    _warn_cert_expiry()

    load_state()
    _stop.clear()
    _install_reload_handler()          # 2.3: SIGHUP 热重载(Windows 用 /reload)
    threading.Thread(target=_refresher, daemon=True).start()
    threading.Thread(target=_probe_all, daemon=True).start()
    threading.Thread(target=pick_ips, args=("github.com",), daemon=True).start()

    _start_metrics()

    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8899))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(128)
    log.info("%s %s listening on %s:%d (socks5=%s)", APP, VERSION, host, port,
             CONFIG.get("enable_socks5", True))
    try:
        _accept_loop(server)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        _stop_metrics()          # 与 _start_metrics 成对, 确保 28898 立即释放
        server.close()
        save_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())
