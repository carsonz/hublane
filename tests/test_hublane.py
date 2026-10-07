"""hublane 单元测试 (P2)

运行:  python -m unittest discover -s tests -v
  或:  python -m pytest tests -q
"""
import base64
import io
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hublane as H  # noqa: E402


class TestMirrorURL(unittest.TestCase):
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_ghproxy_passthrough(self):
        p = "/NousResearch/hermes-agent/main/scripts/install.sh"
        self.assertEqual(
            H.mirror_url("ghproxy", p),
            "https://ghproxy.net/https://raw.githubusercontent.com" + p)

    def test_jsdelivr_strips_refs_heads(self):
        self.assertEqual(
            H.mirror_url("jsdelivr", "/anomalyco/opencode/refs/heads/dev/install"),
            "https://cdn.jsdelivr.net/gh/anomalyco/opencode@dev/install")

    def test_jsdelivr_plain_branch(self):
        self.assertEqual(
            H.mirror_url("jsdelivr_fastly", "/o/r/main/a/b.sh"),
            "https://fastly.jsdelivr.net/gh/o/r@main/a/b.sh")

    def test_jsdelivr_too_short(self):
        self.assertIsNone(H.mirror_url("jsdelivr", "/o/r/main"))

    def test_custom_mirror_template(self):
        H.CONFIG["custom_mirrors"] = {"mine": "https://x.example/{path}"}
        self.assertEqual(H.mirror_url("mine", "/a/b"), "https://x.example//a/b")

    def test_unknown_mirror_returns_none(self):
        self.assertIsNone(H.mirror_url("不存在的镜像", "/a/b"))


class TestMirrorLibrary(unittest.TestCase):
    """镜像库: 所有内置镜像都能生成合法 URL, 且都是已知上游(能通过配置校验)"""

    PATH = "/owner/repo/main/install.sh"

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_every_builtin_mirror_builds_url(self):
        self.assertGreaterEqual(len(H.MIRROR_PREFIX), 10)
        for name in H.MIRROR_PREFIX:
            url = H.mirror_url(name, self.PATH)
            self.assertTrue(url, name)
            self.assertTrue(url.startswith("https://"), name)
            self.assertTrue(url.endswith(self.PATH), name)

    def test_prefix_mirrors_keep_raw_host(self):
        for name in ("ghproxy_com", "ghproxy", "ghfast", "ghp_ci", "gitdl"):
            self.assertIn("raw.githubusercontent.com", H.mirror_url(name, self.PATH))

    def test_swap_mirrors_replace_domain(self):
        # 域名替换式: raw.gitmirror.com / raw.kkgithub.com 直接接路径
        self.assertEqual(H.mirror_url("gitmirror", self.PATH),
                         "https://raw.gitmirror.com" + self.PATH)
        self.assertEqual(H.mirror_url("kkgithub", self.PATH),
                         "https://raw.kkgithub.com" + self.PATH)

    def test_all_builtin_mirrors_pass_validation(self):
        known = H._known_upstreams()
        for name in list(H.MIRROR_PREFIX) + list(H.JSDELIVR_HOSTS) + list(H.SITE_MIRRORS):
            self.assertIn(name, known)
        H.CONFIG["raw_upstreams"] = list(H.MIRROR_PREFIX) + list(H.JSDELIVR_HOSTS)
        self.assertEqual(H.validate_config(), [])


