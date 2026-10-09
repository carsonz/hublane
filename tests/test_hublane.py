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
import subprocess
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
        for name in (list(H.MIRROR_PREFIX) + list(H.GH_MIRROR_PREFIX)
                     + list(H.JSDELIVR_HOSTS) + list(H.SITE_MIRRORS)):
            self.assertIn(name, known)
        H.CONFIG["raw_upstreams"] = list(H.MIRROR_PREFIX) + list(H.JSDELIVR_HOSTS)
        self.assertEqual(H.validate_config(), [])


class TestGithubMirrorPrefix(unittest.TestCase):
    """github.com 专用镜像(给 git 的 smart-HTTP 用)

    背景: raw 镜像(MIRROR_PREFIX)全部写死指向 raw.githubusercontent.com,
    服务不了 github.com。而 git clone/fetch 打的是 github.com:443, 原先只有
    direct / watt —— direct 会被链路在正好 128 KiB 处掐断, 大仓库的 pack
    永远下不来(实测 SSLEOFError, 传输中断于 131072 字节)。所以 github 需要
    自己一套前缀式镜像。
    """

    PATH = "/deepseek-ai/deepseek-harness.git/info/refs?service=git-upload-pack"

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_mirrors_embed_github_host(self):
        """前缀里必须嵌 https://github.com, 否则镜像站不知道去哪取"""
        for name in H.GH_MIRROR_PREFIX:
            url = H.mirror_url(name, self.PATH)
            self.assertTrue(url, name)
            self.assertIn("https://github.com", url, name)
            self.assertTrue(url.endswith(self.PATH), "%s -> %s" % (name, url))

    def test_distinct_from_raw_mirrors(self):
        """同名服务的 raw 版与 github 版指向不同主机, 不得串台"""
        self.assertIn("raw.githubusercontent.com",
                      H.mirror_url("ghproxy_com", self.PATH))
        self.assertIn("github.com", H.mirror_url("ghproxy_com_gh", self.PATH))
        self.assertNotIn("raw.", H.mirror_url("ghproxy_com_gh", self.PATH))
        self.assertIn("github.com", H.mirror_url("ghfast_gh", self.PATH))

    def test_github_upstreams_default_has_mirror_ahead_of_direct(self):
        """默认链里镜像必须排在 direct 之前, 否则会先撞上 128 KiB 掐断"""
        chain = list(H.DEFAULTS["github_upstreams"])
        self.assertIn("ghproxy_com_gh", chain)
        self.assertIn("direct", chain)
        self.assertLess(chain.index("ghproxy_com_gh"), chain.index("direct"),
                        "镜像应优先于 direct: %s" % chain)
        self.assertTrue(any(n in H.GH_MIRROR_PREFIX for n in chain[:3]),
                        "前几个应是镜像: %s" % chain)

    def test_github_mirrors_pass_validation(self):
        H.CONFIG["github_upstreams"] = list(H.GH_MIRROR_PREFIX) + ["direct", "watt"]
        self.assertEqual(H.validate_config(), [])


