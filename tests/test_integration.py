"""hublane 端到端集成测试

不依赖外网: 本地起假上游(明文镜像 + TLS 假 Watt), 再让 hublane 进程内启动,
用真实 TLS 客户端走一遍 MITM 代理链路。

覆盖:
  - 镜像上游端到端转发 / 请求体转发
  - P2 响应被截断时透明回落到下一个上游
  - P1 连接池探活 / 复用连接失效后的透明重试
  - P2 指标鉴权、HTML 面板
  - 未接管域名的纯隧道(不解密)

需要 openssl 生成测试用 CA/叶证书; 缺失时整个模块跳过。
运行:  python -m unittest discover -s tests -v
"""
import base64
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hublane as H  # noqa: E402

OPENSSL_CANDIDATES = [
    "openssl",
    "C:\\Program Files\\Git\\usr\\bin\\openssl.exe",
    "C:\\Program Files (x86)\\Git\\usr\\bin\\openssl.exe",
    os.path.expandvars("%LOCALAPPDATA%\\Programs\\Git\\usr\\bin\\openssl.exe"),
]

RAW_HOST = "raw.githubusercontent.com"


def find_openssl():
    for candidate in OPENSSL_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
        if os.path.exists(candidate):
            return candidate
    return None


OPENSSL = find_openssl()


def make_certs(directory):
    """生成 CA + 叶证书(SAN 覆盖测试域名), 返回路径字典"""
    ca_key = os.path.join(directory, "ca.key")
    ca_crt = os.path.join(directory, "ca.crt")
    leaf_key = os.path.join(directory, "server.key")
    leaf_crt = os.path.join(directory, "server.crt")
    csr = os.path.join(directory, "server.csr")
    ext = os.path.join(directory, "san.ext")
    with open(ext, "w", encoding="utf-8") as fh:
        # 覆盖集成测试里会 MITM 的域名(含非 GitHub 站点, 用于整站换源用例)
        extra = ",".join("DNS:%s" % h for h in (RAW_HOST, "github.com",
                                                "huggingface.co", "pypi.org",
                                                "registry.npmjs.org"))
        fh.write("subjectAltName=%s,DNS:localhost,IP:127.0.0.1\n"
                 "basicConstraints=CA:FALSE\n"
                 "keyUsage=critical,digitalSignature,keyEncipherment\n"
                 "extendedKeyUsage=serverAuth\n" % extra)

    def run(*args):
        return subprocess.run([OPENSSL] + list(args), check=True, capture_output=True)
    # CA 扩展与 hublane.py / install.sh 保持一致: 缺 keyUsage 会被 OpenSSL 3.5+ 拒绝校验
    ca_ext = os.path.join(directory, "ca.ext")
    ca_csr = os.path.join(directory, "ca.csr")
    with open(ca_ext, "w", encoding="utf-8") as fh:
        fh.write("\n".join(H.CA_EXTENSIONS) + "\n")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key,
        "-out", ca_csr, "-subj", "/O=hublane/CN=hublane test CA")
    run("x509", "-req", "-in", ca_csr, "-signkey", ca_key,
        "-extfile", ca_ext, "-days", "2", "-out", ca_crt)
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", leaf_key, "-out", csr,
        "-subj", "/O=hublane/CN=hublane test leaf")
    run("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
        "-CAcreateserial", "-out", leaf_crt, "-days", "2", "-extfile", ext)
    return {"ca": ca_crt, "cert": leaf_crt, "key": leaf_key}


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PlainOrigin(socketserver.ThreadingTCPServer):
    """明文假上游: 用路径前缀模拟正常 / 截断 / 立即断开三种行为"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        super().__init__(("127.0.0.1", 0), PlainHandler)

    @property
    def port(self):
        return self.server_address[1]


class PlainHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            while self._serve_once():
                pass
        except Exception:
            pass

    def _serve_once(self):
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                return False
            head += chunk
        self.server.requests.append(head)
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        target = parts[1] if len(parts) > 1 else "/"
        for prefix, behavior in self.server.routes.items():
            if target.startswith(prefix):
                return self._respond(target, behavior)
        self.request.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        return False

    def _respond(self, target, behavior):
        if behavior == "trunc":
            # 声明 4096 字节, 只发 100 字节就断开 —— 模拟"响应被中途掐断"
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n" + b"x" * 100)
            return False
        body = ("OK %s" % target).encode()
        head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n" % len(body)
        if behavior == "close":
            self.request.sendall(head + b"Connection: close\r\n\r\n" + body)
            return False
        self.request.sendall(head + b"\r\n" + body)
        return True


class TlsOrigin(socketserver.ThreadingTCPServer):
    """TLS 假上游(模拟 Watt): keep-alive 应答, 稍后单方面掐断连接

    linger 秒后关闭 -> hublane 池子里的连接变成"僵尸连接",
    正好用来验证探活(P1)与透明重试(P1)。
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, context, body, linger=0.2):
        self.context = context
        self.body = body
        self.linger = linger
        self.requests = []
        super().__init__(("127.0.0.1", 0), TlsHandler)

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            return self.context.wrap_socket(sock, server_side=True), addr
        except Exception:
            sock.close()
            raise

    @property
    def port(self):
        return self.server_address[1]


class TlsHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        self.server.requests.append(head)
        # 不声明 Connection: close -> http.client 会把连接留在池子里
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s"
            % (len(self.server.body), self.server.body))
        time.sleep(self.server.linger)
        try:
            self.request.close()      # 稍后单方面关闭 -> 复用该连接必然失败
        except Exception:
            pass


@unittest.skipUnless(OPENSSL, "需要 openssl 生成测试证书")
class ProxyTestCase(unittest.TestCase):
    """基类: 一个进程内 hublane 实例 + 一套假上游(子类定义断言)"""

    RAW_UPSTREAMS = ["local_ok"]
    GITHUB_UPSTREAMS = ["watt"]
    EXTRA_CONFIG = {}
    WATT_BODY = b"fake watt payload"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hublane-it-")
        cls.certs = make_certs(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.plain = PlainOrigin({"/ok": "ok", "/trunc": "trunc",
                                  "/close": "close"})
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.certs["cert"], self.certs["key"])
        self.tls_origin = TlsOrigin(ctx, self.WATT_BODY)
        self.origins = [self.plain, self.tls_origin]
        for origin in self.origins:
            threading.Thread(target=origin.serve_forever, daemon=True).start()

        self.listen_port = free_port()
        self.metrics_port = free_port()
        self.config = dict(H.DEFAULTS)
        self.config.update({
            "listen_host": "127.0.0.1",
            "listen_port": self.listen_port,
            "metrics_port": self.metrics_port,
            "raw_upstreams": list(self.RAW_UPSTREAMS),
            "github_upstreams": list(self.GITHUB_UPSTREAMS),
            "watt_host": "127.0.0.1",
            "watt_port": self.tls_origin.port,
            "custom_mirrors": {
                "local_ok": "http://127.0.0.1:%d/ok{path}" % self.plain.port,
                "local_trunc": "http://127.0.0.1:%d/trunc{path}" % self.plain.port,
                "local_close": "http://127.0.0.1:%d/close{path}" % self.plain.port,
            },
            "doh_endpoints": [],
            "enable_ipv6": False,
            "refresh_interval": 3600,
            "log_level": "ERROR",
            "timeout": 10,
        })
        self.config.update(self.EXTRA_CONFIG)
        # 默认关闭后台探测: 只有探测用例需要它, 其余用例不希望被探测流量干扰
        if "probe_enabled" not in self.EXTRA_CONFIG:
            self.config["probe_enabled"] = False
        self.config = self.build_config(self.config)   # 子类可注入动态端口等
        self.cfg_path = os.path.join(self.tmp, "config-%d.json" % self.listen_port)
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh)

        self.patches = []
        for name, value in (("CERT", self.certs["cert"]), ("KEY", self.certs["key"]),
                            ("STATE", os.path.join(self.tmp, "state.json")),
                            ("LOG_FILE", os.path.join(self.tmp, "hublane-it.log"))):
            self.patches.append((name, getattr(H, name)))
            setattr(H, name, value)
        H._srvctx = None
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._ustat_lock:
            H._ustat.clear()
        self._drain_pool()
        with H._counters_lock:
            for key in H._counters:
                H._counters[key] = 0
        self._pick_ips = H.pick_ips
        H.pick_ips = lambda host, force=False: []      # 不触发任何 DNS/外网
        H._stop.clear()
        self.thread = threading.Thread(
            target=H.main, args=(["--config", self.cfg_path],), daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        H._stop.set()
        self.thread.join(timeout=6)
        H.pick_ips = self._pick_ips
        for name, value in self.patches:
            setattr(H, name, value)
        H._srvctx = None
        self._drain_pool()
        for origin in self.origins:
            origin.shutdown()
            origin.server_close()

    def build_config(self, config):
        """钩子: 子类可改写配置(例如注入运行期才知道的端口)"""
        return config

    def read_config(self):
        with open(self.cfg_path, encoding="utf-8") as fh:
            return json.load(fh)

    def write_config(self, data):
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _drain_pool(self):
        """关闭并清空连接池, 避免测试之间串连接/泄漏 fd"""
        with H._pool_lock:
            pooled = [item[0] for item in H._pool.values()]
            H._pool.clear()
        for conn in pooled:
            try:
                conn.close()
            except Exception:
                pass

    def _wait_ready(self):
        # 用指标端点探活: 它不占用代理的并发名额(否则会干扰 max_conns 用例)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, _head, _body = self.http_get("/healthz")
                if status == 200:
                    return
            except OSError:
                pass
            if not self.thread.is_alive():
                self.fail("hublane 线程提前退出")
            time.sleep(0.05)
        self.fail("hublane 未在 10s 内就绪(指标端点 %d)" % self.metrics_port)

    # ---------------- 客户端助手 ----------------
    def connect_tunnel(self, host, port=443):
        """走 HTTP CONNECT 建立隧道, 返回已就绪的明文 socket"""
        raw = socket.create_connection(("127.0.0.1", self.listen_port), timeout=8)
        raw.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                     % (host, port, host, port)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1024)
            if not chunk:
                self.fail("CONNECT 未收到响应")
            head += chunk
        self.assertIn(b"200", head.split(b"\r\n")[0])
        return raw

    def mitm_request(self, host, path, method="GET", body=b"", headers=(),
                     port=None):
        """经 hublane 的 MITM 入口发一个请求, 返回完整响应字节"""
        raw = socket.create_connection(("127.0.0.1", port or self.listen_port),
                                       timeout=8)
        raw.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n"
                     % (host, host)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1024)
            if not chunk:
                self.fail("CONNECT 未收到响应")
            head += chunk
        self.assertIn(b"200", head.split(b"\r\n")[0])
        ctx = ssl.create_default_context(cafile=self.certs["ca"])
        tls = ctx.wrap_socket(raw, server_hostname=host)
        try:
            lines = ["%s %s HTTP/1.1" % (method, path), "Host: %s" % host,
                     "Content-Length: %d" % len(body), "Connection: close"]
            lines += ["%s: %s" % kv for kv in headers]
            tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
            return data
        finally:
            try:
                tls.close()
            except Exception:
                pass

    def mitm_get(self, host, path, **kwargs):
        return self.mitm_request(host, path, **kwargs)

    def wait_active(self, expected, timeout=5):
        """等到活动连接数达到期望值(避免就绪探测连接释放的竞态)"""
        deadline = time.time() + timeout
        while time.time() < deadline and H.active_conns() != expected:
            time.sleep(0.02)
        self.assertEqual(H.active_conns(), expected)

    def http_get(self, path, port=None, headers=()):
        """直连明文端口(指标端点), 返回 (状态码, 头, 体)"""
        addr = ("127.0.0.1", port or self.metrics_port)
        raw = socket.create_connection(addr, timeout=8)
        try:
            lines = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1"]
            lines += ["%s: %s" % kv for kv in headers]
            lines.append("Connection: close")
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while True:
                chunk = raw.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            return int(head.split(b" ")[1]), head.decode("latin-1"), body
        finally:
            raw.close()


