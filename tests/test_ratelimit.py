"""Rate-limit regression: join/answer buckets are split, ``X-Forwarded-For``
is trusted only from an explicitly configured proxy, and a 429 carries an
``error`` field the phone surfaces.

Spins a real server with tiny budgets so the test can trip them quickly, creates
a game + team, then hammers the answer endpoint past its limit and asserts the
phone-visible ``error`` key. Also proves the join and answer buckets are
independent, and that ``X-Forwarded-For`` only splits clients when the direct
peer is a configured trusted proxy.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import server

ADMIN_TOKEN = "ratelimit-test-admin-token-123456"


class RateLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(cls.db_path)
        cls._saved_admin = os.environ.get("TRIVIA_ADMIN_TOKEN")
        os.environ["TRIVIA_ADMIN_TOKEN"] = ADMIN_TOKEN

    @classmethod
    def tearDownClass(cls):
        if cls._saved_admin is None:
            os.environ.pop("TRIVIA_ADMIN_TOKEN", None)
        else:
            os.environ["TRIVIA_ADMIN_TOKEN"] = cls._saved_admin
        cls.tmp.cleanup()

    def _start(self, **kw):
        app = server.App(self.db_path, join_rate_limit=2, answer_rate_limit=2, **kw)
        httpd = server.make_server(app, "127.0.0.1", 0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, port, t

    def _request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", "replace")
            return resp.status, parsed
        finally:
            conn.close()

    def _setup_game(self, port):
        _, g = self._request(port, "POST", "/api/game/create", {"name": "Rate Test"},
                             headers={"X-Admin-Token": ADMIN_TOKEN})
        _, team = self._request(port, "POST", "/api/game/join",
                                {"code": g["code"], "team_name": "Hammers"})
        return g, team

    def test_answer_limit_then_429_with_error_field(self):
        httpd, port, t = self._start()
        try:
            _, team = self._setup_game(port)
            token = team["join_token"]
            for _ in range(2):
                status, body = self._request(
                    port, "POST", "/api/answer",
                    {"join_token": token, "answer_text": "x"})
                self.assertEqual(status, 200, body)
            # third answer trips the bucket -> 429 carrying a phone-visible error
            status, body = self._request(
                port, "POST", "/api/answer",
                {"join_token": token, "answer_text": "x"})
            self.assertEqual(status, 429, body)
            self.assertIn("error", body)
            self.assertTrue(body["error"])
        finally:
            httpd.shutdown(); httpd.server_close(); t.join(timeout=5)

    def test_join_and_answer_buckets_are_independent(self):
        httpd, port, t = self._start()
        try:
            g, team = self._setup_game(port)
            token = team["join_token"]
            # second join fills the join bucket (limit=2)
            status, _ = self._request(port, "POST", "/api/game/join",
                                      {"code": g["code"], "team_name": "Extra"})
            self.assertEqual(status, 200)
            # third join trips the join bucket
            status, _ = self._request(port, "POST", "/api/game/join",
                                      {"code": g["code"], "team_name": "Blocked"})
            self.assertEqual(status, 429)
            # ...but the answer bucket is untouched: two answers still allowed
            for _ in range(2):
                status, body = self._request(
                    port, "POST", "/api/answer",
                    {"join_token": token, "answer_text": "x"})
                self.assertEqual(status, 200, body)
        finally:
            httpd.shutdown(); httpd.server_close(); t.join(timeout=5)

    def test_xff_trusted_only_from_configured_proxy(self):
        # trusted proxy is 127.0.0.1 (this test's direct peer) -> XFF honoured
        httpd, port, t = self._start(trusted_proxies={"127.0.0.1"})
        try:
            _, team = self._setup_game(port)
            token = team["join_token"]
            for _ in range(2):
                status, _ = self._request(
                    port, "POST", "/api/answer",
                    {"join_token": token, "answer_text": "x"},
                    headers={"X-Forwarded-For": "1.2.3.4"})
                self.assertEqual(status, 200)
            status, _ = self._request(
                port, "POST", "/api/answer",
                {"join_token": token, "answer_text": "x"},
                headers={"X-Forwarded-For": "1.2.3.4"})
            self.assertEqual(status, 429)
            # a different client IP gets a fresh bucket behind the same proxy
            status, _ = self._request(
                port, "POST", "/api/answer",
                {"join_token": token, "answer_text": "x"},
                headers={"X-Forwarded-For": "5.6.7.8"})
            self.assertEqual(status, 200)
        finally:
            httpd.shutdown(); httpd.server_close(); t.join(timeout=5)

    def test_xff_ignored_when_no_trusted_proxy(self):
        # no trusted proxy -> XFF ignored; all requests share the direct-peer bucket
        httpd, port, t = self._start()
        try:
            _, team = self._setup_game(port)
            token = team["join_token"]
            for ip in ("1.2.3.4", "5.6.7.8"):
                status, _ = self._request(
                    port, "POST", "/api/answer",
                    {"join_token": token, "answer_text": "x"},
                    headers={"X-Forwarded-For": ip})
                self.assertEqual(status, 200)
            # third request, different spoofed XFF, still 429 -> XFF not the key
            status, body = self._request(
                port, "POST", "/api/answer",
                {"join_token": token, "answer_text": "x"},
                headers={"X-Forwarded-For": "9.9.9.9"})
            self.assertEqual(status, 429)
            self.assertIn("error", body)
        finally:
            httpd.shutdown(); httpd.server_close(); t.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
