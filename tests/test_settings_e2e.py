"""End-to-end tests for LLM settings.

These tests differ from ``tests/test_settings_http.py`` in one important way:
they exercise the **real generation path** (``/api/generate`` -> ``gen.py`` ->
``netutil`` -> a real stub HTTP server) rather than mocking
``gen.generate_questions``. That is the only way to prove that a console save
actually changes the model of the *next job in the same process*, and that the
redirect / key-egress / bounds rules hold on the generation path too.

Stubs bind on this host's RFC1918 address because the SSRF rule rejects loopback
targets; 127.0.0.1 is deliberately unreachable from ``netutil``.

Behaviour map (each test fails if the behaviour is reverted):

* admin token ............... ``test_all_settings_endpoints_require_admin``
* key egress ................ ``test_authorization_header_egress``
* SSRF block ................ ``test_loopback_base_url_rejected``
* no redirects .............. ``test_redirect_target_never_contacted``
* response bounds ........... ``test_oversized_models_body_rejected`` and the
  two drip-feed / concurrency tests
* key never emitted ......... ``test_key_canary_absent_everywhere``
* resolver, no cache ........ ``test_gen_audit_uses_saved_model_without_restart``
* console panel contract .... ``test_console_panel_static_contract``
"""

import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import gen
import server
import settings

ADMIN_TOKEN = "e2e-admin-" + uuid.uuid4().hex
# Unique per run: a canary scan is only meaningful for a value no test data
# could contain by accident.
KEY = "fake-e2e-canary-" + uuid.uuid4().hex

# Questions hand-checked against gen._validate_question (same set the
# committed harness in tools/llm_settings_verify/stub_llm.py uses).
QUESTIONS = [
    ("Which chemical element has the atomic number 7 and the symbol N?",
     ["Nitrogen", "Oxygen", "Argon", "Neon"], "Nitrogen"),
    ("In which year did the Berlin Wall first begin to be constructed?",
     ["1961", "1953", "1949", "1972"], "1961"),
    ("What is the capital city of the country of Portugal?",
     ["Lisbon", "Porto", "Madrid", "Braga"], "Lisbon"),
    ("Which planet in our solar system is closest in size to the Earth?",
     ["Venus", "Mars", "Jupiter", "Mercury"], "Venus"),
    ("Who composed the orchestral suite known as The Planets?",
     ["Gustav Holst", "Edward Elgar", "Ralph Vaughan Williams", "Benjamin Britten"],
     "Gustav Holst"),
    ("What is the longest river that flows entirely within the continent of Europe?",
     ["The Volga", "The Danube", "The Rhine", "The Loire"], "The Volga"),
    ("What instrument does a timpanist traditionally play in an orchestra?",
     ["Kettledrums", "Xylophone", "Triangle", "Bassoon"], "Kettledrums"),
    ("Which country is home to the ancient archaeological site of Machu Picchu?",
     ["Peru", "Bolivia", "Chile", "Ecuador"], "Peru"),
]

MAX_BYTES = 1 << 20  # netutil.open_no_redirect default used by /api/settings/test


