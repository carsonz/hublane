"""Windows 服务模式与纯 TCP 隧道的可测单元。

这两块此前没有自动化覆盖:
  - ``_win_service()`` 原来把状态上报/控制分发全塞在函数闭包里, 直接 import
    ctypes.WINFUNCTYPE + advapi32, 无法单测。现已把纯逻辑抽成
    ``hublane.ServiceCore``, 用假 backend 即可在任意平台驱动。
  - ``tunnel()`` 之前完全不计数, 走隧道的流量对面板不可见。
    现在有 tunnel_conns / tunnel_fail / tunnel_bytes 三个计数。

本文件不依赖 Windows, 在 Linux CI 上同样运行。
"""
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hublane as H  # noqa: E402


class FakeBackend(object):
    """记录 SetServiceStatus 调用, 不碰 advapi32"""

    def __init__(self):
        self.registered = []
        self.statuses = []

    def register(self, handler):
        self.registered.append(handler)
        return "fake-handle"

    def set_status(self, handle, fields):
        self.statuses.append((handle, dict(fields)))


class ServiceCoreTest(unittest.TestCase):
    """ServiceCore 状态机"""

    def setUp(self):
        self.backend = FakeBackend()
        self.stop = threading.Event()
        self.core = H.ServiceCore(self.backend, stop_event=self.stop)

    def states(self):
        return [f["state"] for _h, f in self.backend.statuses]

    def test_constants_match_scm_values(self):
        self.assertEqual(H.SVC_WIN32_OWN_PROCESS, 0x10)
        self.assertEqual(H.SVC_ACCEPT_STOP, 0x01)
        self.assertEqual(H.SVC_STOPPED, 0x01)
        self.assertEqual(H.SVC_STOP_PENDING, 0x03)
        self.assertEqual(H.SVC_RUNNING, 0x04)
        self.assertEqual((H.CTRL_STOP, H.CTRL_SHUTDOWN, H.CTRL_INTERROGATE),
                         (0x01, 0x05, 0x04))
        self.assertEqual(H.ERROR_SERVICE_DISABLED, 1063)

    def test_report_before_handle_is_buffered_only(self):
        """没有控制句柄时不能调 SetServiceStatus, 否则 SCM 会拒绝"""
        self.core.report(H.SVC_RUNNING)
        self.assertEqual(self.backend.statuses, [])
        self.assertEqual(self.core.last["state"], H.SVC_RUNNING)

    def test_running_accepts_stop(self):
        self.core.handle = self.backend.register(lambda *a: 0)
        self.core.report(H.SVC_RUNNING)
        _handle, fields = self.backend.statuses[-1]
        self.assertEqual(fields["controls_accepted"], H.SVC_ACCEPT_STOP)
        self.assertEqual(fields["service_type"], H.SVC_WIN32_OWN_PROCESS)
        self.assertEqual(fields["exit_code"], 0)

    def test_non_running_states_refuse_stop(self):
        """非 RUNNING 不接受停止, 否则启动途中被 stop 会让 SCM 判异常"""
        self.core.handle = "fake-handle"
        for state in (H.SVC_STOPPED, H.SVC_STOP_PENDING):
            self.core.report(state)
            _handle, fields = self.backend.statuses[-1]
            self.assertEqual(fields["controls_accepted"], 0, state)

    def test_ctrl_stop_reports_stop_pending_and_signals(self):
        self.core.handle = "fake-handle"
        self.core.report(H.SVC_RUNNING)
        self.assertEqual(self.core.on_control(H.CTRL_STOP), 0)
        self.assertEqual(self.states()[-1], H.SVC_STOP_PENDING)
        _handle, fields = self.backend.statuses[-1]
        self.assertEqual(fields["wait_hint"], 30000)
        self.assertEqual(fields["checkpoint"], 1)
        self.assertTrue(self.stop.is_set(), "必须置位停止事件, 否则 sc stop 卡住")

    def test_ctrl_shutdown_behaves_like_stop(self):
        self.core.handle = "fake-handle"
        self.core.report(H.SVC_RUNNING)
        self.core.on_control(H.CTRL_SHUTDOWN)
        self.assertEqual(self.states()[-1], H.SVC_STOP_PENDING)
        self.assertTrue(self.stop.is_set())

    def test_ctrl_interrogate_reports_state_without_stopping(self):
        self.core.handle = "fake-handle"
        self.core.report(H.SVC_RUNNING)
        before = len(self.backend.statuses)
        self.assertEqual(self.core.on_control(H.CTRL_INTERROGATE), 0)
        self.assertEqual(len(self.backend.statuses), before + 1)
        self.assertEqual(self.states()[-1], H.SVC_RUNNING)
        self.assertFalse(self.stop.is_set(), "查询状态不应触发停止")

    def test_ctrl_interrogate_before_any_report(self):
        self.core.handle = "fake-handle"
        self.core.on_control(H.CTRL_INTERROGATE)
        self.assertEqual(self.states()[-1], H.SVC_STOPPED)

    def test_unknown_control_is_ignored(self):
        self.core.handle = "fake-handle"
        self.core.report(H.SVC_RUNNING)
        before = len(self.backend.statuses)
        self.assertEqual(self.core.on_control(0xFF), 0)
        self.assertEqual(len(self.backend.statuses), before)
        self.assertFalse(self.stop.is_set())

    def test_full_lifecycle_sequence(self):
        self.core.handle = self.backend.register(lambda *a: 0)
        self.assertEqual(self.core.handle, "fake-handle")
        self.core.report(H.SVC_RUNNING)
        self.core.on_control(H.CTRL_STOP)
        self.core.report(H.SVC_STOPPED)
        self.assertEqual(self.states(),
                         [H.SVC_RUNNING, H.SVC_STOP_PENDING, H.SVC_STOPPED])
        self.assertEqual(self.core.last["controls_accepted"], 0)

    def test_backend_register_called_with_handler(self):
        self.core.handle = self.backend.register(self.core.on_control)
        self.assertEqual(self.backend.registered, [self.core.on_control])


