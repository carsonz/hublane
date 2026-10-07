#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hublane 本地压测: 吞吐与延迟(不需要外网)

起一个本地假上游 + 进程内 hublane, 用真 TLS 客户端并发拉取, 输出 rps / P50 / P95:

    python3 bench/bench_relay.py                    # 默认 200 请求 / 8 并发
    python3 bench/bench_relay.py -n 500 -c 16 --size 262144
    python3 bench/bench_relay.py --save bench/baseline.json     # 存基线
    python3 bench/bench_relay.py --check bench/baseline.json    # 回归对比

退出码: 0 通过; 1 与基线相比明显变差(默认 P95 超过基线 1.5 倍); 2 环境不满足。
"""
import argparse
import json
import os
import shutil
import socket
import socketserver
import ssl
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import hublane as H                       # noqa: E402
from tests.test_integration import make_certs   # noqa: E402

RAW_HOST = "raw.githubusercontent.com"


class Origin(socketserver.ThreadingTCPServer):
    """假上游: 任何路径都返回固定大小的响应体"""

    allow_reuse_address = True
    daemon_threads = True
    payload = b"x" * 65536

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def port(self):
        return self.server_address[1]


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                head += chunk
            body = Origin.payload
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n"
                                 % len(body) + body)
        except Exception:
            pass


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_stack(size, log_level="ERROR"):
    """返回 (停止函数, 监听端口, 证书目录)"""
    if not H.find_openssl():          # 复用 hublane 的探测(含 Git for Windows 路径)
        raise SystemExit("需要 openssl 生成测试证书")
    tmp = tempfile.mkdtemp(prefix="hublane-bench-")
    certs = make_certs(tmp)
    origin = Origin()
    origin.payload = b"x" * size
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    port = free_port()
    config = dict(H.DEFAULTS)
    config.update({
        "listen_host": "127.0.0.1", "listen_port": port,
        "metrics_enabled": False,
        "raw_upstreams": ["local_ok"],
        "custom_mirrors": {"local_ok": "http://127.0.0.1:%d/ok{path}" % origin.port},
        "doh_endpoints": [], "probe_enabled": False,
        "enable_ipv6": False, "refresh_interval": 3600,
        "log_level": log_level, "timeout": 30, "sample_size": 0,
    })
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    for name, value in (("CERT", certs["cert"]), ("KEY", certs["key"]),
                        ("STATE", os.path.join(tmp, "state.json")),
                        ("LOG_FILE", os.path.join(tmp, "bench.log"))):
        setattr(H, name, value)
    H._srvctx = None
    H.CONFIG.clear()
    H.CONFIG.update(H.DEFAULTS)
    H._stop.clear()
    threading.Thread(target=H.main, args=(["--config", cfg_path],), daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise SystemExit("hublane 未能在 10s 内启动")

    def stop():
        H._stop.set()
        time.sleep(0.3)
        origin.shutdown()
        origin.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    return stop, port, certs


def one_request(port, cafile, path, timeout=30):
    """经 hublane 的 MITM 发一次请求, 返回 (耗时秒, 字节数)"""
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    start = time.time()
    try:
        raw.sendall(("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n"
                     % (RAW_HOST, RAW_HOST)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(1024)
            if not chunk:
                return None, 0
            head += chunk
        ctx = ssl.create_default_context(cafile=cafile)
        tls = ctx.wrap_socket(raw, server_hostname=RAW_HOST)
        tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                     % (path, RAW_HOST)).encode())
        total = 0
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            total += len(chunk)
        return time.time() - start, total
    finally:
        try:
            raw.close()
        except Exception:
            pass


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) + 1))) - 1)
    return ordered[max(0, index)]


def run(total, concurrency, size, warmup=5):
    stop, port, certs = start_stack(size)
    try:
        for i in range(warmup):
            one_request(port, certs["ca"], "/warmup/%d" % i)
        latencies = []
        failures = 0
        lock = threading.Lock()
        remaining = [total]

        def worker(idx):
            nonlocal failures
            while True:
                with lock:
                    if remaining[0] <= 0:
                        return
                    remaining[0] -= 1
                    n = total - remaining[0]
                elapsed, got = one_request(port, certs["ca"], "/bench/%d" % n)
                with lock:
                    if elapsed is None or got == 0:
                        failures += 1
                    else:
                        latencies.append(elapsed)

        started = time.time()
        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(concurrency)]
        for one in threads:
            one.start()
        for one in threads:
            one.join()
        wall = time.time() - started
    finally:
        stop()
    return {"requests": total, "concurrency": concurrency, "size": size,
            "wall_seconds": round(wall, 3),
            "rps": round(len(latencies) / wall, 1) if wall else 0,
            "p50_ms": round(percentile(latencies, 0.5) * 1000, 1) if latencies else None,
            "p95_ms": round(percentile(latencies, 0.95) * 1000, 1) if latencies else None,
            "avg_ms": round(statistics.mean(latencies) * 1000, 1) if latencies else None,
            "failures": failures}


def main(argv=None):
    parser = argparse.ArgumentParser(description="hublane 本地压测")
    parser.add_argument("-n", "--requests", type=int, default=200)
    parser.add_argument("-c", "--concurrency", type=int, default=8)
    parser.add_argument("--size", type=int, default=65536, help="响应体字节数")
    parser.add_argument("--save", metavar="PATH", help="把结果存为基线")
    parser.add_argument("--check", metavar="PATH", help="与基线对比")
    parser.add_argument("--tolerance", type=float, default=1.5,
                        help="P95 相对基线的容忍倍数(默认 1.5)")
    args = parser.parse_args(argv)
    result = run(args.requests, args.concurrency, args.size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("已保存基线: %s" % args.save)
    if args.check:
        try:
            with open(args.check, encoding="utf-8") as fh:
                baseline = json.load(fh)
        except OSError as exc:
            print("读取基线失败: %s" % exc, file=sys.stderr)
            return 2
        base_p95 = baseline.get("p95_ms")
        now_p95 = result.get("p95_ms")
        if base_p95 and now_p95 and now_p95 > base_p95 * args.tolerance:
            print("性能回归: P95 %.1fms > 基线 %.1fms × %.1f"
                  % (now_p95, base_p95, args.tolerance), file=sys.stderr)
            return 1
        if result.get("failures"):
            print("有失败请求: %s" % result["failures"], file=sys.stderr)
            return 1
        print("与基线一致(基线 P95 %.1fms, 本次 %.1fms)" % (base_p95 or 0, now_p95 or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
