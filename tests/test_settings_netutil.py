"""netutil transport tests: redirects are never
followed, oversized bodies hit the cap, drip-feed servers are cut by the
wall-clock deadline, and blocked targets raise without connecting.

Because :func:`netutil.open_no_redirect` deliberately REJECTS loopback targets,
the cap/deadline/redirect stubs must be reachable on a NON-loopback address: the
stub binds ``0.0.0.0`` and the test targets the machine's own non-loopback IP
(discovered with a UDP ``connect`` probe — the classic SSRF-test trick).
"""

import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netutil


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET: never routable, no packets sent
        return s.getsockname()[0]
    finally:
        s.close()


def _start(handler_cls, host="0.0.0.0", port=0):
    srv = ThreadingHTTPServer((host, port), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port, t


class _RedirectHandler(BaseHTTPRequestHandler):
    """Answers every request with a 302 pointing at the second stub (a loopback
    target open_no_redirect must never connect to)."""

    target = ""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._redirect()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self._redirect()

    def _redirect(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _CountingHandler(BaseHTTPRequestHandler):
    """The steal target: counts every request it receives (should stay 0)."""

    count = 0

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        type(self).count += 1
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _BigBodyHandler(BaseHTTPRequestHandler):
    size = 0

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(self.size))
        self.end_headers()
        try:
            self.wfile.write(b"x" * self.size)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # the client aborts once it hits the 1 MiB cap


class _DripHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"y")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class NetutilRedirectTest(unittest.TestCase):
    def setUp(self):
        self.ip = _local_ip()
        if not self.ip:
            self.skipTest("no non-loopback address available")

    # a 3xx is raised, never followed — the second stub sees zero requests.
    def test_redirect_not_followed(self):
        steal = _CountingHandler
        steal.count = 0
        _steal_srv, steal_port, _ = _start(steal)

        redir = _RedirectHandler
        redir.target = f"http://127.0.0.2:{steal_port}/steal"
        redir_srv, redir_port, _ = _start(redir)

        try:
            with self.assertRaises(netutil.NetError) as cm:
                netutil.open_no_redirect(f"http://{self.ip}:{redir_port}/models")
            self.assertEqual(cm.exception.reason, "redirect not followed")
            self.assertEqual(cm.exception.status, 302)
            self.assertEqual(steal.count, 0,
                             "the 302 target must receive zero requests")
        finally:
            redir_srv.shutdown(); redir_srv.server_close()
            _steal_srv.shutdown(); _steal_srv.server_close()

    # an 8 MiB body is rejected by the 1 MiB cap.
    def test_oversized_body_rejected_by_cap(self):
        handler = _BigBodyHandler
        handler.size = 8 * 1024 * 1024  # 8 MiB
        srv, port, _ = _start(handler)
        try:
            with self.assertRaises(netutil.NetError) as cm:
                netutil.open_no_redirect(f"http://{self.ip}:{port}/models")
            self.assertEqual(cm.exception.reason, "response too large")
        finally:
            srv.shutdown(); srv.server_close()

    # a drip-feed server is cut off by the wall-clock deadline.
    def test_drip_feed_cut_by_deadline(self):
        srv, port, _ = _start(_DripHandler)
        try:
            t0 = time.monotonic()
            with self.assertRaises(netutil.NetError) as cm:
                netutil.open_no_redirect(f"http://{self.ip}:{port}/models",
                                         timeout=1.0)
            self.assertEqual(cm.exception.reason, "deadline exceeded")
            self.assertLess(time.monotonic() - t0, 3.0)
        finally:
            srv.shutdown(); srv.server_close()

    # a blocked target raises before any connection is attempted.
    def test_blocked_target_raises_without_connecting(self):
        with self.assertRaises(netutil.NetError) as cm:
            netutil.open_no_redirect("http://127.0.0.1:9/v1")
        self.assertEqual(cm.exception.reason, "blocked target")

    # the generation path shares open_no_redirect, so a 302 stub there
    # is also never followed and the steal target receives zero requests.
    def test_generation_path_does_not_follow_redirect(self):
        import gen

        steal = _CountingHandler
        steal.count = 0
        steal_srv, steal_port, _ = _start(steal)

        redir = _RedirectHandler
        redir.target = f"http://127.0.0.2:{steal_port}/steal"
        redir_srv, redir_port, _ = _start(redir)

        try:
            res = gen.generate_questions(
                "science", 3, model="m",
                base=f"http://{self.ip}:{redir_port}/v1", max_calls=1)
            self.assertEqual(steal.count, 0,
                             "generation must never follow the 302")
            self.assertEqual(len(res["accepted"]), 0)
        finally:
            redir_srv.shutdown(); redir_srv.server_close()
            steal_srv.shutdown(); steal_srv.server_close()

    def test_metadata_host_rejected(self):
        with self.assertRaises(netutil.NetError) as cm:
            netutil.open_no_redirect("http://169.254.169.254/v1")
        self.assertEqual(cm.exception.reason, "blocked target")

    def test_net_error_text_never_carries_url_or_auth(self):
        # the exception text is a fixed reason + status, never the URL,
        # never the Authorization header.
        try:
            netutil.open_no_redirect("http://127.0.0.1:9/v1",
                                     headers={"Authorization": "Bearer fake-topsecret"})
        except netutil.NetError as e:
            self.assertNotIn("127.0.0.1", str(e))
            self.assertNotIn("fake-topsecret", str(e))
            self.assertNotIn("Bearer", str(e))
        else:
            self.fail("expected NetError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