class TestUtf8Console(unittest.TestCase):
    """非 UTF-8 控制台下不能崩

    实测事故: GitHub windows-latest runner 是英文 locale(cp1252), Python 的
    stdout 默认用 cp1252, 而本项目日志/提示全是中文 —— `python hublane.py
    --check` 与 `python tools/check_version.py` 一打印就 UnicodeEncodeError,
    五个 Windows 矩阵任务全红。
    """

    def test_ensure_utf8_console_changes_encoding(self):
        saved_out, saved_err = sys.stdout, sys.stderr
        try:
            buf_out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                       errors="strict")
            buf_err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                       errors="strict")
            sys.stdout, sys.stderr = buf_out, buf_err
            H.ensure_utf8_console()
            self.assertEqual(sys.stdout.encoding.lower().replace("-", ""), "utf8")
            self.assertEqual(sys.stderr.encoding.lower().replace("-", ""), "utf8")
        finally:
            sys.stdout, sys.stderr = saved_out, saved_err

    def test_emit_survives_narrow_console(self):
        """cp1252 下打印中文: emit 必须成功, 不能抛异常"""
        saved_out = sys.stdout
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                          errors="strict")
            H.ensure_utf8_console()
            H.emit("配置校验通过 —— 中文提示")     # 不应抛 UnicodeEncodeError
        finally:
            sys.stdout = saved_out

    def test_emit_without_reconfigure_does_not_crash_process(self):
        """兜底: 即使流没被切成 UTF-8, emit 也不能把异常抛给调用方"""
        saved_out, handlers = sys.stdout, list(H.log.handlers)
        try:
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                          errors="strict")
            H.log.handlers = []          # 去掉会写同一条流的 StreamHandler
            H.emit("中文")                # write 失败 -> 退到日志 -> 无 handler
        finally:
            sys.stdout = saved_out
            H.log.handlers = handlers

    def test_gate_script_runs_under_narrow_console(self):
        """端到端复现 CI 事故: cp1252 下跑 check_version.py 必须退出码 0

        这正是 GitHub windows-latest 五个矩阵任务失败的那一步
        (UnicodeEncodeError: 'charmap' codec can't encode ...)。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        # 别写死版本号: 发版后它会与 hublane.VERSION 不符, 这个测试就会假失败
        for args in ([], ["v" + H.VERSION]):
            proc = subprocess.run(
                [sys.executable, os.path.join(root, "tools", "check_version.py")]
                + args, env=env, capture_output=True, cwd=root)
            self.assertEqual(proc.returncode, 0,
                             "cp1252 下 args=%s 失败: %s"
                             % (args, proc.stderr.decode("utf-8", "replace")[-300:]))

    def test_hublane_check_runs_under_narrow_console(self):
        saved = H.CONFIG.get("raw_upstreams")
        try:
            H.CONFIG.update(H.DEFAULTS)
            cfg = os.path.join(self.tmpdir, "cfg.json")
            with io.open(cfg, "w", encoding="utf-8") as fh:
                json.dump(dict(H.DEFAULTS), fh)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env = dict(os.environ, PYTHONIOENCODING="cp1252")
            proc = subprocess.run(
                [sys.executable, os.path.join(root, "hublane.py"),
                 "--config", cfg, "--check"],
                env=env, capture_output=True, cwd=root)
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", "replace")[-300:])
        finally:
            if saved is not None:
                H.CONFIG["raw_upstreams"] = saved

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hl-utf8-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestPanelScroll(unittest.TestCase):
    """"最近请求"与"已校验真实 IP"两块固定高度 + 滚动

    这两块条数多、信息价值低(排障时看前几条就够), 不限高会把页脚顶出屏幕。
    """

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    def test_both_sections_wrapped_in_scroll_container(self):
        html = H.panel_html()
        self.assertEqual(html.count('<div class="scroll">'), 2)
        self.assertIn('<h2>最近请求</h2>\n<div class="scroll">', html)
        self.assertIn('<h2>已校验真实 IP</h2>\n<div class="scroll">', html)

    def test_scroll_container_has_fixed_height_and_overflow(self):
        html = H.panel_html()
        self.assertIn("max-height:%dpx" % H._scroll_px(), html)
        self.assertIn("overflow-y:auto", html)
        self.assertIn("overflow-x:auto", html)

    def test_header_sticks_while_scrolling(self):
        """表头吸顶: 滚动时仍能对列"""
        self.assertIn(".scroll thead th{position:sticky", H.panel_html())

    def test_height_follows_config(self):
        H.CONFIG["panel_scroll_rows"] = 20
        self.assertEqual(H._scroll_px(), 20 * 29)

    def test_height_clamped_to_sane_range(self):
        for rows, expect in ((1, 5), (5, 5), (40, 40), (9999, 40)):
            H.CONFIG["panel_scroll_rows"] = rows
            self.assertEqual(H._scroll_px(), expect * 29, rows)

    def test_bad_value_falls_back_to_default(self):
        for bad in ("abc", None, "", []):
            H.CONFIG["panel_scroll_rows"] = bad
            self.assertEqual(H._scroll_px(), 16 * 29, bad)

    def test_other_sections_not_scrolled(self):
        """只有这两块限高; 健康度/DoH/延迟表要完整展示"""
        html = H.panel_html()
        self.assertNotIn('<div class="scroll"><table><tr><th>域名</th><th>上游</th>',
                         html)


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
            with open(os.path.join(root, name),
                      encoding=("gbk" if name.endswith(".bat") else "utf-8")) as fh:
                found = set(re.findall(r"DNS:[A-Za-z0-9.*\-]+", fh.read()))
            missing = sorted(want - found)
            self.assertEqual(missing, [], "%s 缺少 SAN: %s" % (name, missing[:5]))


class TestCaExtensions(unittest.TestCase):
    """CA 必须带 keyUsage/basicConstraints, 否则 OpenSSL 3.5+ 拒绝校验

    回归: 2026-10 在 Python 3.13 / OpenSSL 3.6 上实测
    "CA cert does not include key usage extension", 15 个集成用例全挂。
    openssl <= 3.0 容忍缺 keyUsage 的 CA, 所以 CI 用老 openssl 发现不了。
    """

    def test_ca_extensions_are_declared(self):
        joined = " ".join(H.CA_EXTENSIONS)
        self.assertIn("CA:TRUE", joined)
        self.assertIn("keyCertSign", joined)
        self.assertIn("critical", joined)

    def test_leaf_extensions_are_declared(self):
        joined = " ".join(H.LEAF_EXTENSIONS)
        self.assertIn("CA:FALSE", joined)
        self.assertIn("serverAuth", joined)

    def test_installers_use_the_same_ca_extensions(self):
        """install.sh / install-windows.bat 不能落后于 hublane.py"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("install.sh", "install-windows.bat"):
            with open(os.path.join(root, name),
                      encoding=("gbk" if name.endswith(".bat") else "utf-8")) as fh:
                text = fh.read()
            for ext in H.CA_EXTENSIONS:
                self.assertIn(ext, text, "%s 缺少 CA 扩展: %s" % (name, ext))
            for ext in H.LEAF_EXTENSIONS:
                self.assertIn(ext, text, "%s 缺少叶证书扩展: %s" % (name, ext))

    @unittest.skipUnless(shutil.which("openssl"), "需要 openssl")
    def test_generated_ca_passes_strict_verification(self):
        """真正生成一次 CA+叶证书, 并用 openssl verify 校验链"""
        import subprocess
        import tempfile
        tmp = tempfile.mkdtemp(prefix="hublane-ca-test-")
        try:
            def run(*args):
                return subprocess.run(["openssl"] + list(args), check=True,
                                      capture_output=True)
            ca_key = os.path.join(tmp, "ca.key")
            ca_crt = os.path.join(tmp, "ca.crt")
            ca_csr = os.path.join(tmp, "ca.csr")
            ca_ext = os.path.join(tmp, "ca.ext")
            with open(ca_ext, "w", encoding="utf-8") as fh:
                fh.write("\n".join(H.CA_EXTENSIONS) + "\n")
            run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key,
                "-out", ca_csr, "-subj", H.CA_SUBJECT)
            run("x509", "-req", "-in", ca_csr, "-signkey", ca_key,
                "-extfile", ca_ext, "-days", "2", "-out", ca_crt)
            text = run("x509", "-noout", "-text", "-in", ca_crt).stdout.decode()
            self.assertIn("Certificate Sign", text)
            self.assertIn("CA:TRUE", text)
            leaf_key = os.path.join(tmp, "s.key")
            leaf_crt = os.path.join(tmp, "s.crt")
            csr = os.path.join(tmp, "s.csr")
            ext = os.path.join(tmp, "leaf.ext")
            with open(ext, "w", encoding="utf-8") as fh:
                fh.write("subjectAltName=DNS:localhost,IP:127.0.0.1\n%s\n"
                         % "\n".join(H.LEAF_EXTENSIONS))
            run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", leaf_key,
                "-out", csr, "-subj", H.LEAF_SUBJECT)
            run("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
                "-CAcreateserial", "-out", leaf_crt, "-days", "2", "-extfile", ext)
            out = run("verify", "-CAfile", ca_crt, leaf_crt).stdout.decode()
            self.assertIn("OK", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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


class TestCredentialAwareChain(unittest.TestCase):
    """敏感请求(带凭证 / git 写操作)不得投递给第三方镜像

    两条动机缺一不可:

    - 功能上: 公共镜像是匿名只读的, 没有授权代表客户端写 GitHub,
      所以 push 必然拿到 401;
    - 安全上: ``Authorization`` 是端到端头, 不在 ``_HOP_HEADERS`` 中,
      会被原样转发 —— 交给镜像等于把凭证泄漏给第三方运营方。
    """

    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)
        with H._ustat_lock:
            H._ustat.clear()

    def _third(self):
        return H._third_party_upstreams()

    def test_anonymous_read_still_uses_mirrors(self):
        """回归防线: 匿名读必须仍优先走镜像, 否则丢掉核心价值"""
        chain = H.build_chain("github.com", "/o/r.git/git-upload-pack", "GET", {})
        self.assertEqual(chain[0], "ghproxy_com_gh")
        self.assertIn("ghfast_gh", chain)

    def test_git_fetch_not_sensitive(self):
        """git fetch/clone 不算写操作, 仍可放心走镜像"""
        self.assertFalse(H.is_sensitive("POST", "/o/r.git/git-upload-pack", {}))

    def test_credential_marks_sensitive(self):
        self.assertTrue(H.is_sensitive("GET", "/x", {"Authorization": "token a"}))

    def test_credential_header_case_insensitive(self):
        self.assertTrue(H.is_sensitive("GET", "/x", {"authorization": "token a"}))

    def test_git_push_marks_sensitive(self):
        self.assertTrue(H.is_sensitive("POST", "/o/r.git/git-receive-pack", {}))

    def test_no_third_party_for_credentialed(self):
        chain = H.build_chain("github.com", "/o/r/info/refs", "GET",
                              {"Authorization": "token a"})
        self.assertTrue(chain)
        for name in chain:
            self.assertNotIn(name, self._third(),
                             "带凭证的请求走第三方镜像会泄漏凭证: %s" % name)
        self.assertIn("direct", chain)

    def test_no_third_party_for_git_push(self):
        chain = H.build_chain("github.com", "/o/r.git/git-receive-pack", "POST", {})
        for name in chain:
            self.assertNotIn(name, self._third())
        self.assertEqual(chain, ["direct", "watt"])

    def test_fallback_to_direct_when_all_third_party(self):
        """上游全是第三方时兜底 direct, 而不是把凭证交出去"""
        self.assertEqual(H.restrict_upstreams(["ghproxy_com_gh"], True), ["direct"])

    def test_not_sensitive_passes_through_untouched(self):
        names = ["ghproxy_com_gh", "direct"]
        self.assertEqual(H.restrict_upstreams(names, False), names)

    def test_raw_host_also_protected(self):
        chain = H.build_chain("raw.githubusercontent.com", "/a/b.txt", "GET",
                              {"Authorization": "token a"})
        for name in chain:
            self.assertNotIn(name, self._third())

    def test_two_arg_call_still_anonymous(self):
        """向后兼容: 不传 method/headers 时按匿名处理"""
        self.assertEqual(H.build_chain("github.com", "/x")[0], "ghproxy_com_gh")


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