class TestSiteMirrors(unittest.TestCase):
    """非 GitHub 站点的镜像库(路径与官方一致, 故可整站换源)"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_every_site_mirror_builds_url(self):
        self.assertGreaterEqual(len(H.SITE_MIRRORS), 14)
        for name in H.SITE_MIRRORS:
            url = H.mirror_url(name, "/some/path?x=1")
            self.assertTrue(url and url.startswith("https://"), name)
            self.assertIn("/some/path?x=1", url, name)

    def test_path_compatible_pairs(self):
        cases = [
            ("hf_mirror", "/api/models/bert-base-uncased",
             "https://hf-mirror.com/api/models/bert-base-uncased"),
            ("pypi_tuna", "/simple/requests/",
             "https://pypi.tuna.tsinghua.edu.cn/simple/requests/"),
            ("npmmirror", "/react/latest",
             "https://registry.npmmirror.com/react/latest"),
            ("goproxy_cn", "/github.com/gin-gonic/gin/@v/list",
             "https://goproxy.cn/github.com/gin-gonic/gin/@v/list"),
            ("conda_tuna", "/pkgs/main/linux-64/repodata.json",
             "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/linux-64/repodata.json"),
            ("conda_cloud_tuna", "/conda-forge/linux-64/repodata.json",
             "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge"
             "/linux-64/repodata.json"),
            ("gstatic_cn", "/s/roboto/v30/x.woff2",
             "https://fonts.gstatic.cn/s/roboto/v30/x.woff2"),
        ]
        for name, path, expect in cases:
            self.assertEqual(H.mirror_url(name, path), expect, name)

    def test_prefix_strip(self):
        # nodejs.org/dist/... -> registry.npmmirror.com/-/binary/node/...
        self.assertEqual(
            H.mirror_url("nodejs_npmmirror", "/dist/v20.11.0/SHASUMS256.txt"),
            "https://registry.npmmirror.com/-/binary/node/v20.11.0/SHASUMS256.txt")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(H.mirror_url("not_a_mirror", "/x"))

    def test_no_name_collision_with_raw_mirrors(self):
        """同名会改变语义(raw 镜像会重写成 /gh/... 路径), 必须互斥"""
        overlap = set(H.SITE_MIRRORS) & (set(H.MIRROR_PREFIX) | set(H.JSDELIVR_HOSTS))
        self.assertEqual(overlap, set())

    def test_builtin_per_host_chains_cover_mirrored_sites(self):
        for host, names in H.BUILTIN_PER_HOST_UPSTREAMS.items():
            self.assertTrue(names, host)
            for name in names:
                self.assertIn(name, H._known_upstreams())


class TestBuiltinPerHost(unittest.TestCase):
    """内置按域名链: 只在域名已被接管时生效, 且用户配置优先"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def _enable(self, *groups):
        H.CONFIG["extra_host_groups_enabled"] = list(groups)

    def test_not_applied_when_group_disabled(self):
        self.assertFalse(H.should_intercept("huggingface.co"))
        self.assertNotIn("huggingface.co", H.per_host_config())

    def test_applied_when_group_enabled(self):
        self._enable("ai_models", "python")
        self.assertTrue(H.should_intercept("huggingface.co"))
        self.assertEqual(H.per_host_chain("huggingface.co"), ["hf_mirror", "direct"])
        self.assertEqual(H.per_host_chain("pypi.org"),
                         ["pypi_tuna", "pypi_aliyun", "direct"])

    def test_user_config_wins(self):
        self._enable("python")
        H.CONFIG["per_host_upstreams"] = {"pypi.org": ["direct"]}
        self.assertEqual(H.per_host_chain("pypi.org"), ["direct"])

    def test_user_wildcard_wins(self):
        self._enable("python")
        H.CONFIG["per_host_upstreams"] = {"*.org": ["direct"]}
        self.assertEqual(H.per_host_chain("pypi.org"), ["direct"])

    def test_prefix_mirror_used_for_huggingface(self):
        self._enable("ai_models")
        chain = H.build_chain("huggingface.co", "/api/models/x")
        self.assertEqual(chain[0], "hf_mirror")


class TestHostGroups(unittest.TestCase):
    """境外站点分组: 按需整组启用, 未启用的分组不接管"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_default_only_fonts_cdn(self):
        self.assertEqual(H.CONFIG["extra_host_groups_enabled"], ["fonts_cdn"])
        self.assertIn("fonts.gstatic.com", H.group_hosts())
        self.assertIn("fonts.gstatic.com", H.extra_hosts())
        self.assertTrue(H.should_intercept("fonts.gstatic.com"))

    def test_disabled_group_not_managed(self):
        self.assertNotIn("ghcr.io", H.extra_hosts())
        self.assertFalse(H.should_intercept("ghcr.io"))

    def test_enabling_group_takes_effect(self):
        H.CONFIG["extra_host_groups_enabled"] = ["container"]
        self.assertTrue(H.should_intercept("registry-1.docker.io"))
        self.assertIn("ghcr.io", H.managed_hosts())
        # 分组主机走 extra_upstreams
        self.assertEqual(H.build_chain("ghcr.io", "/"),
                         H.order_upstreams("ghcr.io", H.CONFIG["extra_upstreams"]))

    def test_unknown_group_rejected(self):
        H.CONFIG["extra_host_groups_enabled"] = ["fonts_cdn", "不存在的分组"]
        errs = H.validate_config()
        self.assertTrue(any("未知分组" in e for e in errs), errs)

    def test_malformed_groups_rejected(self):
        H.CONFIG["extra_host_groups"] = ["not a dict"]
        self.assertTrue(any("extra_host_groups 必须是字典" in e
                            for e in H.validate_config()))


class TestDohEndpoints(unittest.TestCase):
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_all_endpoints_have_host_placeholder(self):
        eps = H.doh_endpoints()
        self.assertEqual(len(eps), len(H.CONFIG["doh_endpoints"]))
        for ep in eps:
            self.assertIn("{host}", ep["url"])
            self.assertTrue(ep["enabled"])
            self.assertTrue(ep["name"])

    def test_mainland_endpoints_come_first(self):
        names = [ep["name"] for ep in H.doh_endpoints()]
        self.assertEqual(names[0], "dns.alidns.com")
        self.assertLess(names.index("doh.pub"), names.index("dns.google"))
        self.assertLess(names.index("doh.360.cn"), names.index("dns.google"))

    def test_endpoint_object_can_be_disabled(self):
        H.CONFIG["doh_endpoints"] = [
            {"url": "https://a.example/dns-query?name={host}&type={type}",
             "name": "a", "enabled": False},
            "https://b.example/dns-query?name={host}&type={type}",
        ]
        eps = H.doh_endpoints()
        self.assertFalse(eps[0]["enabled"])
        self.assertTrue(eps[1]["enabled"])


class TestLatency(unittest.TestCase):
    """2.1 延迟直方图与分位数"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._latency_lock:
            H._latency.clear()

    def test_bucket_index(self):
        self.assertEqual(H._bucket_index(0.05), 0)
        self.assertEqual(H._bucket_index(0.1), 0)
        self.assertEqual(H._bucket_index(0.3), 2)
        self.assertEqual(H._bucket_index(999), len(H.LATENCY_BUCKETS))

    def test_percentile_empty(self):
        self.assertIsNone(H.percentile([0] * 9, 0.5))

    def test_percentile_returns_bucket_upper_bound(self):
        # 9 次 0.05s + 1 次 9s: P50/P90 仍在 0.1s 桶, P95 落到 10s 桶上界
        for _ in range(9):
            H.latency_add("h", "up", 0.05)
        H.latency_add("h", "up", 9)
        with H._latency_lock:
            buckets = H._latency["h|up"]
        self.assertEqual(H.percentile(buckets, 0.5), 0.1)
        self.assertEqual(H.percentile(buckets, 0.9), 0.1)
        self.assertEqual(H.percentile(buckets, 0.95), 10.0)

    def test_stats_split_global_and_upstream(self):
        H.latency_add("a.com", "watt", 0.2)
        H.latency_add("b.com", "ghfast", 0.4)
        stats = H.latency_stats()
        self.assertEqual(stats["global"]["count"], 2)
        self.assertEqual(len(stats["by_upstream"]), 2)
        self.assertEqual({r["host"] for r in stats["by_upstream"]},
                         {"a.com", "b.com"})