class MirrorEndToEndTest(ProxyTestCase):
    def test_mirror_upstream_end_to_end(self):
        data = self.mitm_get(RAW_HOST, "/owner/repo/main/install.sh")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b"OK /ok/owner/repo/main/install.sh", data)
        self.assertGreaterEqual(H.counters_snapshot()["ok"], 1)

    def test_request_body_forwarded(self):
        data = self.mitm_request(RAW_HOST, "/upload", method="POST",
                                 body=b"abc")
        self.assertIn(b"200", data.split(b"\r\n")[0])
        self.assertTrue(self.plain.requests)
        self.assertIn(b"Content-Length: 3", self.plain.requests[-1])


class GithubMirrorTest(ProxyTestCase):
    """github.com 走镜像: 这是 git clone/fetch 能用的前提

    原先 github.com 只有 direct / watt 两个上游。direct 在受限网络里会被
    在正好 128 KiB 处掐断(实测 SSLEOFError, 传输中断于 131072 字节),
    于是任何大于 128 KiB 的响应(pack)都下不来。前缀式镜像由境外取回后
    原样返回, 绕开这个限制 —— 所以 github 必须能走镜像。
    """

    GITHUB_UPSTREAMS = ["local_gh"]

    def build_config(self, cfg):
        cfg["custom_mirrors"]["local_gh"] = \
            "http://127.0.0.1:%d/ok{path}" % self.plain.port
        return cfg

    def test_github_get_via_mirror(self):
        data = self.mitm_get("github.com",
                             "/o/r.git/info/refs?service=git-upload-pack")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b"/o/r.git/info/refs?service=git-upload-pack", data,
                      "路径应原样透传给镜像")
        self.assertTrue(self.plain.requests, "镜像站没收到请求")

    def test_github_post_body_reaches_mirror(self):
        """git-upload-pack 是 POST 带体的, body 必须完整转发"""
        body = b"0014command=fetch\x000000"
        data = self.mitm_request("github.com", "/o/r.git/git-upload-pack",
                                 method="POST", body=body)
        self.assertIn(b"200", data.split(b"\r\n")[0], data[:80])
        self.assertTrue(self.plain.requests)
        self.assertIn(b"Content-Length: %d" % len(body),
                      self.plain.requests[-1],
                      "请求体长度不对, git 会一直等响应")