class TestWindowsInstallParity(unittest.TestCase):
    """Windows 安装脚本能力对齐(v0.2.0 第 1 条)与脚本自身的坑

    这些只能静态断言兜底 —— cmd / PowerShell 的解析错误不会在 Linux CI 上暴露,
    而是等用户实机安装时才炸(历史已炸过三次)。
    """

    SCRIPTS = ("install.sh", "install-windows.bat", "install-windows-service.bat",
               "uninstall-windows.bat", "uninstall-windows-service.bat",
               "tools/setup-firefox-policy.ps1")

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.root = root
        cls.texts = {}
        for name in cls.SCRIPTS:
            with open(os.path.join(root, name), "rb") as fh:
                raw = fh.read()
            # .bat 是 GBK(CP936) 无 BOM; .ps1 是 UTF-8 带 BOM —— 各自按真实编码解码
            if name.endswith(".bat"):
                cls.texts[name] = raw.decode("gbk")
            else:
                cls.texts[name] = raw.decode("utf-8-sig")

    def test_renew_passthrough_on_windows(self):
        """--renew-certs / --renew-ca 透传不能只做 Linux 侧"""
        for name in ("install-windows.bat", "install-windows-service.bat"):
            text = self.texts[name]
            self.assertIn("--renew-certs", text, "%s 缺少 --renew-certs 透传" % name)
            self.assertIn("--renew-ca", text, "%s 缺少 --renew-ca 透传" % name)
            self.assertIn("--config", text)

    def test_firefox_policy_implemented(self):
        """此前只 echo 一句"请手动导入", 承诺的 policies.json 从未实现"""
        ff = self.texts["tools/setup-firefox-policy.ps1"]
        self.assertIn("Certificates", ff)
        self.assertIn("ImportEnterpriseRoots", ff)
        self.assertIn("policies.json", ff)
        for name in ("install-windows.bat", "install-windows-service.bat"):
            self.assertIn("setup-firefox-policy.ps1", self.texts[name],
                          "%s 没有接 Firefox policies.json" % name)

    def test_windows_scripts_are_crlf(self):
        """.bat / .ps1 必须用 CRLF; LF 会让 cmd 解析括号块出错(.gitattributes 管不到工作区)"""
        bats = [n for n in self.SCRIPTS if n.endswith(".bat")]
        bats.append("tools/setup-firefox-policy.ps1")
        for name in bats:
            with open(os.path.join(self.root, name), "rb") as fh:
                raw = fh.read()
            self.assertIn(b"\r\n", raw, "%s 不是 CRLF" % name)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""),
                             "%s 混入裸 LF" % name)

    def test_windows_batch_are_gbk_no_bom(self):
        """Windows 批处理中文编码坑(已在中文 Windows 实机复现):

        - 不能 UTF-8 无 BOM: 中文 Windows 默认 OEM 是 CP936, cmd 把无 BOM 的
          UTF-8 .bat 当 GBK 解码, 中文变乱码, 乱码字节里混进 ')'/'&' 把结构打碎,
          报一堆"不是内部或外部命令"(install/uninstall 的 service bat 实测炸过)。
        - 不能加 UTF-8 BOM: cmd 不识别 BOM, 会把 EF BB BF 当成首行内容弄坏
          @echo off(表现为 '锘匡豢@echo' 不是内部命令)。
        - 唯一稳妥: 文件本身存成 GBK(CP936) 无 BOM, 与系统解码一致; 且脚本里
          chcp 用 936 而非 65001, 让控制台输出编码与文件一致。
        """
        for name in self.SCRIPTS:
            if not name.endswith(".bat"):
                continue
            with open(os.path.join(self.root, name), "rb") as fh:
                raw = fh.read()
            # 绝不能带 UTF-8 BOM —— cmd 会把 BOM 当首行内容
            self.assertNotIn(b"\xef\xbb\xbf", raw,
                             "%s 不应有 UTF-8 BOM(cmd 会弄坏首行)" % name)
            # 必须能被 CP936(GBK) 无损解码 —— 证明它本就是 GBK 编码
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                self.fail("%s 不是合法 GBK 编码(中文 Windows 会读成乱码)" % name)
            # 脚本内 chcp 必须是 936, 与 GBK 文件编码一致(不是 65001)
            self.assertIn("chcp 936", text,
                          "%s 应使用 chcp 936(与 GBK 文件编码一致)" % name)
            self.assertNotIn("chcp 65001", text,
                             "%s 不应使用 chcp 65001(与 GBK 文件编码冲突)" % name)

    def test_powershell_scripts_have_utf8_bom(self):
        """无 BOM 时 Windows PowerShell 5.1 按 ANSI 解码中文, 双字节序列可能
        "造出"一个 `{` 或引号, 让整段脚本语法错误(setup-git-ssh.ps1 实测过)"""
        for name in ("tools/setup-firefox-policy.ps1", "tools/setup-git-ssh.ps1",
                     "tools/setup-windows-env.ps1", "tools/verify-windows.ps1"):
            with open(os.path.join(self.root, name), "rb") as fh:
                head = fh.read(3)
            self.assertEqual(head, b"\xef\xbb\xbf", "%s 缺少 UTF-8 BOM" % name)

    def test_wait_uses_ping_not_timeout(self):
        """timeout 在 stdin 不是控制台时(脚本调脚本/输出重定向)直接报错退出"""
        for name in ("install-windows.bat", "install-windows-service.bat",
                     "uninstall-windows-service.bat"):
            self.assertNotIn("timeout /t", self.texts[name],
                             "%s 仍在用 timeout 等待" % name)


