"""Hardening for public web access.

Each test pins one of the issues found in a security review of the web surface:
  * rejoining a team needs its PIN, not just the id the join page lists
  * team cap per game; award only to a team in the game
  * negative Content-Length no longer bypasses MAX_BODY
  * undo of an answered ad-lib / added question no longer fails the FK
  * uncaught handler errors return 500 instead of a dropped connection
  * tokens accepted from the X-Token header
  * generated questions keep only gate-checked fields; the corrective retry
    carries the topic and failing question; the request caps output tokens
  * page escaping covers quotes (attribute contexts)
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine
import gen
import server

ADMIN_TOKEN = "hardening-test-admin-token-123456"
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class EngineHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self.e = engine.GameEngine(self.db_path)
        g = self.e.create_game("G")
        self.gid, self.host = g["game_id"], g["host_token"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejoin_requires_pin(self):
        t = self.e.join_team(self.gid, "Alpha")
        self.assertRegex(t["rejoin_pin"], r"^\d{6}$")
        for bad in (None, "", "000000" if t["rejoin_pin"] != "000000" else "111111"):
            with self.assertRaisesRegex(ValueError, "team or PIN not recognised"):
                self.e.join_team(self.gid, None, team_id=t["team_id"], pin=bad)
        again = self.e.join_team(self.gid, None, team_id=t["team_id"], pin=t["rejoin_pin"])
        self.assertEqual(again["join_token"], t["join_token"])
        self.assertTrue(again["is_rejoin"])

    def test_unknown_team_gives_same_error_as_wrong_pin(self):
        with self.assertRaisesRegex(ValueError, "team or PIN not recognised"):
            self.e.join_team(self.gid, None, team_id=9999, pin="123456")

    def test_team_without_pin_cannot_rejoin(self):
        # Teams created before the column existed have a NULL PIN.
        t = self.e.join_team(self.gid, "Legacy")
        conn = db.connect(self.db_path)
        conn.execute("UPDATE teams SET rejoin_pin = NULL WHERE id = ?", (t["team_id"],))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            self.e.join_team(self.gid, None, team_id=t["team_id"], pin="")

    def test_team_cap(self):
        for i in range(engine.MAX_TEAMS):
            self.e.join_team(self.gid, f"T{i}")
        with self.assertRaisesRegex(ValueError, "full"):
            self.e.join_team(self.gid, "one too many")

    def test_pins_only_visible_to_host_and_own_team(self):
        a = self.e.join_team(self.gid, "Alpha")
        b = self.e.join_team(self.gid, "Beta")
        host = self.e.snapshot(self.gid, "host", self.host)
        self.assertEqual({t["rejoin_pin"] for t in host["teams"]},
                         {a["rejoin_pin"], b["rejoin_pin"]})
        team = self.e.snapshot(self.gid, "team", a["join_token"])
        self.assertEqual(team["my_pin"], a["rejoin_pin"])
        self.assertTrue(all("rejoin_pin" not in t for t in team["teams"]))
        stage = self.e.snapshot(self.gid, "stage", self.host)
        self.assertTrue(all("rejoin_pin" not in t for t in stage["teams"]))
        self.assertTrue(all("rejoin_pin" not in t for t in self.e.list_teams(self.gid)))

    def test_award_rejects_team_from_another_game(self):
        other = self.e.create_game("Other")
        stranger = self.e.join_team(other["game_id"], "Stranger")
        self.assertEqual(self.e.award(self.gid, stranger["team_id"], 5), "no such team")

    def _answered_adlib(self):
        t = self.e.join_team(self.gid, "Alpha")
        self.assertIsNone(self.e.adlib(self.gid, "Two plus two?", "4", ["3", "4", "5", "6"]))
        r = self.e.submit_answer(t["team_id"], self.gid, "4")
        self.assertTrue(r["ok"])
        return t

    def test_undo_adlib_after_an_answer(self):
        self._answered_adlib()
        self.assertIsNone(self.e.undo(self.gid))
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        conn.close()

    def test_undo_add_question_after_an_answer(self):
        t = self.e.join_team(self.gid, "Alpha")
        self.assertIsNone(self.e.add_question(self.gid, {
            "stem": "Capital of France?", "answer": "Paris",
            "options": ["Paris", "Rome", "Oslo", "Bern"], "type": "mc"}))
        self.assertIsNone(self.e.start_game(self.gid))
        self.assertTrue(self.e.submit_answer(t["team_id"], self.gid, "Paris")["ok"])
        self.assertIsNone(self.e.undo(self.gid))   # undo start
        self.assertIsNone(self.e.undo(self.gid))   # undo add_question
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        conn.close()


class HttpHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self._saved_admin = os.environ.get("TRIVIA_ADMIN_TOKEN")
        os.environ["TRIVIA_ADMIN_TOKEN"] = ADMIN_TOKEN
        self.app = server.App(self.db_path)
        self.httpd = server.make_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._saved_admin is None:
            os.environ.pop("TRIVIA_ADMIN_TOKEN", None)
        else:
            os.environ["TRIVIA_ADMIN_TOKEN"] = self._saved_admin
        self.tmp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"null")
        finally:
            conn.close()

    def _game(self):
        status, g = self._request("POST", "/api/game/create", {"name": "G"},
                                  headers={"X-Admin-Token": ADMIN_TOKEN})
        self.assertEqual(status, 201)
        return g

    def test_team_list_then_rejoin_without_pin_fails(self):
        g = self._game()
        _, victim = self._request("POST", "/api/game/join",
                                  {"code": g["code"], "team_name": "Victims"})
        _, listing = self._request("POST", "/api/game/teams", {"code": g["code"]})
        self.assertNotIn("rejoin_pin", listing["teams"][0])
        tid = listing["teams"][0]["id"]
        status, res = self._request("POST", "/api/game/join",
                                    {"code": g["code"], "team_id": tid})
        self.assertEqual(status, 400)
        self.assertNotIn("join_token", res)
        status, res = self._request("POST", "/api/game/join",
                                    {"code": g["code"], "team_id": tid,
                                     "pin": victim["rejoin_pin"]})
        self.assertEqual(status, 200)
        self.assertEqual(res["join_token"], victim["join_token"])

    def test_team_listing_is_rate_limited(self):
        app = server.App(self.db_path, join_rate_limit=3)
        httpd = server.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            port, codes = httpd.server_address[1], []
            for _ in range(4):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("POST", "/api/game/teams", body=b'{"code":"ZZZZ"}',
                             headers={"Content-Type": "application/json"})
                codes.append(conn.getresponse().status)
                conn.close()
            self.assertEqual(codes, [404, 404, 404, 429])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_negative_content_length_is_not_read(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /api/game/teams HTTP/1.1\r\nHost: x\r\n"
                      b"Content-Length: -1\r\nContent-Type: application/json\r\n\r\n"
                      b'{"code":"ZZZZ"}')
            reply = s.recv(4096)
            # Answered at once (body treated as missing), not after reading to EOF.
            self.assertIn(b"HTTP/1.1 404", reply.split(b"\r\n")[0])
        finally:
            s.close()

    def test_handler_has_socket_timeout(self):
        self.assertTrue(server.Handler.timeout and server.Handler.timeout <= 60)

    def test_uncaught_error_returns_500(self):
        g = self._game()
        with mock.patch.object(self.app.engine, "undo", side_effect=RuntimeError("boom")):
            status, res = self._request("POST", "/api/action",
                                        {"game_id": g["game_id"], "token": g["host_token"],
                                         "action": "undo"})
        self.assertEqual(status, 500)
        self.assertEqual(res["error"], "internal error")

    def test_state_accepts_token_header(self):
        g = self._game()
        status, s = self._request("GET", f"/api/state?game_id={g['game_id']}&role=host",
                                  headers={"X-Token": g["host_token"]})
        self.assertEqual(status, 200)
        self.assertTrue(s["host_token_ok"])
        status, _ = self._request("GET", f"/api/state?game_id={g['game_id']}&role=host",
                                  headers={"X-Token": "wrong"})
        self.assertEqual(status, 401)


class ClientIpTest(unittest.TestCase):
    """Rate-limit key behind NPM and the Cloudflare tunnel."""

    def _ip(self, trusted, direct, xff=None):
        app = server.App.__new__(server.App)
        app.trusted_proxies = trusted
        app._proxy_nets = server._proxy_networks(trusted)
        h = server.Handler.__new__(server.Handler)
        h.app, h.client_address = app, (direct, 0)
        h.headers = {"X-Forwarded-For": xff} if xff is not None else {}
        return h._client_ip()

    TRUSTED = {"192.168.1.10", "172.16.0.0/12"}

    def test_tunnel_chain_keys_on_the_internet_client(self):
        self.assertEqual(self._ip(self.TRUSTED, "192.168.1.10",
                                  "203.0.113.7, 172.16.12.1"), "203.0.113.7")

    def test_spoofed_entries_left_of_the_client_are_ignored(self):
        self.assertEqual(self._ip(self.TRUSTED, "192.168.1.10",
                                  "1.1.1.1, 203.0.113.7, 172.16.12.1"), "203.0.113.7")

    def test_lan_phone_through_npm(self):
        self.assertEqual(self._ip(self.TRUSTED, "192.168.1.10", "192.168.1.55"),
                         "192.168.1.55")

    def test_untrusted_direct_peer_ignores_xff(self):
        self.assertEqual(self._ip(self.TRUSTED, "192.168.1.55", "203.0.113.7"),
                         "192.168.1.55")

    def test_all_hops_trusted_falls_back_to_leftmost(self):
        self.assertEqual(self._ip(self.TRUSTED, "192.168.1.10", "172.16.3.2"),
                         "172.16.3.2")


class GenHardeningTest(unittest.TestCase):
    def setUp(self):
        self._orig_chat = gen._chat

    def tearDown(self):
        gen._chat = self._orig_chat

    @staticmethod
    def _q(stem, **extra):
        return {"stem": stem, "options": ["Paris", "Rome", "Oslo", "Bern"],
                "answer": "Paris", "confidence": "high", **extra}

    def test_accepted_questions_drop_unchecked_fields(self):
        evil = self._q("Which city is the capital of France?",
                       media_type="image", media_url='x" onerror="alert(1)',
                       type="text", points=10 ** 9)
        gen._chat = lambda *a, **k: json.dumps([evil])
        out = gen.generate_questions("capitals", 1)
        self.assertEqual(out["accepted"], [{
            "stem": evil["stem"], "options": evil["options"], "answer": "Paris",
            "confidence": "high", "type": "mc", "source": "generated"}])

    def test_corrective_retry_carries_topic_and_question(self):
        seen = []
        bad = self._q("Paris is the capital of which country, France?")  # leaks answer

        def fake(messages, *a, **k):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:
                return json.dumps([bad])
            return json.dumps(self._q("Which city is the capital of France?"))
        gen._chat = fake
        out = gen.generate_questions("European capitals", 1)
        self.assertEqual(len(out["accepted"]), 1)
        self.assertIn("European capitals", seen[1])
        self.assertIn(bad["stem"], seen[1])

    def test_request_caps_tokens_and_reasoning(self):
        sent = {}

        def fake_open(url, *, data=None, headers=None, timeout=5.0, max_bytes=0):
            sent.update(json.loads(data))
            return 200, json.dumps({"choices": [{"message": {"content": "[]"}}]}).encode()
        with mock.patch("netutil.open_no_redirect", side_effect=fake_open):
            gen._chat([{"role": "user", "content": "hi"}], "m", "http://192.168.1.50/v1",
                      None, 10, 777)
        self.assertEqual(sent["max_tokens"], 777)
        self.assertEqual(sent["reasoning_effort"], "none")

    def test_job_respects_wall_clock_budget(self):
        calls = []
        gen._chat = lambda *a, **k: calls.append(1) or "[]"
        out = gen.generate_questions("anything", 5, max_seconds=0)
        self.assertEqual(calls, [])
        self.assertEqual(out["shortfall"], 5)


class PageEscapingTest(unittest.TestCase):
    def test_every_page_escapes_quotes(self):
        for page in ("console.html", "stage.html", "play.html", "join.html"):
            with open(os.path.join(STATIC, page), encoding="utf-8") as f:
                src = f.read()
            self.assertIn('.replace(/"/g,"&quot;")', src, page)
            self.assertNotIn("d.textContent", src, page)

    def test_pages_send_tokens_in_a_header(self):
        for page in ("console.html", "stage.html", "play.html"):
            with open(os.path.join(STATIC, page), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("&token=", src, page)


if __name__ == "__main__":
    unittest.main()