def _local_ip() -> str:
    """This host's RFC1918 address (netutil rejects 127.0.0.0/8)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


class StubLLM:
    """Recording OpenAI-compatible stub.

    ``mode``:
      * ``"ok"``       — normal /models + /chat/completions
      * ``"redirect"`` — 302 (GET) / 307 (POST) to ``redirect_to``
      * ``"big"``      — /models returns ``big_bytes`` of filler
      * ``"drip"``     — /models sends headers then one byte at a time
    """

    def __init__(self, mode="ok", redirect_to=None, big_bytes=0):
        self.mode = mode
        self.redirect_to = redirect_to
        self.big_bytes = big_bytes
        self.requests = []
        self._lock = threading.Lock()

    def record(self, method, path, auth, model):
        with self._lock:
            self.requests.append({"method": method, "path": path,
                                  "auth": auth, "model": model})

    def snapshot(self):
        with self._lock:
            return list(self.requests)

    def auths(self, method=None):
        return [r["auth"] for r in self.snapshot()
                if method is None or r["method"] == method]


def _stub_handler(state: StubLLM):
    class _H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _redirect(self, status):
            self.send_response(status)
            if state.redirect_to:
                self.send_header("Location", state.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _big(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(state.big_bytes))
            self.end_headers()
            chunk = b"x" * 65536
            sent = 0
            try:
                while sent < state.big_bytes:
                    n = min(len(chunk), state.big_bytes - sent)
                    self.wfile.write(chunk[:n])
                    sent += n
            except OSError:
                pass

        def _drip(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "500")
            self.end_headers()
            try:
                for _ in range(24):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.25)
            except OSError:
                pass

        def do_GET(self):
            state.record("GET", self.path, self.headers.get("Authorization"), None)
            if state.mode == "redirect":
                self._redirect(302)
                return
            if state.mode == "big":
                self._big()
                return
            if state.mode == "drip":
                self._drip()
                return
            if self.path.split("?")[0].rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [
                    {"id": "stub-model-a", "object": "model"},
                    {"id": "stub-model-b", "object": "model"},
                ]})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(raw.decode("utf-8"))
            except Exception:
                req = {}
            model = req.get("model")
            state.record("POST", self.path, self.headers.get("Authorization"), model)
            if state.mode == "redirect":
                self._redirect(307)
                return
            if not self.path.split("?")[0].rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": "not found"})
                return
            want = 3
            for msg in req.get("messages") or []:
                m = re.search(r"Write (\d+)", str(msg.get("content") or ""))
                if m:
                    want = int(m.group(1))
            items = []
            for stem, options, answer in QUESTIONS[:max(1, min(want, len(QUESTIONS)))]:
                items.append({"stem": stem, "options": options, "answer": answer,
                              "confidence": "high", "category": "general"})
            self._json(200, {
                "id": "chatcmpl-stub", "object": "chat.completion",
                "model": model or "stub",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": json.dumps(items)}}],
            })

    return _H


class SettingsE2ETest(unittest.TestCase):
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
        self.captured = []      # every raw response body, for the key-leak scan
        self._servers = []

    def tearDown(self):
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass

    # ---- harness -----------------------------------------------------------
    def _start_app(self, **kw):
        app = server.App(self.db_path, **kw)
        httpd = server.make_server(app, "127.0.0.1", 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._servers.append(httpd)
        self.app = app
        return httpd, port

    def _start_stub(self, **kw):
        state = StubLLM(**kw)
        srv = ThreadingHTTPServer(("0.0.0.0", 0), _stub_handler(state))
        srv.daemon_threads = True
        srv.block_on_close = False  # don't join a drip handler at teardown
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)
        return state, srv.server_address[1]

    def _base(self, port):
        return f"http://{self.ip}:{port}/v1"

    def _request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
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
                                     {"name": "E2E Settings"},
                                     headers=self._admin())
        self.assertEqual(status, 201)
        return g

    def _save(self, port, g, fields):
        body = {"game_id": g["game_id"], "token": g["host_token"]}
        body.update(fields)
        return self._request(port, "POST", "/api/settings", body,
                             headers=self._admin())

    def _poll_job(self, port, job_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body, _ = self._request(
                port, "GET", f"/api/generate/status?job_id={job_id}",
                headers=self._admin())
            if isinstance(body, dict) and body.get("done"):
                return body
            time.sleep(0.02)
        self.fail("generation job did not finish in time")

    def _generate(self, port, g, count=3):
        status, body, _ = self._request(
            port, "POST", "/api/generate",
            {"game_id": g["game_id"], "token": g["host_token"],
             "topic": "general knowledge", "count": count})
        self.assertEqual(status, 202, body)
        return body["job_id"]

    def _audit_rows(self, job_id):
        conn = db.connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM gen_audit WHERE job_id = ? ORDER BY id",
                (job_id,)).fetchall()]
        finally:
            conn.close()

    # ---- save then generate, same process --------------
    def test_gen_audit_uses_saved_model_without_restart(self):
        httpd, port = self._start_app()
        pid_before = os.getpid()
        g = self._setup_game(port)
        stub, s_port = self._start_stub()

        new_model = "e2e-model-" + uuid.uuid4().hex[:8]
        status, body, _ = self._save(
            port, g, {"base_url": self._base(s_port), "model": new_model})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["model"], new_model)
        self.assertEqual(body["source"]["model"], "db")

        job_id = self._generate(port, g, count=3)
        done = self._poll_job(port, job_id)
        self.assertFalse(done.get("error"), done)
        self.assertEqual(done.get("accepted"), 3, done)

        rows = self._audit_rows(job_id)
        self.assertTrue(rows)
        self.assertEqual({r["model"] for r in rows}, {new_model},
                         "gen_audit.model must be the newly saved model")
        self.assertEqual(rows[0]["status"], "summary")

        posts = [r for r in stub.snapshot() if r["method"] == "POST"]
        self.assertTrue(posts, "stub saw no generation request")
        self.assertEqual(posts[0]["model"], new_model,
                         "the model in the outbound request body must be the new one")

        # No restart happened: same interpreter, same App, same listener.
        self.assertEqual(os.getpid(), pid_before)
        self.assertIs(server.Handler.app, self.app)
        status, again, _ = self._request(
            port, "GET",
            f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
            headers=self._admin())
        self.assertEqual(status, 200)
        self.assertEqual(again["model"], new_model)
        self.assertEqual(gen.DEFAULT_MODEL in {r["model"] for r in rows}, False,
                         "the default model must never have run")

    # ---- admin gate on every settings endpoint -------------------
    def test_all_settings_endpoints_require_admin(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        creds = {"game_id": g["game_id"], "token": g["host_token"]}
        # No header at all.
        status, body, _ = self._request(
            port, "GET",
            f"/api/settings?game_id={g['game_id']}&token={g['host_token']}")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "admin token rejected")
        for path in ("/api/settings", "/api/settings/test"):
            status, body, _ = self._request(port, "POST", path, creds)
            self.assertEqual(status, 403, path)
            self.assertEqual(body["error"], "admin token rejected", path)
        # Wrong header.
        status, body, _ = self._request(
            port, "POST", "/api/settings/test", creds,
            headers={"X-Admin-Token": "not-the-token"})
        self.assertEqual(status, 403)
        # Correct header + bad host token: 401, not 403 (order is admin first).
        status, body, _ = self._request(
            port, "POST", "/api/settings/test",
            {"game_id": g["game_id"], "token": "wrong"},
            headers=self._admin())
        self.assertEqual(status, 401)

    # ---- key egress rule -----------------------------------------
    def test_authorization_header_egress(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub_a, a_port = self._start_stub()
        stub_b, b_port = self._start_stub()
        creds = {"game_id": g["game_id"], "token": g["host_token"]}

        # The stored key belongs to stub A's base_url.
        status, body, _ = self._save(
            port, g, {"base_url": self._base(a_port), "api_key": KEY})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["api_key_set"])

        # (a) Cross-host draft, no body key -> no Authorization at all.
        status, body, _ = self._request(
            port, "POST", "/api/settings/test",
            {**creds, "base_url": self._base(b_port)}, headers=self._admin())
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertTrue(body.get("key_required_for_new_host"), body)
        got = stub_b.auths()
        self.assertTrue(got, "cross-host stub was never contacted")
        self.assertEqual([a for a in got if a], [],
                         "violated: stored key egressed to a new host")

        # (b) Same host -> the stored key is sent.
        status, body, _ = self._request(
            port, "POST", "/api/settings/test", creds, headers=self._admin())
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertNotIn("key_required_for_new_host", body)
        self.assertIn("Bearer " + KEY, stub_a.auths("GET"))

        # (c) Generation on the same host also carries the stored key.
        job_id = self._generate(port, g, count=3)
        done = self._poll_job(port, job_id)
        self.assertFalse(done.get("error"), done)
        self.assertEqual([a for a in stub_a.auths("POST") if a],
                         ["Bearer " + KEY])

    # ---- SSRF block still applies to settings --------------------
    def test_loopback_base_url_rejected(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        for bad in ("http://127.0.0.1:9/v1", "http://localhost:9/v1",
                    "http://169.254.169.254/latest", "http://[::1]:9/v1"):
            status, body, _ = self._save(port, g, {"base_url": bad})
            self.assertEqual(status, 400, f"{bad} was accepted: {body}")
            joined = " ".join(body.get("errors") or [])
            self.assertIn("base_url", joined)
        # The draft probe path is validated too (not just save).
        status, body, _ = self._request(
            port, "POST", "/api/settings/test",
            {"game_id": g["game_id"], "token": g["host_token"],
             "base_url": "http://127.0.0.1:9/v1"}, headers=self._admin())
        self.assertEqual(status, 400, body)

    # ---- no redirects, on BOTH paths -----------------------------
    def test_redirect_target_never_contacted(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub_b, b_port = self._start_stub()          # the redirect target
        target = self._base(b_port)
        stub_a, a_port = self._start_stub(mode="redirect", redirect_to=target)
        creds = {"game_id": g["game_id"], "token": g["host_token"]}

        status, body, _ = self._save(port, g, {"base_url": self._base(a_port)})
        self.assertEqual(status, 200, body)

        # (a) /api/settings/test: a 302 is reported, never followed.
        status, body, _ = self._request(
            port, "POST", "/api/settings/test", creds, headers=self._admin())
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("ok"), body)
        self.assertIn("redirect", (body.get("error") or ""), body)
        self.assertEqual(body.get("status"), 302, body)

        # (b) generation: a 307 is reported in gen_audit, never followed.
        job_id = self._generate(port, g, count=3)
        done = self._poll_job(port, job_id)
        self.assertEqual(done.get("accepted"), 0, done)
        rows = self._audit_rows(job_id)
        reasons = " ".join(str(r.get("reason") or "") for r in rows)
        self.assertIn("redirect not followed", reasons)
        self.assertIn("307", reasons)

        self.assertEqual(stub_b.snapshot(), [],
                         "violated: the redirect target received requests")

    # ---- bounds ---------------------------------------------------
    def test_oversized_models_body_rejected(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub, s_port = self._start_stub(mode="big", big_bytes=8 * 1024 * 1024)
        self._save(port, g, {"base_url": self._base(s_port)})
        t0 = time.monotonic()
        status, body, _ = self._request(
            port, "POST", "/api/settings/test",
            {"game_id": g["game_id"], "token": g["host_token"]},
            headers=self._admin())
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(body.get("error"), "response too large", body)
        self.assertLess(elapsed, 10.0)
        self.assertGreater(len(stub.snapshot()), 0)

    def test_drip_feed_hits_deadline(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub, s_port = self._start_stub(mode="drip")
        self._save(port, g, {"base_url": self._base(s_port)})
        t0 = time.monotonic()
        status, body, _ = self._request(
            port, "POST", "/api/settings/test",
            {"game_id": g["game_id"], "token": g["host_token"]},
            headers=self._admin())
        elapsed = time.monotonic() - t0
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(body.get("error"), "deadline exceeded", body)
        # The handler's budget is 5.0 s; allow scheduling slack, not more.
        self.assertGreaterEqual(elapsed, 3.0)
        self.assertLess(elapsed, 9.0)

    def test_third_concurrent_probe_gets_503(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub, s_port = self._start_stub(mode="drip")
        self._save(port, g, {"base_url": self._base(s_port)})
        creds = {"game_id": g["game_id"], "token": g["host_token"]}
        results = []

        def probe():
            try:
                results.append(self._request(
                    port, "POST", "/api/settings/test", creds,
                    headers=self._admin())[:2])
            except Exception as e:  # pragma: no cover - surfaces as a failure
                results.append(("exc", repr(e)))

        threads = [threading.Thread(target=probe) for _ in range(2)]
        try:
            for t in threads:
                t.start()
            # Wait until both probes are actually in flight (stub saw 2 GETs),
            # so both permits are held when the third arrives.
            deadline = time.monotonic() + 5.0
            while len(stub.snapshot()) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(len(stub.snapshot()), 2,
                                    "the two probes never reached the stub")
            status, body, _ = self._request(
                port, "POST", "/api/settings/test", creds, headers=self._admin())
            self.assertEqual(status, 503, body)
            self.assertEqual(body.get("error"), "test in progress")
        finally:
            for t in threads:
                t.join(timeout=15)
        self.assertEqual(len(results), 2, results)
        for status, body in results:
            self.assertEqual(status, 200, body)
            self.assertEqual(body.get("error"), "deadline exceeded", body)
        # Normalise the global semaphore so later tests start clean.
        for _ in range(2):
            if not server._TEST_SEM.acquire(blocking=False):
                break
            server._TEST_SEM.release()

    # ---- raw-byte canary -----------------------------------------
    def test_key_canary_absent_everywhere(self):
        httpd, port = self._start_app()
        g = self._setup_game(port)
        stub_a, a_port = self._start_stub()
        stub_b, b_port = self._start_stub()
        creds = {"game_id": g["game_id"], "token": g["host_token"]}

        self._save(port, g, {"base_url": self._base(a_port), "api_key": KEY})
        self._request(port, "GET",
                      f"/api/settings?game_id={g['game_id']}&token={g['host_token']}",
                      headers=self._admin())
        # same-host probe (key egresses to the stub), cross-host probe (it must not)
        self._request(port, "POST", "/api/settings/test", creds,
                      headers=self._admin())
        self._request(port, "POST", "/api/settings/test",
                      {**creds, "base_url": self._base(b_port)},
                      headers=self._admin())
        job_id = self._generate(port, g, count=3)
        done = self._poll_job(port, job_id)
        self.assertFalse(done.get("error"), done)
        # A host action writes an action_log row (the other key-leak scan target).
        status, act_body, _ = self._request(
            port, "POST", "/api/action",
            {"game_id": g["game_id"], "token": g["host_token"],
             "action": "start", "params": {}})
        self.assertEqual(status, 200, act_body)

        canary = KEY.encode()
        for raw in self.captured:
            self.assertNotIn(canary, raw,
                             f"API key leaked in an HTTP response: {raw[:400]!r}")

        def scan_rows(table):
            conn = db.connect(self.db_path)
            try:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            finally:
                conn.close()
            self.assertTrue(rows, f"{table} should not be empty")
            for row in rows:
                blob = json.dumps(row, default=str)
                self.assertNotIn(KEY, blob,
                                 f"API key leaked in {table} id={row.get('id')}")
            return rows

        scan_rows("gen_audit")
        scan_rows("action_log")

    # ---- static contract (file + served bytes identical) --------------
    def test_console_panel_static_contract(self):
        html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "console.html")
        with open(html_path, "r", encoding="utf-8") as fh:
            text = fh.read()

        ids = ["llm-settings", "llm-admin-token", "llm-provider", "llm-base",
               "llm-model", "llm-model-list", "llm-key", "llm-clear-key",
               "llm-status", "llm-detect", "llm-test", "llm-save"]
        for i in ids:
            self.assertIn(f'id="{i}"', text, f"missing panel id {i}")

        # Secrets are never seeded into markup.
        for input_id in ("llm-key", "llm-admin-token"):
            tag = re.search(rf'<input[^>]*id="{input_id}"[^>]*>', text)
            self.assertIsNotNone(tag, input_id)
            self.assertNotRegex(tag.group(0), r'\bvalue\s*=',
                                f"{input_id} must not carry a value attribute")

        # The admin token / key never touch web storage.
        self.assertNotRegex(text, r'sessionStorage\s*\.',
                            "the panel must not use sessionStorage")
        self.assertNotRegex(text, r'localStorage\s*\.\s*setItem\([^)]*llm',
                            "LLM values must not be written to localStorage")
        self.assertNotRegex(text, r'[?&](admin_token|api_key|llm_key)=',
                            "secrets must never be placed in a URL")
        self.assertNotRegex(text, r'data-[a-z-]*\s*=\s*["\'][^"\']*llm[Aa]dmin',
                            "admin token must not be embedded in a data-* attribute")

        # Remote model strings are inserted as nodes, never as HTML.
        self.assertNotRegex(text, r'innerHTML\s*=\s*[^;]*(models|model_)',
                            "remote model strings must not reach innerHTML")
        self.assertIn("createElement(\"option\")", text)
        self.assertIn("replaceChildren", text)

        # The served page is byte-identical to the file checked above.
        httpd, port = self._start_app()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/static/console.html")
            resp = conn.getresponse()
            served = resp.read()
            self.assertEqual(resp.status, 200)
        finally:
            conn.close()
        self.assertEqual(served, text.encode("utf-8"),
                         "served console.html differs from the checked file")
        self.assertNotIn(ADMIN_TOKEN.encode(), served)


if __name__ == "__main__":
    unittest.main(verbosity=2)