@unittest.skipUnless(hasattr(socket, "socketpair"),
                     "socket.socketpair 在 Windows 上要 Python 3.13+")
class TunnelCountersTest(unittest.TestCase):
    """tunnel() 的隧道维度计数"""

    HTTP_KEYS = ("requests", "ok", "fail", "truncated", "bytes")

    def setUp(self):
        self._saved = H.counters_snapshot()
        with H._counters_lock:
            for key in ("tunnel_conns", "tunnel_fail", "tunnel_bytes"):
                H._counters[key] = 0

    def tearDown(self):
        with H._counters_lock:
            H._counters.update(self._saved)

    @staticmethod
    def _close(*socks):
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass

    def test_successful_tunnel_counts_conn_and_bytes(self):
        # socketpair 充当"客户端<->代理"与"代理<->目标"两条链路;
        # tunnel() 自己会发起真实连接, 这里把 create_connection 换掉。
        client_sock, proxy_sock = socket.socketpair()
        remote_sock, origin_sock = socket.socketpair()
        payload = b"hello-tunnel-payload"
        got = b""
        saved = H.socket.create_connection
        H.socket.create_connection = lambda *a, **k: remote_sock
        thread = threading.Thread(
            target=H.tunnel, args=(proxy_sock, "example.test", 80), daemon=True)
        thread.start()
        try:
            client_sock.sendall(payload)
            client_sock.shutdown(socket.SHUT_WR)
            while len(got) < len(payload):
                chunk = origin_sock.recv(65536)
                if not chunk:
                    break
                got += chunk
        finally:
            thread.join(timeout=5)
            H.socket.create_connection = saved
            self._close(client_sock, proxy_sock, remote_sock, origin_sock)

        counters = H.counters_snapshot()
        self.assertEqual(got, payload, "payload 应被中继到目标端")
        self.assertEqual(counters["tunnel_conns"], 1)
        self.assertEqual(counters["tunnel_fail"], 0)
        self.assertEqual(counters["tunnel_bytes"], len(payload))

    def test_failed_tunnel_counts_conn_and_fail(self):
        client_sock, peer_sock = socket.socketpair()
        before = H.counters_snapshot()
        try:
            H.tunnel(client_sock, "127.0.0.1", 1)   # 保留端口, 必定连不上
        finally:
            self._close(client_sock, peer_sock)

        after = H.counters_snapshot()
        self.assertEqual(after["tunnel_conns"], before["tunnel_conns"] + 1)
        self.assertEqual(after["tunnel_fail"], before["tunnel_fail"] + 1)
        self.assertEqual(after["tunnel_bytes"], before["tunnel_bytes"])

    def test_tunnel_does_not_pollute_http_counters(self):
        """隧道不并入 requests/ok/fail —— 那三项口径是 HTTP 请求与上游尝试"""
        client_sock, peer_sock = socket.socketpair()
        before = H.counters_snapshot()
        try:
            H.tunnel(client_sock, "127.0.0.1", 1)
        finally:
            self._close(client_sock, peer_sock)
        after = H.counters_snapshot()
        for key in self.HTTP_KEYS:
            self.assertEqual(after[key], before[key], key)

    def test_tunnel_duration_not_in_latency_histogram(self):
        """隧道可存活数分钟, 混进直方图会带偏 HTTP 延迟分位数"""
        client_sock, peer_sock = socket.socketpair()
        before = H.latency_stats()
        try:
            H.tunnel(client_sock, "127.0.0.1", 1)
        finally:
            self._close(client_sock, peer_sock)
        self.assertEqual(H.latency_stats(), before)


class FormatBytesTest(unittest.TestCase):
    """面板字节格式化"""

    def test_units(self):
        self.assertEqual(H._fmt_bytes(0), "0 B")
        self.assertEqual(H._fmt_bytes(512), "512 B")
        self.assertEqual(H._fmt_bytes(8430), "8.2 KB")
        self.assertEqual(H._fmt_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(H._fmt_bytes(3 * 1024 ** 3), "3.0 GB")

    def test_bad_input(self):
        self.assertEqual(H._fmt_bytes(None), "-")
        self.assertEqual(H._fmt_bytes("abc"), "-")


if __name__ == "__main__":
    unittest.main()