class TruncatedFallbackTest(ProxyTestCase):
    """P2: 上游把响应掐断时, 客户端仍应拿到完整内容(透明回落)"""

    RAW_UPSTREAMS = ["local_trunc", "local_ok"]

    def test_truncated_upstream_falls_back(self):
        data = self.mitm_get(RAW_HOST, "/owner/repo/main/x.sh")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b"OK /ok/owner/repo/main/x.sh", data)
        self.assertNotIn(b"xxxx", data)
        counters = H.counters_snapshot()
        self.assertGreaterEqual(counters["truncated"], 1)
        self.assertGreaterEqual(counters["ok"], 1)

    def test_upstream_demoted_after_truncation(self):
        self.mitm_get(RAW_HOST, "/a/b/c.sh")
        with H._ustat_lock:
            state = dict(H._ustat[H._u_key(RAW_HOST, "local_trunc")])
        self.assertGreaterEqual(state["fail"], 1)
        self.assertGreater(state["cool_until"], time.time())


class PoolRetryTest(ProxyTestCase):
    """P1: 复用连接被上游单方面关闭时的两条自愈路径"""

    GITHUB_UPSTREAMS = ["watt"]

    def test_probe_discards_dead_connection(self):
        first = self.mitm_get("github.com", "/one")
        time.sleep(self.tls_origin.linger + 0.15)   # 让上游把连接掐断
        second = self.mitm_get("github.com", "/two")
        self.assertTrue(first.startswith(b"HTTP/1.1 200"), first[:80])
        self.assertTrue(second.startswith(b"HTTP/1.1 200"), second[:80])
        self.assertIn(self.WATT_BODY, second)
        self.assertEqual(len(self.tls_origin.requests), 2)
        self.assertEqual(H.counters_snapshot()["pool_retry"], 0)   # 探活已提前丢弃

    def test_transparent_retry_when_probe_disabled(self):
        H.CONFIG["pool_probe"] = False    # 强制走"拿到坏连接 -> 换新连接重试"
        try:
            first = self.mitm_get("github.com", "/one")
            time.sleep(self.tls_origin.linger + 0.15)
            second = self.mitm_get("github.com", "/two")
        finally:
            H.CONFIG["pool_probe"] = True
        self.assertTrue(first.startswith(b"HTTP/1.1 200"), first[:80])
        self.assertTrue(second.startswith(b"HTTP/1.1 200"), second[:80])
        self.assertGreaterEqual(H.counters_snapshot()["pool_retry"], 1)


