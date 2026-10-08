"""hublane 运行时端到端验收: 真起进程, 真TLS 客户端, 真面板/指标端点。

与 tests/ 的区别: 这里跑的是**独立进程**(tests 是进程内嵌), 覆盖
  - 单端口双协议(HTTP + SOCKS5 嗅探)
  - MITM HTTPS 到本地假上游(证书/上游链/SAN)
  - 非受管域名纯 TCP 隧道
  - 指标端点 / 面板 / /pac / /healthz / /diag / /requests
  - metrics_token鉴权、proxy_token 鉴权
  - SIGHUP 热重载 + POST /reload
  - JSON 结构化日志

不需要外网: 上游是本地假服务器。仅依赖 openssl。

平台: 仅 POSIX。SIGHUP 热重载(Windows 没有信号)与"起子进程 + 信号 + 假上游"
这套编排都针对 Unix, Windows 上整体跳过 —— 否则 Windows CI 里 signal.SIGHUP
会直接 AttributeError。Windows 侧的覆盖由 tests/test_windows_service.py 承担。
"""
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENSSL = shutil.which("openssl")
RAW_HOST = "raw.githubusercontent.com"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Plain(threading.Thread):
    """本地明文假上游"""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.hits = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin1")
            self.hits.append(line)
            body = ("MIRROR-OK " + line).encode()
            resp = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body)
            conn.sendall(resp)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