class TestSamples(unittest.TestCase):
    """2.1 最近请求样本(环形缓冲)"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        H._samples.clear()

    def tearDown(self):
        H._samples.clear()

    def test_keeps_newest_first_and_respects_size(self):
        H.CONFIG["sample_size"] = 3
        for i in range(10):
            H.sample_add(host="h", upstream="up", path="/%d" % i)
        got = H.samples()
        self.assertEqual([s["path"] for s in got], ["/9", "/8", "/7"])

    def test_disabled_by_zero(self):
        H.CONFIG["sample_size"] = 0
        H.sample_add(host="h", upstream="up")
        self.assertEqual(len(H._samples), 0)

    def test_ts_added(self):
        H.sample_add(host="h", upstream="up")
        self.assertIn("ts", H.samples()[0])


class TestJsonLog(unittest.TestCase):
    """2.2 结构化日志"""

    def test_json_output(self):
        record = logging.LogRecord("hublane", logging.INFO, "f", 1,
                                   "ok %s <- %s", ("a.com", "watt"), None)
        out = H.JsonFormatter().format(record)
        payload = json.loads(out)
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["msg"], "ok a.com <- watt")
        self.assertIn("ts", payload)

    def test_json_includes_exception(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = logging.LogRecord("hublane", logging.ERROR, "f", 1,
                                       "failed", None, sys.exc_info())
        payload = json.loads(H.JsonFormatter().format(record))
        self.assertIn("RuntimeError: boom", payload["exc"])


class TestCertSan(unittest.TestCase):
    """叶证书 SAN 必须覆盖库里所有会被接管的域名"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def _library_hosts(self):
        hosts = set(H.GH_HOSTS) | set(H.RAW_HOSTS)
        hosts |= {str(h).lower() for h in H.DEFAULTS.get("extra_hosts", [])}
        for names in H.DEFAULTS.get("extra_host_groups", {}).values():
            hosts |= {str(h).lower() for h in names or []}
        hosts |= set(H.BUILTIN_PER_HOST_UPSTREAMS)
        return {h for h in hosts if h}

    def test_san_covers_library(self):
        san = H.leaf_san()
        missing = [h for h in self._library_hosts() if "DNS:" + h not in san]
        self.assertEqual(missing, [], "证书 SAN 缺少: %s" % missing)

    def test_installers_use_the_same_san(self):
        """install.sh / install-windows.bat 里的 SAN 不能落后于 hublane.py"""
        want = set(H.leaf_san().split(","))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("install.sh", "install-windows.bat"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                found = set(re.findall(r"DNS:[A-Za-z0-9.*\-]+", fh.read()))
            missing = sorted(want - found)
            self.assertEqual(missing, [], "%s 缺少 SAN: %s" % (name, missing[:5]))


class TestDiag(unittest.TestCase):
    """2.4 诊断包"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_secrets_masked(self):
        H.CONFIG["metrics_token"] = "s3cr3t"
        H.CONFIG["proxy_token"] = "p4ss"
        rep = H.config_report()
        self.assertEqual(rep["metrics_token"], "***")
        self.assertEqual(rep["proxy_token"], "***")
        self.assertNotIn("s3cr3t", json.dumps(rep, ensure_ascii=False))

    def test_diag_contains_sections(self):
        text = H.diag_text()
        for part in ("hublane 诊断包", "版本:", "---- 配置", "---- 状态",
                     "---- 日志尾部"):
            self.assertIn(part, text)


class TestReloadConfig(unittest.TestCase):
    """2.3 配置热重载"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        self.tmp = tempfile.mkdtemp(prefix="hublane-reload-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old_conf = H.CONF
        self.path = os.path.join(self.tmp, "config.json")
        H.CONF = self.path
        self.addCleanup(setattr, H, "CONF", self.old_conf)

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_valid_reload_applies(self):
        self._write({"max_conns": 42})
        ok, msg, restart = H.reload_config()
        self.assertTrue(ok, msg)
        self.assertFalse(restart)
        self.assertEqual(H.CONFIG["max_conns"], 42)

    def test_invalid_reload_keeps_old(self):
        self._write({"raw_upstreams": ["no_such_upstream"]})
        before = H.CONFIG["max_conns"]
        ok, msg, _restart = H.reload_config()
        self.assertFalse(ok)
        self.assertIn("未知上游", msg)
        self.assertEqual(H.CONFIG["max_conns"], before)

    def test_structural_change_needs_restart(self):
        self._write({"listen_port": 9999})
        ok, msg, restart = H.reload_config()
        self.assertTrue(ok)
        self.assertTrue(restart)
        self.assertIn("需重启", msg)

    def test_missing_file_fails(self):
        H.CONF = os.path.join(self.tmp, "nope.json")
        ok, msg, _r = H.reload_config()
        self.assertFalse(ok)
        self.assertIn("读取配置失败", msg)

    @unittest.skipIf(H.IS_WIN, "SIGHUP 仅 POSIX")
    def test_sighup_handler_registered(self):
        previous = signal.getsignal(signal.SIGHUP)
        try:
            self.assertTrue(H._install_reload_handler())
            self.assertTrue(callable(signal.getsignal(signal.SIGHUP)))
        finally:
            signal.signal(signal.SIGHUP, previous)


class TestAccessControl(unittest.TestCase):
    """1.2 本地访问控制: proxy_token(HTTP/SOCKS5) + uid 白名单"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_no_token_means_open(self):
        self.assertTrue(H.proxy_token_ok({}))
        self.assertTrue(H.proxy_token_ok({"proxy-authorization": ["Basic xyz"]}))

    def test_basic_credentials(self):
        H.CONFIG["proxy_token"] = "t0k3n"
        good = base64.b64encode(b"hublane:t0k3n").decode()
        bad = base64.b64encode(b"hublane:nope").decode()
        self.assertTrue(H.proxy_token_ok(
            {"proxy-authorization": ["Basic " + good]}))
        self.assertFalse(H.proxy_token_ok(
            {"proxy-authorization": ["Basic " + bad]}))
        self.assertFalse(H.proxy_token_ok({}))

    def test_raw_and_bearer_forms(self):
        H.CONFIG["proxy_token"] = "t0k3n"
        self.assertTrue(H.proxy_token_ok(
            {"Proxy-Authorization": ["Bearer t0k3n"]}))
        self.assertTrue(H.proxy_token_ok({"proxy-authorization": ["t0k3n"]}))
        self.assertFalse(H.proxy_token_ok({"proxy-authorization": [""]}))

    def test_broken_base64_rejected(self):
        H.CONFIG["proxy_token"] = "t0k3n"
        self.assertFalse(H.proxy_token_ok(
            {"proxy-authorization": ["Basic !!!not-base64!!!"]}))

    def test_uid_whitelist_empty_means_open(self):
        self.assertTrue(H.peer_allowed(socket.socket()))

    @unittest.skipIf(H.IS_WIN, "SO_PEERCRED 仅 Linux/WSL")
    def test_uid_whitelist(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        uid = H.peer_uid(left)
        self.assertIsInstance(uid, int)
        H.CONFIG["proxy_uid_whitelist"] = [uid]
        self.assertTrue(H.peer_allowed(left))
        H.CONFIG["proxy_uid_whitelist"] = [uid + 1000]
        self.assertFalse(H.peer_allowed(left))


class TestProbeSpecs(unittest.TestCase):
    """1.5 全链路主动探测: 探测目标覆盖 raw 镜像 / github 链 / extra 站点"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_raw_mirrors_are_probed(self):
        specs = H.probe_specs()
        raw = [s for s in specs if s[0] == H.RAW_HOST]
        self.assertEqual(len(raw), len(H.CONFIG["raw_upstreams"]))
        self.assertTrue(all(s[1] == H.PROBE_RAW_PATH for s in raw))

    def test_raw_probe_scope_matches_real_traffic(self):
        """探测与真实流量必须写在同一 scope, 否则排序拿不到探测数据"""
        for host, _path, _names in H.probe_specs():
            if host != H.RAW_HOST:
                continue
            self.assertEqual(host, "raw.githubusercontent.com")
            self.assertIn(host, H.RAW_HOSTS)

    def test_github_chain_is_probed(self):
        specs = {s[0]: s[2] for s in H.probe_specs()}
        self.assertIn("github.com", specs)
        self.assertEqual(specs["github.com"], H.CONFIG["github_upstreams"])

    def test_extra_hosts_limited(self):
        H.CONFIG["probe_extra_max"] = 2
        extra = [s for s in H.probe_specs()
                 if s[0] not in (H.RAW_HOST, "github.com")]
        self.assertEqual(len(extra), 2)
        self.assertEqual([s[0] for s in extra],
                         list(H.CONFIG["extra_hosts"])[:2])
        self.assertTrue(all(s[2] == H.CONFIG["extra_upstreams"] for s in extra))

    def test_disabled_probe_yields_nothing(self):
        H.CONFIG["probe_enabled"] = False
        self.assertEqual(H.probe_specs(), [])


class TestConnLimits(unittest.TestCase):
    """1.1 客户端连接治理: 并发上限(每个监听实例一个限流器)"""

    def test_admit_up_to_limit(self):
        limiter = H.ConnLimiter(2)
        self.assertTrue(limiter.acquire())
        self.assertTrue(limiter.acquire())
        self.assertFalse(limiter.acquire())
        self.assertEqual(limiter.count(), 2)

    def test_release_frees_slot(self):
        limiter = H.ConnLimiter(1)
        self.assertTrue(limiter.acquire())
        self.assertFalse(limiter.acquire())
        limiter.release()
        self.assertTrue(limiter.acquire())

    def test_zero_limit_means_unlimited(self):
        limiter = H.ConnLimiter(0)
        for _ in range(50):
            self.assertTrue(limiter.acquire())
        self.assertEqual(limiter.count(), 50)

    def test_instances_are_isolated(self):
        """旧实例延迟释放不会把新实例的计数带歪(重启场景)"""
        old, new = H.ConnLimiter(1), H.ConnLimiter(1)
        self.assertTrue(old.acquire())
        self.assertTrue(new.acquire())
        old.release()                   # 旧实例释放不应影响新实例
        self.assertEqual(new.count(), 1)
        self.assertFalse(new.acquire())

    def test_bad_limit_treated_as_unlimited(self):
        self.assertEqual(H.ConnLimiter(None).limit, 0)
        self.assertEqual(H.ConnLimiter("x").limit, 0)
        self.assertTrue(H.ConnLimiter(None).acquire())


class TestHeaders(unittest.TestCase):
    def test_hop_by_hop_stripped(self):
        raw = {"Host": ["github.com"], "Accept-Encoding": ["gzip"],
               "Proxy-Connection": ["keep-alive"], "User-Agent": ["curl"]}
        out = H.flat_headers(raw)
        self.assertNotIn("Host", out)
        self.assertNotIn("Accept-Encoding", out)
        self.assertNotIn("Proxy-Connection", out)
        self.assertEqual(out["User-Agent"], "curl")


class TestBodyParsing(unittest.TestCase):
    def test_content_length_small(self):
        raw = b"hello world"
        f = io.BytesIO(raw)
        self.assertEqual(H.build_body(f, {"Content-Length": ["11"]}), raw)

    def test_chunked(self):
        # 第一块 7 字节 "hello w", 第二块 5 字节 "orld!"
        payload = b"7\r\nhello w\r\n5\r\norld!\r\n0\r\n\r\n"
        f = io.BytesIO(payload)
        self.assertEqual(H.build_body(f, {"Transfer-Encoding": ["chunked"]}),
                         b"hello world!")

    def test_no_body(self):
        self.assertEqual(H.build_body(io.BytesIO(b""), {}), b"")


class TestUpstreamRanking(unittest.TestCase):
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._ustat_lock:
            H._ustat.clear()

    def _learn(self, scope, name, ewma):
        with H._ustat_lock:
            H._ustat[H._u_key(scope, name)] = {
                "ewma": ewma, "fails": 0, "cool_until": 0.0}

    def test_faster_first(self):
        self._learn("raw", "slow", 2.0)
        self._learn("raw", "fast", 0.5)
        self.assertEqual(H.order_upstreams("raw", ["slow", "fast"]),
                         ["fast", "slow"])

    def test_unproven_after_known(self):
        self._learn("raw", "known", 1.0)
        order = H.order_upstreams("raw", ["unknown", "known"])
        self.assertEqual(order[0], "known")

    def test_failure_demotes(self):
        self._learn("raw", "bad", 0.1)
        for _ in range(2):
            H._u_fail("raw", "bad")
        self._learn("raw", "good", 5.0)
        self.assertEqual(H.order_upstreams("raw", ["bad", "good"])[0], "good")

    def test_scope_isolated(self):
        self._learn("raw", "x", 0.1)
        order = H.order_upstreams("github.com", ["x", "y"])
        self.assertTrue(set(order) == {"x", "y"})


class TestSuccessRate(unittest.TestCase):
    """P1: 成功率维度(滑动窗口)"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._ustat_lock:
            H._ustat.clear()

    def _seed(self, scope, name, ewma, outcomes):
        hist = [1 if o else 0 for o in outcomes]
        with H._ustat_lock:
            state = H._u_entry()
            state["ewma"] = ewma
            state["hist"] = hist
            state["ok"] = hist.count(1)
            state["fail"] = hist.count(0)
            H._ustat[H._u_key(scope, name)] = state

    def test_rate_and_samples(self):
        self._seed("raw", "a", 1.0, [1, 1, 1, 0])
        rate, samples = H.success_rate(H._ustat[H._u_key("raw", "a")])
        self.assertAlmostEqual(rate, 0.75)
        self.assertEqual(samples, 4)

    def test_hist_trimmed_to_twice_window(self):
        H.CONFIG["success_window"] = 4
        for _ in range(20):
            H._u_ok("raw", "a", 1.0)
        hist = H._ustat[H._u_key("raw", "a")]["hist"]
        self.assertEqual(len(hist), 8)
        self.assertEqual(H.success_rate(H._ustat[H._u_key("raw", "a")])[1], 4)

    def test_flaky_loses_to_slow_but_stable(self):
        # 快但一半失败 vs 慢但从不失败 -> 慢的应该优先(失败要多付一次回落代价)
        self._seed("raw", "flaky", 0.4, [1, 0] * 3)
        self._seed("raw", "stable", 1.0, [1] * 6)
        self.assertEqual(H.order_upstreams("raw", ["flaky", "stable"]),
                         ["stable", "flaky"])

    def test_unproven_sample_not_penalised(self):
        self._seed("raw", "flaky", 0.4, [0])
        self._seed("raw", "known", 2.0, [1] * 5)
        # 样本不足 -> 归入"未证明"档, 排在已证明可用之后
        self.assertEqual(H.order_upstreams("raw", ["flaky", "known"]),
                         ["known", "flaky"])

    def test_legacy_state_without_hist_still_ranked(self):
        with H._ustat_lock:
            H._ustat[H._u_key("raw", "old")] = {
                "ewma": 0.5, "fails": 0, "cool_until": 0.0}
        self.assertEqual(H.success_rate(H._ustat[H._u_key("raw", "old")])[0], 1.0)
        self.assertEqual(H.order_upstreams("raw", ["old", "unknown"])[0], "old")

    def test_direct_cooldown_config_applied_after_threshold(self):
        for _ in range(H.CONFIG["direct_fail_max"]):
            H._u_fail("github.com", "direct", cooldown=600)
        state = H._ustat[H._u_key("github.com", "direct")]
        self.assertGreater(state["cool_until"] - time.time(), 500)


class TestPerHostUpstreams(unittest.TestCase):
    """P2: 按域名的上游链"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        H.CONFIG["per_host_upstreams"] = {
            "github.com": ["watt", "direct"],
            "*.example.com": ["chain"],
        }
        with H._ustat_lock:
            H._ustat.clear()

    def test_exact_host(self):
        self.assertEqual(H.per_host_chain("github.com"), ["watt", "direct"])

    def test_wildcard_suffix(self):
        self.assertEqual(H.per_host_chain("a.b.example.com"), ["chain"])
        self.assertIsNone(H.per_host_chain("example.com.evil.net"))

    def test_longest_match_wins(self):
        H.CONFIG["per_host_upstreams"]["sub.example.com"] = ["direct"]
        self.assertEqual(H.per_host_chain("sub.example.com"), ["direct"])

    def test_per_host_implies_managed(self):
        self.assertTrue(H.should_intercept("a.example.com"))
        self.assertFalse(H.should_intercept("not-configured.com"))

    def test_build_chain_prefers_per_host(self):
        self.assertEqual(H.build_chain("github.com", "/x"), ["watt", "direct"])

    def test_pac_lists_wildcard(self):
        pac = H.pac_content()
        self.assertIn("dnsDomainIs", pac)
        self.assertIn("example.com", pac)

    def test_validate_rejects_bad_entry(self):
        H.CONFIG["per_host_upstreams"] = {"a.com": ["nope"]}
        self.assertTrue(any("未知上游" in e for e in H.validate_config()))
        H.CONFIG["per_host_upstreams"] = {"a.com": []}
        self.assertTrue(any("非空列表" in e for e in H.validate_config()))