class ObservabilityTest(ProxyTestCase):
    """可观测端点: /requests · /diag · /reload"""

    EXTRA_CONFIG = {"metrics_token": "t0k3n", "sample_size": 20,
                    "log_level": "ERROR"}
    TOKEN = "t0k3n"

    def get_text(self, path, headers=(), method="GET"):
        return self.http_get(path, headers=headers)

    def post(self, path, headers=()):
        raw = socket.create_connection(("127.0.0.1", self.metrics_port), timeout=8)
        try:
            lines = ["POST %s HTTP/1.1" % path, "Host: 127.0.0.1"]
            lines += ["%s: %s" % kv for kv in headers]
            lines += ["Content-Length: 0", "Connection: close"]
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while True:
                chunk = raw.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            return int(head.split(b" ")[1]), body
        finally:
            raw.close()

    def test_requests_endpoint_requires_token(self):
        self.mitm_get(RAW_HOST, "/obs")
        status, _h, _b = self.get_text("/requests")
        self.assertEqual(status, 401)
        status, _h, body = self.get_text(
            "/requests", headers=[("X-Hublane-Token", self.TOKEN)])
        self.assertEqual(status, 200)
        rows = json.loads(body.decode("utf-8"))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["host"], RAW_HOST)

    def test_status_includes_latency_and_samples(self):
        self.mitm_get(RAW_HOST, "/obs")
        status, _h, body = self.get_text(
            "/status", headers=[("X-Hublane-Token", self.TOKEN)])
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("latency", data)
        self.assertGreaterEqual(data["latency"]["global"]["count"], 1)
        self.assertGreaterEqual(data["sample_count"], 1)

    def test_diag_endpoint(self):
        status, _h, body = self.get_text(
            "/diag", headers=[("X-Hublane-Token", self.TOKEN)])
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("hublane 诊断包", text)
        self.assertIn("---- 状态 ----", text)
        self.assertNotIn("t0k3n", text)          # token 必须打码

    def test_reload_chains_and_rejects_bad_config(self):
        self.mitm_get(RAW_HOST, "/before")
        self.assertIn(b"/ok/before", b"".join(
            r for r in [self.plain.requests[-1]]))

        # 1) 改成走 local_close: 重载后新链立即生效(无需重启)
        cfg = self.read_config()
        cfg["per_host_upstreams"] = {RAW_HOST: ["local_close"]}
        self.write_config(cfg)
        status, body = self.post("/reload",
                                 headers=[("X-Hublane-Token", self.TOKEN)])
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])
        self.mitm_get(RAW_HOST, "/after")
        self.assertIn(b"/close/after", self.plain.requests[-1])

        # 2) 写坏配置: 重载失败(400), 旧配置继续生效
        cfg["raw_upstreams"] = ["no_such_upstream"]
        self.write_config(cfg)
        status, body = self.post("/reload",
                                 headers=[("X-Hublane-Token", self.TOKEN)])
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body.decode("utf-8"))["ok"])
        self.mitm_get(RAW_HOST, "/still")
        self.assertIn(b"/close/still", self.plain.requests[-1])

    def test_reload_requires_token(self):
        status, _body = self.post("/reload")
        self.assertEqual(status, 401)


