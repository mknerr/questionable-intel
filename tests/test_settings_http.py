"""End-to-end settings HTTP tests: an in-process server on 127.0.0.1:0 with a
temp data dir and an admin token set. The LLM stub is served on the machine's non-loopback address
(open_no_redirect rejects loopback), so ``/api/settings/test`` can reach it.
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import gen
import server
import settings

ADMIN_TOKEN = "test-admin-token-123456789"
KEY = "fake-provider-key-12345"


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _make_stub_handler(state):
    class _StubLLM(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            state["auths"].append(self.headers.get("Authorization"))
            body = json.dumps({"object": "list", "data": [
                {"id": "stub-model-a", "object": "model"},
                {"id": "stub-model-b", "object": "model"},
            ]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _StubLLM


def _start_stub():
    state = {"auths": []}
    srv = ThreadingHTTPServer(("0.0.0.0", 0), _make_stub_handler(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port, t, state


class SettingsHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(cls.db_path)
        cls._saved_admin = os.environ.get("TRIVIA_ADMIN_TOKEN")
        os.environ["TRIVIA_ADMIN_TOKEN"] = ADMIN_TOKEN
        cls.ip = _local_ip()

    @classmethod
    def tearDownClass(cls):
        if cls._saved_admin is None:
            os.environ.pop("TRIVIA_ADMIN_TOKEN", None)
        else:
            os.environ["TRIVIA_ADMIN_TOKEN"] = cls._saved_admin
        cls.tmp.cleanup()

    def setUp(self):
        self.captured = []  # raw response bodies, for the key-leak byte scan
        self._servers = []

    def tearDown(self):
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:
                pass
            srv.server_close()

    # ---- helpers -----------------------------------------------------------
    def _start_app(self, **kw):
        app = server.App(self.db_path, **kw)
        httpd = server.make_server(app, "127.0.0.1", 0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self._servers.append(httpd)
        return httpd, port, t

    def _request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            self.captured.append(raw)
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", "replace")
            return resp.status, parsed, raw
        finally:
            conn.close()

    def _admin(self, **extra):
        return {"X-Admin-Token": ADMIN_TOKEN, **extra}

    def _setup_game(self, port):
        status, g, _ = self._request(port, "POST", "/api/game/create",
                                     {"name": "Settings Test"},
                                     headers=self._admin())
        self.assertEqual(status, 201)
        return g

    def _save(self, port, g, fields):
        body = {"game_id": g["game_id"], "token": g["host_token"]}
        body.update(fields)
        return self._request(port, "POST", "/api/settings", body,
                             headers=self._admin())

    def _poll_job(self, port, job_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body, _ = self._request(
                port, "GET", f"/api/generate/status?job_id={job_id}",
                headers=self._admin())
            if body and body.get("done"):
                return body
            time.sleep(0.05)
        self.fail("generation job did not finish in time")

    # ---- admin token ----------------------------------------------
    def test_locked_when_admin_token_unset(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        with mock.patch.dict(os.environ, {"TRIVIA_ADMIN_TOKEN": ""}):
            status, body, _ = self._request(
                port, "GET", f"/api/settings?game_id={g['game_id']}&token={g['host_token']}")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"],
                         "management locked: set TRIVIA_ADMIN_TOKEN (at least 16 characters)")

    def test_wrong_admin_token_403(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        status, body, _ = self._request(
            port, "GET",
            f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
            headers={"X-Admin-Token": "wrong-token"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "admin token rejected")

    def test_valid_admin_invalid_host_token_401(self):
        httpd, port, _ = self._start_app()
        self._setup_game(port)
        status, body, _ = self._request(
            port, "GET", "/api/settings?game_id=1&token=not-a-real-host-token",
            headers=self._admin())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "not authorized")

    # ---- the key never appears in any response -------------
    def test_get_never_contains_key(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        self._save(port, g, {"api_key": KEY})
        status, body, raw = self._request(
            port, "GET",
            f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
            headers=self._admin())
        self.assertEqual(status, 200)
        self.assertTrue(body["api_key_set"])
        self.assertNotIn("api_key", body)
        self.assertNotIn(KEY.encode(), raw)

    # ---- save persists, next generation uses the new model ----------
    def test_post_persists_next_generation_uses_new_model(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        status, body, _ = self._save(port, g, {"model": "new-model-xyz"})
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "new-model-xyz")

        recorded = {}

        def fake(topic, count, **kwargs):
            recorded.update(kwargs)
            return {"accepted": [], "rejected": [], "audit": []}

        with mock.patch.object(gen, "generate_questions", fake):
            status, body, _ = self._request(
                port, "POST", "/api/generate",
                {"game_id": g["game_id"], "token": g["host_token"],
                 "topic": "science", "count": 3})
            self.assertEqual(status, 202)
            self._poll_job(port, body["job_id"])

        self.assertEqual(recorded.get("model"), "new-model-xyz")

    # ---- key egress on /api/settings/test --------------------------
    def test_cross_host_test_sends_no_authorization(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        stub_a, a_port, _, state_a = _start_stub()
        stub_b, b_port, _, state_b = _start_stub()
        self._servers.append(stub_a)
        self._servers.append(stub_b)
        try:
            # Resolve to stub A (same host the key is "for"); test against stub B.
            base_a = f"http://{self.ip}:{a_port}/v1"
            base_b = f"http://{self.ip}:{b_port}/v1"
            self._save(port, g, {"base_url": base_a, "api_key": KEY})
            status, body, _ = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"], "base_url": base_b},
                headers=self._admin())
            self.assertEqual(status, 200, body)
            self.assertIn("key_required_for_new_host", body)
            self.assertTrue(body["key_required_for_new_host"])
            # stub B must have seen NO Authorization header.
            self.assertEqual([a for a in state_b["auths"] if a], [])
            self.assertTrue(state_b["auths"], "stub B should have been contacted")
        finally:
            stub_a.shutdown(); stub_a.server_close()
            stub_b.shutdown(); stub_b.server_close()

    def test_same_host_test_uses_stored_key(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        stub, s_port, _, state = _start_stub()
        self._servers.append(stub)
        try:
            base = f"http://{self.ip}:{s_port}/v1"
            self._save(port, g, {"base_url": base, "api_key": KEY})
            status, body, _ = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"]},
                headers=self._admin())
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)
            self.assertNotIn("key_required_for_new_host", body)
            self.assertIn(f"Bearer {KEY}", state["auths"])
        finally:
            stub.shutdown(); stub.server_close()

    # ---- test bounds -------------------------------------------------
    def test_test_persists_nothing(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)

        def _count():
            conn = db.connect(self.db_path)
            try:
                return conn.execute(
                    "SELECT COUNT(*) AS n FROM app_settings").fetchone()["n"]
            finally:
                conn.close()

        before = _count()
        stub, s_port, _, _ = _start_stub()
        self._servers.append(stub)
        try:
            base = f"http://{self.ip}:{s_port}/v1"
            status, body, _ = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"],
                 "base_url": base, "api_key": "fake-draft-only"},
                headers=self._admin())
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ok"], body)
            # Nothing persisted: the row count is unchanged by the test probe.
            self.assertEqual(_count(), before)
        finally:
            stub.shutdown(); stub.server_close()

    def test_503_when_test_semaphore_busy(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        # Hold both permits so the handler's non-blocking acquire fails.
        self.assertTrue(server._TEST_SEM.acquire(blocking=False))
        self.assertTrue(server._TEST_SEM.acquire(blocking=False))
        try:
            status, body, _ = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"]},
                headers=self._admin())
            self.assertEqual(status, 503)
            self.assertEqual(body["error"], "test in progress")
        finally:
            server._TEST_SEM.release()
            server._TEST_SEM.release()

    def test_429_rate_limited(self):
        httpd, port, _ = self._start_app(settings_rate_limit=1)
        g = self._setup_game(port)
        # First request consumes the single-per-minute budget (may or may not
        # reach a stub; the limiter is checked before any outbound call).
        stub, s_port, _, _ = _start_stub()
        self._servers.append(stub)
        try:
            base = f"http://{self.ip}:{s_port}/v1"
            first = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"], "base_url": base},
                headers=self._admin())
            self.assertIn(first[0], (200, 400, 500))  # passes the limiter
            status, body, _ = self._request(
                port, "POST", "/api/settings/test",
                {"game_id": g["game_id"], "token": g["host_token"], "base_url": base},
                headers=self._admin())
            self.assertEqual(status, 429)
            self.assertIn("error", body)
        finally:
            stub.shutdown(); stub.server_close()

    # ---- clear_api_key + both-together -----------------------------
    def test_clear_api_key(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        self._save(port, g, {"api_key": KEY})
        status, body, _ = self._request(
            port, "GET",
            f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
            headers=self._admin())
        self.assertTrue(body["api_key_set"])

        status, body, _ = self._request(
            port, "POST", "/api/settings",
            {"game_id": g["game_id"], "token": g["host_token"], "clear_api_key": True},
            headers=self._admin())
        self.assertEqual(status, 200)
        self.assertFalse(body["api_key_set"])

    def test_empty_api_key_is_unchanged(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        self._save(port, g, {"api_key": KEY})
        # api_key: "" means "unchanged", not "clear".
        status, body, _ = self._save(port, g, {"api_key": ""})
        self.assertEqual(status, 200)
        self.assertTrue(body["api_key_set"])

    def test_api_key_and_clear_together_400(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        status, body, _ = self._request(
            port, "POST", "/api/settings",
            {"game_id": g["game_id"], "token": g["host_token"],
             "api_key": KEY, "clear_api_key": True},
            headers=self._admin())
        self.assertEqual(status, 400)

    def test_unknown_key_400(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        status, body, _ = self._request(
            port, "POST", "/api/settings",
            {"game_id": g["game_id"], "token": g["host_token"], "nonsense": "x"},
            headers=self._admin())
        self.assertEqual(status, 400)
        self.assertIn("errors", body)

    # ---- raw-byte scan across a representative flow -----------------
    def test_raw_response_bytes_never_contain_key(self):
        httpd, port, _ = self._start_app()
        g = self._setup_game(port)
        stub_a, a_port, _, _ = _start_stub()
        stub_b, b_port, _, _ = _start_stub()
        self._servers.append(stub_a)
        self._servers.append(stub_b)
        try:
            base_a = f"http://{self.ip}:{a_port}/v1"
            base_b = f"http://{self.ip}:{b_port}/v1"
            # save, get, same-host test, cross-host test
            self._save(port, g, {"base_url": base_a, "api_key": KEY})
            self._request(port, "GET",
                          f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
                          headers=self._admin())
            self._request(port, "POST", "/api/settings/test",
                          {"game_id": g["game_id"], "token": g["host_token"]},
                          headers=self._admin())
            self._request(port, "POST", "/api/settings/test",
                          {"game_id": g["game_id"], "token": g["host_token"],
                           "base_url": base_b},
                          headers=self._admin())
        finally:
            stub_a.shutdown(); stub_a.server_close()
            stub_b.shutdown(); stub_b.server_close()

        for raw in self.captured:
            self.assertNotIn(KEY.encode(), raw,
                             f"API key leaked in a response body: {raw!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