class TestV020Features(unittest.TestCase):
    """v0.2.0 新增项的单元覆盖

    原则: 全部离线。涉及网络的地方用桩替换, 保证 CI(无外网)可跑。
    """
    def setUp(self):
        H.CONFIG.clear()
        H.CONFIG.update(H.DEFAULTS)

    # ---- 第 1 条: verbose / enable_socks5 ----
    def test_verbose_forces_debug(self):
        old = H.log.level
        try:
            H.CONFIG["verbose"] = True
            H.setup_logging()
            self.assertEqual(H.log.level, logging.DEBUG)
            H.CONFIG["verbose"] = False
            H.CONFIG["log_level"] = "INFO"
            H.setup_logging()
            self.assertEqual(H.log.level, logging.INFO)
        finally:
            H.log.setLevel(old)

    def test_verbose_default_is_false(self):
        # 默认 true 会把 DEBUG 访问日志全刷进文件, 既吵又占空间
        self.assertFalse(H.DEFAULTS["verbose"])

    # ---- 第 5 条: 动态已验真 IP 池 ----
    def test_is_ip(self):
        self.assertTrue(H._is_ip("1.2.3.4"))
        self.assertTrue(H._is_ip("2001:db8::1"))
        for bad in ("", "not-an-ip", "1.2.3.4/24", "a b"):
            self.assertFalse(H._is_ip(bad), bad)

    def test_validate_preset_ips(self):
        errs = []
        H._validate_preset_ips({"preset_ips": {"a.test": ["1.2.3.4"]}}, errs)
        self.assertEqual(errs, [])
        errs = []
        H._validate_preset_ips({"preset_ips": {"a.test": ["not-an-ip"]}}, errs)
        self.assertTrue(errs, "非法 IP 必须报错")
        errs = []
        H._validate_preset_ips({"preset_ips": []}, errs)
        self.assertTrue(errs, "非字典必须报错")

    def test_pick_ips_merges_preset_history_and_doh(self):
        H.CONFIG["preset_ips"] = {"a.test": ["10.0.0.1"]}
        with H._ipcache_lock:
            H._ipcache["a.test"] = (time.time() - 10, [["10.0.0.2", socket.AF_INET]])
        seen = []
        old_r, old_v = H.doh_resolve, H._verify_ip
        H.doh_resolve = lambda host: [("10.0.0.3", socket.AF_INET)]
        H._verify_ip = lambda host, ip, fam, timeout: (seen.append(ip) or 0.01)
        try:
            got = H.pick_ips("a.test", force=True)
        finally:
            H.doh_resolve, H._verify_ip = old_r, old_v
            with H._ipcache_lock:
                H._ipcache.pop("a.test", None)
        self.assertEqual(sorted(seen), ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual([p[0] for p in got], ["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_pick_ips_drops_dead_history(self):
        with H._ipcache_lock:
            H._ipcache["b.test"] = (time.time() - 10, [["10.0.0.9", socket.AF_INET]])
        old_r, old_v = H.doh_resolve, H._verify_ip
        H.doh_resolve = lambda host: []
        H._verify_ip = lambda *a, **k: None          # 全部验真失败
        try:
            got = H.pick_ips("b.test", force=True)
        finally:
            H.doh_resolve, H._verify_ip = old_r, old_v
            with H._ipcache_lock:
                H._ipcache.pop("b.test", None)
        self.assertEqual(got, [], "有候选却全没验过, 不该复活历史 IP")

    # ---- 第 7 条: GET /hosts ----
    def test_hosts_text_is_readonly(self):
        with H._ipcache_lock:
            H._ipcache["c.test"] = (time.time(), [["1.2.3.4", socket.AF_INET]])
        try:
            text = H.hosts_text()
        finally:
            with H._ipcache_lock:
                H._ipcache.pop("c.test", None)
        self.assertIn("1.2.3.4 c.test", text)
        self.assertIn("不会写入系统 hosts", text)

    def test_hosts_text_empty_pool(self):
        self.assertIn("暂无已验真 IP", H.hosts_text())

    def test_ip_pool_summary(self):
        with H._ipcache_lock:
            H._ipcache["d.test"] = (time.time(), [["5.6.7.8", socket.AF_INET]])
        try:
            text = H.ip_pool_summary()
        finally:
            with H._ipcache_lock:
                H._ipcache.pop("d.test", None)
        self.assertIn("5.6.7.8", text)
        self.assertIn("d.test", text)

    # ---- 第 6b 条: abort 伪上游 ----
    def test_abort_is_known_upstream(self):
        H.CONFIG["per_host_upstreams"] = {"z.test": ["abort"]}
        self.assertEqual(H.validate_config(), [])

    def test_abort_fails_fast_without_touching_network(self):
        before = H.counters_snapshot().get("aborted", 0)
        ok, errors = H.relay_chain(None, "x.test", "/p", "GET", {}, b"", ["abort"])
        self.assertFalse(ok)
        self.assertTrue(any("abort" in str(e) for e in errors))
        self.assertEqual(H.counters_snapshot().get("aborted", 0), before + 1)

    # ---- 第 10 条: 暂停/恢复 ----
    def test_pause_makes_managed_host_pass_through(self):
        try:
            self.assertTrue(H.should_intercept("github.com"))
            ok, _msg = H.set_paused(True)
            self.assertTrue(ok)
            self.assertFalse(H.should_intercept("github.com"),
                             "暂停期间不该再接管受管域名")
            self.assertTrue(H.status_json()  # /status 要能看出处于暂停
                            and json.loads(H.status_json())["paused"])
        finally:
            H.set_paused(False)
        self.assertTrue(H.should_intercept("github.com"), "恢复后应重新接管")

    def test_pause_btn_switches(self):
        try:
            H.set_paused(True)
            self.assertIn("/resume", H.panel_html())
            H.set_paused(False)
            self.assertIn("/pause", H.panel_html())
        finally:
            H.set_paused(False)

    # ---- Windows 实测发现的面板渲染问题 ----
    def test_panel_autorefresh_keeps_url_tool_input(self):
        """meta refresh 每 5s 整页重载, 会把 URL 工具里贴的 URL 与生成结果
        一起清掉(Chrome/Edge 实测); 改为 JS 定时刷新并在输入框有内容时跳过"""
        page = H.panel_html()
        self.assertNotIn('http-equiv="refresh"', page)
        self.assertIn("setInterval", page)
        self.assertIn("hlUrl", page)

    def test_panel_sort_keeps_header_row(self):
        """表格没有 <thead>, 表头行就在 tbody 里 —— 不筛掉的话第一次排序
        表头会被当成数据行挪走(Chrome/Edge 实测)"""
        self.assertIn("!r.querySelector('th')", H.panel_html())

    # ---- 第 9 条: URL -> 等价命令 ----
    def test_cmd_for_url_git(self):
        got = H.cmd_for_url("https://github.com/o/r.git")
        self.assertTrue(got["ok"])
        labels = [c["label"] for c in got["commands"]]
        self.assertIn("git clone", labels)
        self.assertIn("curl", labels)
        self.assertIn("http://127.0.0.1:%d" % H.CONFIG["listen_port"], got["proxy"])

    def test_cmd_for_url_plain(self):
        got = H.cmd_for_url("https://example.com/a.zip")
        labels = [c["label"] for c in got["commands"]]
        self.assertNotIn("git clone", labels)

    def test_cmd_for_url_empty(self):
        self.assertFalse(H.cmd_for_url("")["ok"])

    # ---- 第 11 条: PAC 有效性 ----
    def test_pac_covers_per_host_exact_domains(self):
        # 回归: 此前 PAC 只取 GH/RAW/extra, 漏掉 per_host_upstreams 的精确域名,
        # 于是浏览器走 PAC 时会绕过代理, 配的规则形同虚设。
        H.CONFIG["per_host_upstreams"] = {"blocked.test": ["abort"]}
        pac = H.pac_content()
        self.assertIn('"blocked.test": 1', pac)
        self.assertIn('"github.com": 1', pac)

    def test_pac_lets_unmanaged_go_direct(self):
        pac = H.pac_content()
        self.assertNotIn("baidu.com", pac, "非受管域名绝不能进代理")
        self.assertIn('return "DIRECT";', pac)

    def test_pac_uses_listen_port(self):
        H.CONFIG["listen_port"] = 12345
        self.assertIn("PROXY 127.0.0.1:12345", H.pac_content())

    # ---- 第 4/12 条: 版本比较与更新查询 ----
    def test_ver_tuple(self):
        self.assertEqual(H._ver_tuple("v0.2.0"), (0, 2, 0))
        self.assertTrue(H._ver_tuple("v0.1.1") < H._ver_tuple("v0.2.0"))
        self.assertTrue(H._ver_tuple("v0.1.1") == H._ver_tuple("0.1.1"))

    def test_check_update_reports_newer_and_current(self):
        class FakeResp(object):
            def __init__(self, data):
                self._d = json.dumps(data).encode("utf-8")

            def read(self):
                return self._d

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeOpener(object):
            def __init__(self, data):
                self.d = data

            def open(self, url, timeout=None):
                return FakeResp(self.d)

        old = H._OPENER
        try:
            H._OPENER = FakeOpener({"tag_name": "v9.9.9", "html_url": "u"})
            newer = H.check_update()
            H._OPENER = FakeOpener({"tag_name": "v0.0.1", "html_url": "u"})
            older = H.check_update()
        finally:
            H._OPENER = old
        self.assertTrue(newer["ok"])
        self.assertFalse(newer["up_to_date"])
        self.assertTrue(older["up_to_date"])

    # ---- 日志打码(接 verbose 时暴露的真实缺陷) ----
    def test_redact_query_masks_token(self):
        line = 'GET /status?token=supersecret HTTP/1.1'
        red = H._redact_query(line)
        self.assertIn("token=***", red)
        self.assertNotIn("supersecret", red)

    def test_redact_query_leaves_plain_text(self):
        self.assertEqual(H._redact_query("no token here"), "no token here")

    def test_looks_like_python_rejects_junk(self):
        self.assertTrue(H._looks_like_python(b"VERSION = '1'\ndef main():\n    pass"))
        self.assertFalse(H._looks_like_python(b"<html>502</html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