class TestDoHEndpoints(unittest.TestCase):
    """P3: DoH 端点可插拔 + 健康度"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._doh_lock:
            H._doh.clear()

    def test_normalises_str_and_dict(self):
        H.CONFIG["doh_endpoints"] = [
            "https://dns.alidns.com/resolve?name={host}&type={type}",
            {"url": "https://doh.pub/dns-query?name={host}&type={type}",
             "name": "pub", "enabled": False},
        ]
        eps = H.doh_endpoints()
        self.assertEqual([e["name"] for e in eps], ["dns.alidns.com", "pub"])
        self.assertFalse(eps[1]["enabled"])

    def test_config_order_kept_and_failures_moved_back(self):
        H.CONFIG["doh_endpoints"] = [
            "https://a.example/resolve?name={host}&type={type}",
            "https://b.example/resolve?name={host}&type={type}",
        ]
        self.assertEqual([e["name"] for e in H.doh_ordered()],
                         ["a.example", "b.example"])
        for _ in range(2):
            H._doh_fail("a.example")
        self.assertEqual([e["name"] for e in H.doh_ordered()],
                         ["b.example", "a.example"])

    def test_disabled_endpoint_excluded(self):
        H.CONFIG["doh_endpoints"] = [
            {"url": "https://a.example/x?name={host}&type={type}", "enabled": False}]
        self.assertEqual(H.doh_ordered(), [])

    def test_probe_records_health(self):
        calls = []
        H.CONFIG["doh_endpoints"] = [
            "https://a.example/resolve?name={host}&type={type}"]
        original = H._doh_once
        H._doh_once = lambda url: calls.append(url) or [("1.2.3.4", socket.AF_INET)]
        try:
            H.doh_probe()
        finally:
            H._doh_once = original
        with H._doh_lock:
            state = H._doh["a.example"]
        self.assertEqual(state["ok"], 1)
        self.assertEqual(calls, ["https://a.example/resolve?name=github.com&type=A"])

    def test_validate_requires_placeholders(self):
        H.CONFIG["doh_endpoints"] = ["https://a.example/resolve?name=x"]
        self.assertTrue(any("{host}" in e for e in H.validate_config()))


class TestIntegrity(unittest.TestCase):
    """P2: 响应完整性校验"""

    class Resp:
        def __init__(self, body, declared=None, chunked=False, status=200):
            self.status = status
            self._body = body
            self._pos = 0
            self._chunked = chunked
            self._declared = len(body) if declared is None else declared

        def getheaders(self):
            if self._chunked:
                return [("Transfer-Encoding", "chunked")]
            return [("Content-Length", str(self._declared))]

        def read(self, n=-1):
            if n is None or n < 0:
                n = len(self._body) - self._pos
            chunk = self._body[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_small_response_buffered_and_forwarded(self):
        out = io.BytesIO()
        sent, size = H.relay_response(out, self.Resp(b"hello world"), "GET")
        self.assertTrue(sent)
        self.assertEqual(size, 11)
        self.assertTrue(out.getvalue().endswith(b"hello world"))
        self.assertIn(b"Content-Length: 11", out.getvalue())

    def test_small_truncated_raises_before_any_head(self):
        out = io.BytesIO()
        with self.assertRaises(H.TruncatedError):
            H.relay_response(out, self.Resp(b"x" * 10, declared=4096), "GET")
        self.assertNotIn(b"HTTP/1.1", out.getvalue())   # 头未发出 -> 可回落

    def test_large_truncated_raises_after_head(self):
        H.CONFIG["integrity_buffer_max"] = 4
        out = io.BytesIO()
        with self.assertRaises(H.TruncatedError):
            H.relay_response(out, self.Resp(b"x" * 10, declared=4096), "GET")
        self.assertIn(b"HTTP/1.1 200 OK", out.getvalue())
        self.assertNotIn(b"0\r\n\r\n", out.getvalue())

    def test_large_complete_streams(self):
        H.CONFIG["integrity_buffer_max"] = 4
        out = io.BytesIO()
        sent, size = H.relay_response(out, self.Resp(b"y" * 10), "GET")
        self.assertEqual(size, 10)
        self.assertIn(b"Content-Length: 10", out.getvalue())

    def test_stream_abort_omits_chunked_terminator(self):
        class Boom(self.Resp):
            def __init__(self):
                super().__init__(b"z" * 8, chunked=True)
                self._calls = 0

            def read(self, n=-1):
                self._calls += 1
                if self._calls > 1:
                    raise OSError("connection reset by peer")
                return super().read(n)

        out = io.BytesIO()
        with self.assertRaises(H.TruncatedError):
            H.relay_response(out, Boom(), "GET")
        self.assertNotIn(b"0\r\n\r\n", out.getvalue())   # 客户端能察觉截断
        self.assertIn(b"8\r\n", out.getvalue())

    def test_head_needs_no_body(self):
        out = io.BytesIO()
        sent, size = H.relay_response(out, self.Resp(b"", declared=1234), "HEAD")
        self.assertEqual(size, 0)
        self.assertIn(b"Content-Length: 1234", out.getvalue())

    def test_integrity_check_can_be_disabled(self):
        H.CONFIG["integrity_check"] = False
        H.CONFIG["integrity_buffer_max"] = 0
        out = io.BytesIO()
        with self.assertRaises(H.TruncatedError):
            H.relay_response(out, self.Resp(b"x" * 10, declared=4096), "GET")

    def test_expected_length_ignores_compressed(self):
        self.assertIsNone(
            H.expected_length({"Content-Length": "10", "Content-Encoding": "gzip"}))
        self.assertEqual(H.expected_length({"Content-Length": " 42 "}), 42)
        self.assertIsNone(H.expected_length({}))


class TestPoolProbe(unittest.TestCase):
    """P1: 复用连接探活"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._pool_lock:
            H._pool.clear()
        with H._ustat_lock:
            H._ustat.clear()

    class Conn:
        def __init__(self, sock):
            self.sock = sock

        def close(self):
            try:
                self.sock.close()
            except Exception:
                pass

    def test_closed_socket_detected(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        conn = self.Conn(left)
        self.assertTrue(H.sock_alive(conn))
        right.close()
        self.assertFalse(H.sock_alive(conn))

    def test_pool_drops_dead_and_idle_connections(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.close()
        with H._pool_lock:
            H._pool[("watt", "h", "")] = [(self.Conn(left), time.time())]
        self.assertIsNone(H._pool_get(("watt", "h", "")))
        left2, right2 = socket.socketpair()
        self.addCleanup(left2.close)
        self.addCleanup(right2.close)
        with H._pool_lock:
            H._pool[("watt", "h2", "")] = [(self.Conn(left2),
                                            time.time() - 10 ** 6)]
        self.assertIsNone(H._pool_get(("watt", "h2", "")))
        self.assertEqual(H._pool, {})

    def test_probe_can_be_disabled(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.close()
        H.CONFIG["pool_probe"] = False
        with H._pool_lock:
            H._pool[("watt", "h", "")] = [(self.Conn(left), time.time())]
        self.assertIsNotNone(H._pool_get(("watt", "h", "")))


class TestPoolCapacity(unittest.TestCase):
    """1.3: 每个 key 多条连接 + 全局 LRU 淘汰 + 命中统计"""

    class Conn:
        def __init__(self, sock):
            self.sock = sock

        def close(self):
            try:
                self.sock.close()
            except Exception:
                pass

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._pool_lock:
            H._pool.clear()
        self._peers = []
        self.addCleanup(self._close_peers)

    def tearDown(self):
        with H._pool_lock:
            for bucket in H._pool.values():
                for conn, _ts in bucket:
                    H._close(conn)
            H._pool.clear()

    def _conn(self):
        """一条"活着"的假连接: 对端保持打开, 探活才会认为可用"""
        left, right = socket.socketpair()
        self._peers.append(right)
        return self.Conn(left)

    def _close_peers(self):
        for peer in getattr(self, "_peers", []):
            try:
                peer.close()
            except Exception:
                pass

    def test_multiple_connections_per_key(self):
        H.CONFIG["pool_max_per_key"] = 3
        for _ in range(3):
            H._pool_put(("watt", "h", ""), self._conn())
        self.assertEqual(H.pool_size(), 3)
        got = [H._pool_get(("watt", "h", "")) for _ in range(3)]
        self.assertEqual(len([g for g in got if g is not None]), 3)
        self.assertIsNone(H._pool_get(("watt", "h", "")))

    def test_per_key_cap_enforced(self):
        H.CONFIG["pool_max_per_key"] = 2
        for _ in range(5):
            H._pool_put(("watt", "h", ""), self._conn())
        self.assertEqual(H.pool_size(), 2)

    def test_global_lru_evicts_oldest(self):
        H.CONFIG["pool_max_per_key"] = 8
        H.CONFIG["pool_max_total"] = 3
        oldest, older = self._conn(), self._conn()
        with H._pool_lock:
            H._pool["a"] = [(oldest, time.time() - 100),
                            (older, time.time() - 99)]
        H._pool_put("b", self._conn())
        H._pool_put("b", self._conn())
        self.assertEqual(H.pool_size(), 3)
        self.assertEqual(len(H._pool.get("a", [])), 1)      # 最旧的一条被淘汰
        self.assertEqual(len(H._pool.get("b", [])), 2)
        self.assertFalse(H.sock_alive(oldest))
        self.assertTrue(H.sock_alive(older))

    def test_hit_and_miss_counters(self):
        before = H.counters_snapshot()
        self.assertIsNone(H._pool_get(("watt", "none", "")))
        self.assertEqual(H.counters_snapshot()["pool_miss"] - before["pool_miss"], 1)
        H._pool_put(("watt", "h", ""), self._conn())
        self.assertIsNotNone(H._pool_get(("watt", "h", "")))
        self.assertEqual(H.counters_snapshot()["pool_hit"] - before["pool_hit"], 1)


class TestMetricsAuth(unittest.TestCase):
    """P2: 指标端点鉴权 + HTML 面板"""

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_no_token_means_open(self):
        self.assertTrue(H.metrics_authorized("", "/status"))

    def test_token_via_header_and_query(self):
        H.CONFIG["metrics_token"] = "s3cr3t"
        self.assertFalse(H.metrics_authorized("", "/status"))
        self.assertFalse(H.metrics_authorized("wrong", "/status"))
        self.assertTrue(H.metrics_authorized("s3cr3t", "/status"))
        self.assertTrue(H.metrics_authorized("", "/status?token=s3cr3t"))

    def test_metrics_host_on_lan_requires_token(self):
        H.CONFIG["metrics_host"] = "0.0.0.0"
        self.assertTrue(any("metrics_token" in e for e in H.validate_config()))
        H.CONFIG["metrics_token"] = "x"
        self.assertEqual(H.validate_config(), [])

    def test_panel_renders_health_table(self):
        H._u_ok("raw", "ghproxy_com", 0.8)
        page = H.panel_html()
        self.assertIn("<!doctype html>", page)
        self.assertIn("ghproxy_com", page)
        self.assertIn("上游健康度", page)
        with H._ustat_lock:
            H._ustat.clear()

    def test_status_json_exposes_new_fields(self):
        import json
        data = json.loads(H.status_json())
        self.assertIn("counters", data)
        self.assertIn("doh_health", data)
        self.assertIn("per_host_upstreams", data)


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_defaults_valid(self):
        self.assertEqual(H.validate_config(), [])

    def test_unknown_upstream(self):
        H.CONFIG["raw_upstreams"] = ["does_not_exist"]
        self.assertTrue(any("未知上游" in e for e in H.validate_config()))

    def test_port_conflict(self):
        H.CONFIG["metrics_port"] = H.CONFIG["listen_port"]
        self.assertTrue(any("不能相同" in e for e in H.validate_config()))


class TestMisc(unittest.TestCase):
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_pac_contains_managed_host(self):
        pac = H.pac_content()
        self.assertIn("github.com", pac)
        self.assertIn("FindProxyForURL", pac)
        self.assertIn("PROXY 127.0.0.1:", pac)

    def test_should_intercept(self):
        self.assertTrue(H.should_intercept("github.com"))
        self.assertTrue(H.should_intercept("raw.githubusercontent.com"))
        self.assertTrue(H.should_intercept("hcaptcha.com"))
        self.assertFalse(H.should_intercept("www.baidu.com"))

    def test_state_roundtrip(self):
        H._u_ok("raw", "ghproxy_com", 0.9)
        H.save_state()
        with H._ustat_lock:
            H._ustat.clear()
        H.load_state()
        self.assertIn(H._u_key("raw", "ghproxy_com"), H._ustat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