class SiteMirrorTest(ProxyTestCase):
    """非 GitHub 站点整站换源: 内置链把域名指到镜像(这里用本地假镜像代替)"""

    def build_config(self, config):
        """用本地假镜像代替真实的 hf-mirror / 清华源"""
        config["custom_mirrors"]["local_site"] = \
            "http://127.0.0.1:%d/ok{path}" % self.plain.port
        config["per_host_upstreams"] = {"huggingface.co": ["local_site"],
                                        "pypi.org": ["local_site"]}
        return config

    def test_huggingface_served_by_mirror(self):
        data = self.mitm_get("huggingface.co", "/api/models/bert-base-uncased")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b"OK /ok/api/models/bert-base-uncased", data)
        self.assertEqual(H.per_host_chain("huggingface.co"), ["local_site"])

    def test_pypi_served_by_mirror(self):
        data = self.mitm_get("pypi.org", "/simple/requests/")
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b"OK /ok/simple/requests/", data)

    def test_builtin_chain_points_at_real_mirror(self):
        """未覆盖的域名仍用内置链(这里 huggingface.co 被测试覆盖了, 看 pypi 之外的例子)"""
        self.assertEqual(H.BUILTIN_PER_HOST_UPSTREAMS["pypi.org"],
                         ["pypi_tuna", "pypi_aliyun", "direct"])


class ProxyTokenTest(ProxyTestCase):
    """1.2 本地访问控制: HTTP CONNECT 与 SOCKS5 都需要 proxy_token"""

    EXTRA_CONFIG = {"proxy_token": "t0k3n", "log_level": "ERROR"}
    TOKEN = "t0k3n"

    def _connect(self, extra_headers=()):
        raw = socket.create_connection(("127.0.0.1", self.listen_port), timeout=8)
        lines = ["CONNECT %s:443 HTTP/1.1" % RAW_HOST, "Host: %s:443" % RAW_HOST]
        lines += ["%s: %s" % kv for kv in extra_headers]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1024)
            if not chunk:
                break
            head += chunk
        return raw, head

    def _basic(self, password):
        value = base64.b64encode(("hublane:%s" % password).encode()).decode()
        return [("Proxy-Authorization", "Basic " + value)]

    def test_missing_token_gets_407(self):
        raw, head = self._connect()
        try:
            self.assertIn(b"407", head.split(b"\r\n")[0])
            self.assertIn(b"Proxy-Authenticate", head)
        finally:
            raw.close()
        self.assertGreaterEqual(H.counters_snapshot()["rejected"], 1)

    def test_wrong_token_gets_407(self):
        raw, head = self._connect(self._basic("wrong"))
        raw.close()
        self.assertIn(b"407", head.split(b"\r\n")[0])

    def test_correct_token_passes(self):
        raw, head = self._connect(self._basic(self.TOKEN))
        try:
            self.assertIn(b"200", head.split(b"\r\n")[0])
            tls = ssl.create_default_context(
                cafile=self.certs["ca"]).wrap_socket(raw, server_hostname=RAW_HOST)
            tls.sendall(b"GET /ok/x.sh HTTP/1.1\r\nHost: %s\r\n\r\n"
                        % RAW_HOST.encode())
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"OK /ok/ok/x.sh", data)
        finally:
            raw.close()

    def test_socks5_requires_password(self):
        raw = socket.create_connection(("127.0.0.1", self.listen_port), timeout=8)
        try:
            raw.sendall(b"\x05\x01\x00")          # 只提供"无鉴权"方法
            self.assertEqual(raw.recv(2), b"\x05\xff")
        finally:
            raw.close()

    def test_socks5_auth_success_and_failure(self):
        for password, expected in ((self.TOKEN, b"\x01\x00"), ("bad", b"\x01\x01")):
            raw = socket.create_connection(("127.0.0.1", self.listen_port),
                                           timeout=8)
            try:
                raw.sendall(b"\x05\x01\x02")      # 提供用户名/密码方法
                self.assertEqual(raw.recv(2), b"\x05\x02")
                name, pwd = b"hublane", password.encode()
                raw.sendall(b"\x01" + bytes([len(name)]) + name
                            + bytes([len(pwd)]) + pwd)
                self.assertEqual(raw.recv(2), expected)
            finally:
                raw.close()