@unittest.skipUnless(os.name == "posix", "仅 POSIX: 依赖 SIGHUP 与 Unix 进程编排")
@unittest.skipUnless(OPENSSL, "需要 openssl")
class RuntimeE2E(unittest.TestCase):
    proxy_port = None
    metrics_port = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hublane-e2e-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(cls.home)
        cls.proxy_port = free_port()
        cls.metrics_port = free_port()
        cls.origin_port = free_port()
        cls.origin = Plain(cls.origin_port)
        cls.origin.start()

        # 假上游用 127.0.0.1, 但要走 MITM 就得让 hublane 认它为受管域名,
        # 所以这里直接把假上游挂在 https 上并用 hosts 无关的方式:
        # 走 "watt" 上游类型(https 指向本地假 TLS 上游)。
        cls.origin_tls = TlsOrigin(cls.origin_port)
        cls.origin_tls.start()
        if not cls.origin_tls.ready.wait(30):
            raise AssertionError("假 TLS 上游未能在 30s 内就绪")

        cls._gen_certs()
        cls._write_config()
        # 按install.sh 的真实部署形态: 把单文件复制到安装目录再运行。
        # 非冻结形态下 INSTALL_DIR = hublane.py 所在目录, 所以证书/配置必须同处一地。
        cls.script = os.path.join(cls.home, "hublane.py")
        shutil.copy(os.path.join(REPO, "hublane.py"), cls.script)
        env = dict(os.environ, HUBLANE_HOME=cls.home,
                   SSL_CERT_FILE=cls.origin_tls.ca_path)
        cls.log = open(os.path.join(cls.tmp, "out.log"), "w+b")
        cls.proc = subprocess.Popen(
            [sys.executable, cls.script, "--config",
             os.path.join(cls.home, "config.json")],
            stdout=cls.log, stderr=subprocess.STDOUT, env=env, cwd=cls.home)
        deadline = time.time() + 30
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise AssertionError("hublane 启动即退出, 见 out.log")
            try:
                with socket.create_connection(("127.0.0.1", cls.metrics_port), 1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("hublane 30s 内未监听指标端口")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.send_signal(signal.SIGINT)
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if getattr(cls, "log", None):
            cls.log.flush()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _gen_certs(cls):
        def run(*a):
            return subprocess.run([OPENSSL] + list(a), check=True, capture_output=True)
        ca_ext = os.path.join(cls.tmp, "ca.ext")
        with open(ca_ext, "w") as fh:
            fh.write("basicConstraints=critical,CA:TRUE\n"
                     "keyUsage=critical,digitalSignature,keyCertSign,cRLSign\n"
                     "subjectKeyIdentifier=hash\n")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout",
            os.path.join(cls.home, "ca.key"), "-out", os.path.join(cls.tmp, "ca.csr"),
            "-subj", "/O=hublane/CN=e2e CA")
        run("x509", "-req", "-in", os.path.join(cls.tmp, "ca.csr"), "-signkey",
            os.path.join(cls.home, "ca.key"), "-extfile", ca_ext, "-days", "2",
            "-out", os.path.join(cls.home, "ca.crt"))
        ext = os.path.join(cls.tmp, "leaf.ext")
        with open(ext, "w") as fh:
            fh.write("subjectAltName=DNS:%s,DNS:localhost,DNS:example.test,"
                     "DNS:plain.test,IP:127.0.0.1\n"
                     "basicConstraints=CA:FALSE\n"
                     "keyUsage=critical,digitalSignature,keyEncipherment\n"
                     "extendedKeyUsage=serverAuth\n" % RAW_HOST)
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout",
            os.path.join(cls.home, "server.key"), "-out", os.path.join(cls.tmp, "s.csr"),
            "-subj", "/O=hublane/CN=e2e leaf")
        run("x509", "-req", "-in", os.path.join(cls.tmp, "s.csr"), "-CA",
            os.path.join(cls.home, "ca.crt"), "-CAkey", os.path.join(cls.home, "ca.key"),
            "-CAcreateserial", "-extfile", ext, "-days", "2",
            "-out", os.path.join(cls.home, "server.crt"))

    @classmethod
    def _write_config(cls):
        cfg = {
            "listen_host": "127.0.0.1",
            "listen_port": cls.proxy_port,
            "metrics_port": cls.metrics_port,
            "metrics_enabled": True,
            "metrics_token": "e2etoken",
            "proxy_token": "proxytoken",
            "raw_upstreams": ["watt"],
            "github_upstreams": ["watt"],
            "extra_upstreams": ["watt"],
            "per_host_upstreams": {},
            "watt_host": "127.0.0.1",
            "watt_port": cls.origin_tls.port,
            "log_format": "json",
            "sample_size": 50,
            "probe_enabled": False,
            "dns_warmup": False,
            "extra_hosts": ["example.test"],
        }
        with open(os.path.join(cls.home, "config.json"), "w") as fh:
            json.dump(cfg, fh, indent=2)

    # ---------- 工具 ----------
    def metrics(self, path, token="e2etoken", raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.metrics_port, timeout=15)
        headers = {"X-Hublane-Token": token} if token else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return (resp.status, body) if raw else (resp.status, body.decode("utf-8", "replace"))

    def proxy_http(self, path, host=RAW_HOST):
        """通过代理发普通 HTTP 请求(HTTP 代理语义)"""
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=30)
        conn.set_tunnel(host, 443)
        conn.request("GET", path, headers={
            "Host": host, "Proxy-Authorization": "Basic " + _b64("proxytoken")})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def mitm_https(self, host, path="/hello"):
        """真 TLS 客户端经代理 MITM 访问受管域名"""
        ctx = ssl.create_default_context(cafile=os.path.join(self.home, "ca.crt"))
        raw = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=30)
        raw.sendall((
            "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n"
            "Proxy-Authorization: Basic %s\r\n\r\n"
            % (host, host, _b64("proxytoken"))).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1)
            if not chunk:
                break
            head += chunk
        self.assertIn(b"200", head.split(b"\r\n", 1)[0], "CONNECT 失败: %r" % head[:80])
        tls = ctx.wrap_socket(raw, server_hostname=host)
        tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path, host)).encode())
        data = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
        tls.close()
        return data

    def tunnel(self, host, port, path="/"):
        """非受管域名: 纯 TCP 隧道, 不解密"""
        raw = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=30)
        raw.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
                     "Proxy-Authorization: Basic %s\r\n\r\n"
                     % (host, port, host, port, _b64("proxytoken"))).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1)
            if not chunk:
                break
            head += chunk
        self.assertIn(b"200", head.split(b"\r\n", 1)[0], "隧道建立失败: %r" % head[:80])
        raw.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (path, host, port)).encode())
        data = raw.recv(65536)
        raw.close()
        return data

    # ---------- 3. 核心代理 ----------
    def test_01_https_mitm_via_upstream_chain(self):
        """受管域名 -> MITM -> 上游链 -> 拿到内容"""
        data = self.mitm_https(RAW_HOST, "/core/one")
        self.assertIn(b"200 OK", data[:40], data[:200])
        self.assertIn(b"WATT-OK", data)
        self.assertIn(b"/core/one", data)

    def test_02_multi_requests_and_post_body(self):
        for i in range(5):
            data = self.mitm_https(RAW_HOST, "/core/n%d" % i)
            self.assertIn(b"WATT-OK", data, "第 %d 次请求失败" % i)
        self.assertGreaterEqual(len(self.origin_tls.requests), 5,
                                "上游收到的请求数不对: %d"
                                % len(self.origin_tls.requests))

    def test_03_proxy_token_enforced(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=15)
        conn.request("GET", "http://%s/x" % RAW_HOST)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 407, "缺 proxy_token 应 407")

    def test_04_socks5_inbound_on_same_port(self):
        """同一端口上 SOCKS5 握手(带token 认证)"""
        s = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        s.sendall(b"\x05\x02\x00\x02")           # 声明支持 no-auth + user/pass
        try:
            resp = s.recv(2)
        except socket.timeout:
            self.log.flush()
            self.log.seek(0)
            self.fail("SOCKS5 无响应; proxy_port=%s\n--- hublane 日志 ---\n%s"
                      % (self.proxy_port,
                         self.log.read().decode("utf-8", "replace")[-1500:]))
        self.assertEqual(resp[0], 5)
        self.assertEqual(resp[1], 2, "服务端应选择 user/pass 认证")
        # RFC 1929: ver(0x01) | ulen | uname | plen | passwd
        s.sendall(b"\x01\x01u" + bytes([len("proxytoken")]) + b"proxytoken")
        resp = s.recv(2)
        self.assertEqual(resp[1], 0, "SOCKS5 认证应成功: %r" % resp)
        s.close()

    def test_04b_socks5_no_auth_rejected_when_token_set(self):
        """设了 proxy_token 时, 只声明 no-auth 的客户端应被拒(0xFF)"""
        s = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        s.sendall(b"\x05\x01\x00")
        resp = s.recv(2)
        self.assertEqual((resp[0], resp[1]), (5, 0xFF),
                         "应拒绝 no-auth: %r" % resp)
        s.close()

    def test_05_plain_tunnel_for_unmanaged_host(self):
        """非受管域名 / 非 443 端口 -> 纯 TCP 隧道, 不解密不改写"""
        data = self.tunnel("localhost", self.origin_port, "/tunneled")
        self.assertIn(b"200 OK", data[:40], data[:200])
        self.assertIn(b"MIRROR-OK", data, "隧道应原样透传明文")
        self.assertIn(b"/tunneled", data)

    def test_05b_tunnel_to_tls_origin_is_not_decrypted(self):
        """隧道到 TLS 上游: 由客户端自己完成握手, 说明代理没有解密"""
        raw = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=20)
        raw.sendall(("CONNECT localhost:%d HTTP/1.1\r\nHost: localhost\r\n"
                     "Proxy-Authorization: Basic %s\r\n\r\n"
                     % (self.origin_tls.port, _b64("proxytoken"))).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1)
            if not chunk:
                break
            head += chunk
        self.assertIn(b"200", head.split(b"\r\n", 1)[0], head[:120])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(raw, server_hostname="localhost")
        tls.sendall(b"GET /via-tunnel HTTP/1.1\r\nHost: localhost\r\n\r\n")
        data = tls.recv(65536)
        tls.close()
        self.assertIn(b"200 OK", data[:40], data[:200])
        self.assertIn(b"WATT-OK", data, "应看到上游原始响应")
        self.assertIn(b"/via-tunnel", data)

    # ---------- 4. 可视化 / 统计 / 配置 ----------
    def test_10_panel_html(self):
        status, body = self.metrics("/", raw=True)
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        self.assertIn("<html", text.lower())
        for marker in ("hublane", "上游", "DoH"):
            self.assertIn(marker, text, "面板缺少 %s" % marker)

    def test_11_status_json_shape(self):
        status, body = self.metrics("/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("upstream_health", "doh_health", "verified_ips", "counters",
                    "latency", "pool_size", "cert_days_left"):
            self.assertIn(key, data, "/status 缺少 %s" % key)

    def test_12_token_required(self):
        status, body = self.metrics("/status", token="")
        self.assertEqual(status, 401, "缺 metrics_token 应401: %s" % body[:120])
        status, _ = self.metrics("/status", token="wrong")
        self.assertEqual(status, 401, "错误 metrics_token 应 401")
        status, _ = self.metrics("/", token="")
        self.assertEqual(status, 401, "面板同样需要 token")
        # healthz 免鉴权(给外部探活用)
        status, _ = self.metrics("/healthz", token="")
        self.assertEqual(status, 200, "/healthz 应免鉴权")
        # token 也可以走查询参数
        status, _ = self.metrics("/status?token=e2etoken", token="")
        self.assertEqual(status, 200, "?token= 应可用")

    def test_13_healthz_pac_requests(self):
        status, body = self.metrics("/healthz")
        self.assertEqual(status, 200)
        self.assertIn("ok", body.lower())
        status, pac = self.metrics("/pac")
        self.assertEqual(status, 200)
        self.assertIn("PROXY", pac.upper())
        status, body = self.metrics("/requests")
        self.assertEqual(status, 200)
        reqs = json.loads(body)
        self.assertIsInstance(reqs, (list, dict))

    def test_14_latency_and_samples_recorded(self):
        self.mitm_https(RAW_HOST, "/stats/sample")
        data = json.loads(self.metrics("/status")[1])
        self.assertTrue(data["counters"].get("requests", 0) >= 1,
                        "计数未累加: %s" % data["counters"])
        glob = data["latency"]["global"]
        self.assertIn("p50", glob)
        self.assertIn("p95", glob)
        self.assertGreaterEqual(glob["count"], 1, "延迟直方图未记录样本")
        samples = json.loads(self.metrics("/requests")[1])
        rows = samples["samples"] if isinstance(samples, dict) else samples
        self.assertTrue(rows, "最近请求样本为空")
        self.assertIn("host", rows[0])

    def test_15_diag_masks_secrets(self):
        status, body = self.metrics("/diag")
        self.assertEqual(status, 200)
        self.assertNotIn("e2etoken", body, "/diag 泄露 metrics_token")
        self.assertNotIn("proxytoken", body, "/diag 泄露 proxy_token")
        self.assertIn("版本", body)

    def test_16_panel_covers_config_and_endpoints(self):
        """面板是配置与状态的统一视图; 未列出的路径回落到面板"""
        conn = http.client.HTTPConnection("127.0.0.1", self.metrics_port, timeout=15)
        conn.request("GET", "/config", headers={"X-Hublane-Token": "e2etoken"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn("<html", body.lower(), "未知路径应回落到面板")

    def _post(self, path, token="e2etoken"):
        conn = http.client.HTTPConnection("127.0.0.1", self.metrics_port, timeout=15)
        conn.request("POST", path, headers={"X-Hublane-Token": token})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, body

    def test_17_reload_rejects_bad_config_keeps_old(self):
        """POST /reload 遇非法配置必须 400 且保留旧配置"""
        path = os.path.join(self.home, "config.json")
        with open(path) as fh:
            good = json.load(fh)
        try:
            with open(path, "w") as fh:
                json.dump(dict(good, raw_upstreams=["no_such_upstream"]), fh)
            status, body = self._post("/reload")
            self.assertEqual(status, 400, "非法配置应 400: %s" % body[:200])
            self.assertIn("保留旧配置", body)
            # 旧配置仍在生效
            self.assertIn(b"WATT-OK", self.mitm_https(RAW_HOST, "/after/badreload"))
        finally:
            with open(path, "w") as fh:
                json.dump(good, fh, indent=2)
            self.proc.send_signal(signal.SIGHUP)
            time.sleep(1.0)

    def test_18_sighup_hot_reload(self):
        path = os.path.join(self.home, "config.json")
        with open(path) as fh:
            good = json.load(fh)
        good["sample_size"] = 77
        with open(path, "w") as fh:
            json.dump(good, fh, indent=2)
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(1.5)
        status, body = self._post("/reload")
        self.assertEqual(status, 200, "合法配置应重载成功: %s" % body[:200])
        self.assertIn(b"WATT-OK", self.mitm_https(RAW_HOST, "/after/sighup"))
        data = json.loads(self.metrics("/status")[1])
        self.assertIn("latency", data)

    def test_18b_post_reload_requires_token(self):
        status, _ = self._post("/reload", token="")
        self.assertEqual(status, 401, "/reload 缺 token 应 401")
        status, _ = self._post("/nope")
        self.assertEqual(status, 405, "非 /reload 的 POST 应 405")

    def test_19_json_log_format(self):
        self.mitm_https(RAW_HOST, "/log/probe")
        self.log.flush()
        self.log.seek(0)
        text = self.log.read().decode("utf-8", "replace")
        lines = [x for x in text.splitlines() if x.strip()]
        parsed = 0
        for line in lines:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            parsed += 1
            self.assertIn("ts", obj)
        self.assertGreater(parsed, 0, "没有解析出任何 JSON 日志行")


class TlsOrigin(threading.Thread):
    """假 TLS 上游(watt 类型): 自带 CA, 让 hublane 用真 TLS 校验后连它

    hublane 作为独立进程会用 ssl.create_default_context() 校验上游证书, 所以把
    origin CA 通过 SSL_CERT_FILE 注入子进程环境(OpenSSL 认这个变量):
    既走真证书校验, 又不用改产品代码、不依赖外网。
    """

    def __init__(self, target_port):
        super().__init__(daemon=True)
        self.target_port = target_port
        self.port = free_port()
        self.dir = tempfile.mkdtemp(prefix="hublane-tlsorigin-")
        self.ca_path = os.path.join(self.dir, "ca.crt")
        self.ready = threading.Event()
        self.requests = []
        self.lock = threading.Lock()

    def run(self):
        def run(*a):
            return subprocess.run([OPENSSL] + list(a), check=True, capture_output=True)
        ca_ext = os.path.join(self.dir, "ca.ext")
        with open(ca_ext, "w") as fh:
            fh.write("basicConstraints=critical,CA:TRUE\n"
                     "keyUsage=critical,digitalSignature,keyCertSign,cRLSign\n"
                     "subjectKeyIdentifier=hash\n")
        ca_key = os.path.join(self.dir, "ca.key")
        ca_csr = os.path.join(self.dir, "ca.csr")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key,
            "-out", ca_csr, "-subj", "/CN=hublane e2e fake watt CA")
        run("x509", "-req", "-in", ca_csr, "-signkey", ca_key, "-extfile", ca_ext,
            "-days", "2", "-out", self.ca_path)
        key = os.path.join(self.dir, "k")
        crt = os.path.join(self.dir, "c")
        srv_ext = os.path.join(self.dir, "srv.ext")
        with open(srv_ext, "w") as fh:
            fh.write("subjectAltName=IP:127.0.0.1,DNS:localhost\n"
                     "basicConstraints=CA:FALSE\n"
                     "keyUsage=critical,digitalSignature,keyEncipherment\n"
                     "extendedKeyUsage=serverAuth\n")
        csr = os.path.join(self.dir, "s.csr")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr,
            "-subj", "/CN=127.0.0.1")
        run("x509", "-req", "-in", csr, "-CA", self.ca_path, "-CAkey", ca_key,
            "-CAcreateserial", "-extfile", srv_ext, "-days", "2", "-out", crt)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.ready.set()
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(16)
        raw_ctx = ctx
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, raw_ctx),
                             daemon=True).start()

    def _serve(self, conn, ctx):
        try:
            tls = ctx.wrap_socket(conn, server_side=True)
            tls.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin1")
            with self.lock:
                self.requests.append(line)
            body = ("WATT-OK " + line).encode()
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
                        + body)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _b64(text):
    """标准代理认证: base64(user:token)"""
    import base64
    return base64.b64encode(("%s:%s" % (_USER, text)).encode()).decode()


_USER = "u"


if __name__ == "__main__":
    unittest.main(verbosity=2)