class CertLifecycleTest(unittest.TestCase):
    """1.4 证书生命周期: 剩余天数解析 + 续期命令"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hublane-cert-")
        cls.certs = make_certs(cls.tmp)      # CA 2 天 + 叶证书 2 天

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_days_left_for_real_cert(self):
        days = H.cert_days_left(self.certs["cert"])
        self.assertIsNotNone(days)
        self.assertGreaterEqual(days, 0)
        self.assertLessEqual(days, 2)

    def test_days_left_for_ca(self):
        self.assertIsNotNone(H.cert_days_left(self.certs["ca"]))

    def test_missing_file_returns_none(self):
        self.assertIsNone(H.cert_days_left(os.path.join(self.tmp, "nope.crt")))

    def test_expiry_report_shape(self):
        report = [item for item in H.cert_expiry()]
        self.assertEqual(len(report), 2)
        for label, path, days in report:
            self.assertTrue(os.path.isabs(path))

    def test_renew_leaf_keeps_ca(self):
        """临时目录里跑一次续期: CA 指纹不变, 叶证书被替换"""
        work = os.path.join(self.tmp, "renew")
        os.makedirs(work, exist_ok=True)
        shutil.copy(self.certs["ca"], os.path.join(work, "ca.crt"))
        shutil.copy(os.path.join(self.tmp, "ca.key"), os.path.join(work, "ca.key"))
        shutil.copy(self.certs["cert"], os.path.join(work, "server.crt"))
        shutil.copy(self.certs["key"], os.path.join(work, "server.key"))
        before_ca = _fingerprint(os.path.join(work, "ca.crt"))
        before_leaf = _fingerprint(os.path.join(work, "server.crt"))
        saved = {name: getattr(H, name)
                 for name in ("CERT", "KEY", "CA_CRT", "CA_KEY", "CSR")}
        try:
            for name in saved:
                setattr(H, name, os.path.join(work, os.path.basename(saved[name])))
            ok, msg = H.gen_certs(renew_ca=False, days=30)
        finally:
            for name, value in saved.items():
                setattr(H, name, value)
        self.assertTrue(ok, msg)
        self.assertEqual(_fingerprint(os.path.join(work, "ca.crt")), before_ca)
        self.assertNotEqual(_fingerprint(os.path.join(work, "server.crt")),
                            before_leaf)
        self.assertGreater(H.cert_days_left(os.path.join(work, "server.crt")), 25)


def _fingerprint(path):
    """证书指纹(用文件内容哈希即可, 只用于比较是否变化)"""
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class ProbeAllChainsTest(ProxyTestCase):
    """1.5: github 链也参与主动探测(不再只靠真实流量冷启动)"""

    GITHUB_UPSTREAMS = ["watt"]
    EXTRA_CONFIG = {"probe_enabled": True, "probe_delay": 1, "probe_interval": 2,
                    "probe_extra_max": 0, "raw_upstreams": [],
                    "log_level": "ERROR"}

    def _health_row(self, scope, name):
        for item in H.health_rows():
            if item["scope"] == scope and item["name"] == name:
                return item
        return None

    def test_github_chain_probed_without_traffic(self):
        deadline = time.time() + 15
        while time.time() < deadline and self._health_row("github.com", "watt") is None:
            time.sleep(0.1)             # 等探测线程跑完第一轮(probe_delay=1s)
        row = self._health_row("github.com", "watt")
        self.assertIsNotNone(row, "面板健康度里没有 github.com <- watt")
        self.assertEqual(row["status"], "正常")
        self.assertIsNotNone(row["ewma"])
        self.assertTrue(self.tls_origin.requests, "探测线程未访问假 Watt 上游")
        self.assertIn(b"GET / HTTP/1.1", self.tls_origin.requests[0])
        self.assertIn(b"Host: github.com:", self.tls_origin.requests[0])


class ClientConnLimitTest(ProxyTestCase):
    """1.1 客户端连接治理: 并发上限 + 空闲上限(防 slowloris)"""

    EXTRA_CONFIG = {"max_conns": 1, "client_timeout": 2, "log_level": "ERROR"}

    def test_over_limit_connection_rejected(self):
        self.wait_active(0)              # 先等此前的连接全部释放
        held = socket.create_connection(("127.0.0.1", self.listen_port), timeout=5)
        self.wait_active(1)              # 确认 held 占用了唯一名额
        try:
            # 占住唯一名额: 连上但不发请求
            extra = socket.create_connection(("127.0.0.1", self.listen_port),
                                             timeout=5)
            try:
                extra.sendall(b"CONNECT raw.githubusercontent.com:443 HTTP/1.1\r\n\r\n")
                try:
                    data = extra.recv(100)
                except OSError:            # 被直接关闭(RST), 平台差异
                    data = b""
                self.assertEqual(data, b"")   # 无响应即被拒绝
            finally:
                extra.close()
            self.assertGreaterEqual(H.counters_snapshot()["rejected"], 1)
        finally:
            held.close()

    def test_idle_connection_closed_and_slot_freed(self):
        idle = socket.create_connection(("127.0.0.1", self.listen_port), timeout=5)
        try:
            # client_timeout=2, 不发任何数据 -> 应被关闭(读到 EOF)
            self.assertEqual(idle.recv(10), b"")
        finally:
            idle.close()
        deadline = time.time() + 5
        while time.time() < deadline and H.active_conns() > 0:
            time.sleep(0.05)
        self.assertEqual(H.active_conns(), 0)

    def test_slot_released_after_finish(self):
        self.wait_active(0)
        first = self.mitm_get(RAW_HOST, "/one")
        self.assertTrue(first.startswith(b"HTTP/1.1 200"), first[:80])
        second = self.mitm_get(RAW_HOST, "/two")     # 名额已释放, 不应被拒
        self.assertTrue(second.startswith(b"HTTP/1.1 200"), second[:80])
        self.assertEqual(H.counters_snapshot()["rejected"], 0)


class MetricsPanelTest(ProxyTestCase):
    """P2: 指标鉴权 + HTML 面板 + 未接管域名的纯隧道"""

    EXTRA_CONFIG = {"metrics_token": "t0k3n"}

    def test_token_required_and_accepted(self):
        status, _head, _body = self.http_get("/status")
        self.assertEqual(status, 401)
        status, _head, body = self.http_get("/status",
                                            headers=[("X-Hublane-Token", "t0k3n")])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["version"], H.VERSION)
        status, _head, _body = self.http_get("/status?token=t0k3n")
        self.assertEqual(status, 200)

    def test_healthz_needs_no_token(self):
        status, _head, body = self.http_get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"ok")

    def test_panel_renders(self):
        self.mitm_get(RAW_HOST, "/panel")          # 制造一条健康度记录
        status, _head, body = self.http_get("/?token=t0k3n")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn("上游健康度", page)
        self.assertIn("DoH 端点", page)

    def test_tunnel_passthrough_for_unmanaged_host(self):
        raw = self.connect_tunnel("127.0.0.1", self.plain.port)
        try:
            raw.sendall(b"GET /ok/tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertIn(b"OK /ok/tunnel", raw.recv(65536))
        finally:
            raw.close()

    def test_socks5_inbound_reaches_relay(self):
        """P1: SOCKS5 入站(与 HTTP 共端口)到被接管域名应走 MITM"""
        raw = socket.create_connection(("127.0.0.1", self.listen_port), timeout=8)
        try:
            raw.sendall(b"\x05\x01\x00")
            self.assertEqual(raw.recv(2), b"\x05\x00")
            host = RAW_HOST.encode()
            raw.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host
                        + (443).to_bytes(2, "big"))
            self.assertEqual(raw.recv(10)[:2], b"\x05\x00")
            ctx = ssl.create_default_context(cafile=self.certs["ca"])
            tls = ctx.wrap_socket(raw, server_hostname=RAW_HOST)
            tls.sendall(b"GET /socks HTTP/1.1\r\nHost: %s\r\n\r\n"
                        % RAW_HOST.encode())
            data = b""
            while True:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"OK /ok/socks", data)
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
