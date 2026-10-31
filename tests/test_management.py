"""Management APIs, round library, async configuration and access controls.

Covers the management/auth design end to end against an in-process
``ThreadingHTTPServer`` with a temp DB and a stub LLM (``mock`` on
``gen.generate_questions`` — no live model, no network beyond loopback).

Sections:
  * migrations (duplicate-safe unique index, library tables)
  * locked / unauthorized / authorized paths for every management endpoint
  * room-code / team-token / admin-as-contestant rejection
  * game list / select (token rotation) / delete (cascade) / library retention
  * config validation (bounds, unknown keys, unknown library ids)
  * deterministic delayed generation: round 1 ready while round 2 pending, and
    next_round is safe (clear error, state untouched, never skips)
  * mixed generated/library reuse with deep-copy behaviour
  * deletion during generation (no rows recreated)
  * restart / interrupted status (never phantom-ready)
  * QR public origin (TRIVIA_PUBLIC_BASE_URL) + room-code bound
"""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine
import gen
import server

ADMIN_TOKEN = "management-test-admin-token-123456"


def _question(topic, i):
    return {"stem": f"{topic} question number {i}", "type": "mc",
            "options": ["alpha", "bravo", "charlie", "delta"],
            "answer": "alpha", "points": 10, "confidence": "high"}


def _instant_stub(topic_map=None):
    """A stub ``generate_questions`` that returns ``count`` deterministic
    questions for any topic, immediately."""
    def fake(topic, count, **kw):
        return {"accepted": [_question(topic, i) for i in range(count)],
                "rejected": [], "audit": []}
    return fake


class ManagementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(cls.db_path)
        cls._saved_admin = os.environ.get("TRIVIA_ADMIN_TOKEN")
        os.environ["TRIVIA_ADMIN_TOKEN"] = ADMIN_TOKEN
        # DeepSeek is the default provider and needs a key before the server
        # will start a generation; the stubbed generator never uses it.
        cls._key_patch = mock.patch.dict(os.environ, {"TRIVIA_LLM_API_KEY": "stub-key"})
        cls._key_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._key_patch.stop()
        if cls._saved_admin is None:
            os.environ.pop("TRIVIA_ADMIN_TOKEN", None)
        else:
            os.environ["TRIVIA_ADMIN_TOKEN"] = cls._saved_admin
        cls.tmp.cleanup()

    def setUp(self):
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
        return httpd, port, app

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
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", "replace")
            return resp.status, parsed
        finally:
            conn.close()

    def _request_raw(self, port, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, headers=dict(headers or {}))
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _admin(self, **extra):
        return {"X-Admin-Token": ADMIN_TOKEN, **extra}

    def _create(self, port, name="G"):
        status, g = self._request(port, "POST", "/api/game/create",
                                  {"name": name}, headers=self._admin())
        self.assertEqual(status, 201, g)
        return g

    def _state(self, port, g, role="host", token=None):
        tok = token if token is not None else g["host_token"]
        status, s = self._request(
            port, "GET",
            f"/api/state?game_id={g['game_id']}&role={role}&token={tok}")
        return status, s

    def _library(self, port):
        status, b = self._request(port, "GET", "/api/library/rounds",
                                  headers=self._admin())
        self.assertEqual(status, 200, b)
        return b["rounds"]

    def _host_action(self, port, g, action, params=None):
        status, body = self._request(
            port, "POST", "/api/action",
            {"game_id": g["game_id"], "token": g["host_token"],
             "action": action, "params": params or {}})
        self.assertEqual(status, 200, (action, body))
        return body

    def _poll_state(self, port, g, pred, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, s = self._state(port, g)
            if status == 200 and pred(s):
                return s
            time.sleep(0.05)
        self.fail("state predicate not met within timeout")

    # ---- migrations --------------------------------------------------------
    def test_migration_dedup_unique_index_and_columns(self):
        # Simulate a pre-feature DB: old rounds table (no new columns), no
        # unique index, and a duplicate (game_id, position) pair.
        path = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, code TEXT NOT NULL UNIQUE, host_token_hash TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'lobby', current_round_id INTEGER, "
            "current_question_id INTEGER, clock_ends_at REAL, "
            "clock_duration INTEGER NOT NULL DEFAULT 30, created_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "game_id INTEGER NOT NULL REFERENCES games(id), position INTEGER NOT NULL, title TEXT)")
        conn.execute(
            "CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "game_id INTEGER NOT NULL REFERENCES games(id), round_id INTEGER REFERENCES rounds(id), "
            "position INTEGER NOT NULL, type TEXT NOT NULL DEFAULT 'mc', stem TEXT NOT NULL, "
            "options TEXT, answer TEXT NOT NULL, points INTEGER NOT NULL DEFAULT 10, "
            "media_type TEXT NOT NULL DEFAULT 'none', media_url TEXT, "
            "source TEXT NOT NULL DEFAULT 'generated', confidence TEXT, "
            "status TEXT NOT NULL DEFAULT 'ready', created_at REAL NOT NULL)")
        conn.commit()
        conn.execute("INSERT INTO games(name, code, host_token_hash, state, created_at) "
                     "VALUES ('old', 'AAAA', 'h', 'lobby', 1.0)")
        conn.execute("INSERT INTO rounds(game_id, position, title) VALUES (1, 1, 'a')")
        conn.execute("INSERT INTO rounds(game_id, position, title) VALUES (1, 1, 'b')")
        ra = conn.execute("SELECT id FROM rounds WHERE title = 'a'").fetchone()[0]
        rb = conn.execute("SELECT id FROM rounds WHERE title = 'b'").fetchone()[0]
        conn.execute("INSERT INTO questions(game_id, round_id, position, stem, answer, created_at) "
                     "VALUES (1, ?, 1, 'q a', 'x', 1.0)", (ra,))
        conn.execute("INSERT INTO questions(game_id, round_id, position, stem, answer, created_at) "
                     "VALUES (1, ?, 2, 'q b', 'y', 1.0)", (rb,))
        conn.commit()
        conn.close()

        db.init_db(path)

        conn = db.connect(path)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(rounds)")}
            self.assertTrue({"category", "status", "error", "library_round_id"} <= cols)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE game_id = 1").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE game_id = 1").fetchone()[0], 2)
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_rounds_game_position'").fetchone())
            st = conn.execute("SELECT status FROM rounds WHERE game_id = 1").fetchone()[0]
            self.assertEqual(st, "ready")
        finally:
            conn.close()

        # idempotent: re-running init_db on an already-migrated DB is a no-op.
        db.init_db(path)

    def test_fresh_db_has_library_tables(self):
        conn = db.connect(self.db_path)
        try:
            for table in ("library_rounds", "library_questions"):
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                self.assertIsNotNone(row, table)
        finally:
            conn.close()

    # ---- locked / unauthorized / authorized ---------------------------------
    def _management_paths(self, g):
        return [
            ("GET", "/api/games", None),
            ("GET", "/api/library/rounds", None),
            ("POST", "/api/game/create", {"name": "x"}),
            ("POST", "/api/game/select", {"game_id": g["game_id"]}),
            ("POST", "/api/game/delete", {"game_id": g["game_id"], "confirm": True}),
            ("POST", "/api/game/configure", {"game_id": g["game_id"],
                                             "rounds": [{"category": "c"}]}),
        ]

    def test_management_locked_when_admin_unset_or_short(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        for method, path, body in self._management_paths(g):
            with mock.patch.dict(os.environ, {"TRIVIA_ADMIN_TOKEN": ""}):
                status, b = self._request(port, method, path, body,
                                          headers={"X-Admin-Token": ADMIN_TOKEN})
            self.assertEqual(status, 403, path)
            self.assertEqual(b["error"],
                             "management locked: set TRIVIA_ADMIN_TOKEN "
                             "(at least 16 characters)")
        # too short (< 16 chars)
        with mock.patch.dict(os.environ, {"TRIVIA_ADMIN_TOKEN": "short"}):
            status, b = self._request(port, "GET", "/api/games",
                                      headers={"X-Admin-Token": "short"})
        self.assertEqual(status, 403)

    def test_management_wrong_token_rejected(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        for method, path, body in self._management_paths(g):
            status, b = self._request(port, method, path, body,
                                      headers={"X-Admin-Token": "not-the-token"})
            self.assertEqual(status, 403, path)
            self.assertEqual(b["error"], "admin token rejected")

    def test_management_authorized(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        # GET /api/games
        status, b = self._request(port, "GET", "/api/games", headers=self._admin())
        self.assertEqual(status, 200)
        self.assertIn("games", b)
        self.assertTrue(any(x["id"] == g["game_id"] for x in b["games"]))
        # GET /api/library/rounds
        status, b = self._request(port, "GET", "/api/library/rounds", headers=self._admin())
        self.assertEqual(status, 200)
        self.assertIn("rounds", b)
        # configure (202 + job_id + game_id)
        with mock.patch.object(gen, "generate_questions", _instant_stub()):
            status, b = self._request(
                port, "POST", "/api/game/configure",
                {"game_id": g["game_id"], "rounds": [{"category": "c"}]},
                headers=self._admin())
            self.assertEqual(status, 202, b)
            self.assertIn("job_id", b)
            self.assertEqual(b["game_id"], g["game_id"])
            self._poll_state(port, g, lambda s: all(r["status"] == "ready"
                                                    for r in s["rounds"]))

    # ---- contestant credentials cannot call management APIs ----------------
    def test_room_code_team_token_and_admin_not_accepted_as_credentials(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        _, team = self._request(port, "POST", "/api/game/join",
                                {"code": g["code"], "team_name": "T"})
        # team token / room code in the admin header slot -> 403
        for bad in (team["join_token"], g["code"], g["host_token"]):
            status, b = self._request(port, "GET", "/api/games",
                                      headers={"X-Admin-Token": bad})
            self.assertEqual(status, 403, bad)
            self.assertEqual(b["error"], "admin token rejected")
        # admin token is never a contestant credential (join/answer/rename)
        status, b = self._request(port, "POST", "/api/answer",
                                  {"join_token": ADMIN_TOKEN, "answer_text": "x"})
        self.assertEqual(status, 401)
        status, b = self._request(port, "POST", "/api/team/rename",
                                  {"join_token": ADMIN_TOKEN, "name": "x"})
        self.assertEqual(status, 401)
        status, b = self._request(
            port, "GET",
            f"/api/state?game_id={g['game_id']}&role=team&token={ADMIN_TOKEN}")
        self.assertEqual(status, 401)
        # admin token is never a host token
        status, b = self._request(
            port, "POST", "/api/action",
            {"game_id": g["game_id"], "token": ADMIN_TOKEN, "action": "start",
             "params": {}})
        self.assertEqual(status, 401)

    # ---- game list / select / token rotation / delete ----------------------
    def test_list_select_token_rotation_delete_cascade_library_retained(self):
        httpd, port, _ = self._start_app()
        g = self._create(port, "Game One")
        old_token = g["host_token"]
        # seed a library round by generating one round in game A
        lib_before = len(self._library(port))
        with mock.patch.object(gen, "generate_questions", _instant_stub()):
            self._request(port, "POST", "/api/game/configure",
                          {"game_id": g["game_id"],
                           "rounds": [{"category": "Science", "question_count": 3}]},
                          headers=self._admin())
            self._poll_state(port, g, lambda s: s["rounds"][0]["status"] == "ready")
        lib_after = self._library(port)
        self.assertEqual(len(lib_after), lib_before + 1)
        science = next(r for r in lib_after if r["category"] == "Science")
        self.assertEqual(science["question_count"], 3)
        # second game to prove delete is per-game
        g2 = self._create(port, "Game Two")

        # list shows both with counts
        status, b = self._request(port, "GET", "/api/games", headers=self._admin())
        ids = {x["id"]: x for x in b["games"]}
        self.assertIn(g["game_id"], ids)
        self.assertIn(g2["game_id"], ids)
        self.assertEqual(ids[g["game_id"]]["round_count"], 1)
        self.assertEqual(ids[g["game_id"]]["question_count"], 3)

        # select rotates the host token
        status, b = self._request(port, "POST", "/api/game/select",
                                  {"game_id": g["game_id"]}, headers=self._admin())
        self.assertEqual(status, 200, b)
        new_token = b["host_token"]
        self.assertNotEqual(new_token, old_token)
        # old token no longer authorizes
        status, _ = self._request(
            port, "GET",
            f"/api/state?game_id={g['game_id']}&role=host&token={old_token}")
        self.assertEqual(status, 401)
        # new token does
        status, s = self._request(
            port, "GET",
            f"/api/state?game_id={g['game_id']}&role=host&token={new_token}")
        self.assertEqual(status, 200)
        # metadata returned
        self.assertEqual(b["game_id"], g["game_id"])
        self.assertEqual(b["name"], "Game One")

        # delete requires confirm:true
        status, _ = self._request(port, "POST", "/api/game/delete",
                                  {"game_id": g["game_id"]}, headers=self._admin())
        self.assertEqual(status, 400)
        # add a team + answer so the cascade has real child rows to delete
        self._request(port, "POST", "/api/game/join",
                      {"code": g["code"], "team_name": "Team A"})
        # delete game A
        status, b = self._request(port, "POST", "/api/game/delete",
                                  {"game_id": g["game_id"], "confirm": True},
                                  headers=self._admin())
        self.assertEqual(status, 200, b)
        # game gone from list
        status, b = self._request(port, "GET", "/api/games", headers=self._admin())
        self.assertNotIn(g["game_id"], {x["id"] for x in b["games"]})
        self.assertIn(g2["game_id"], {x["id"] for x in b["games"]})
        # library round survives
        science = next(r for r in self._library(port) if r["category"] == "Science")
        self.assertEqual(science["question_count"], 3)

    # ---- config validation -------------------------------------------------
    def test_config_validation(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)

        def configure(rounds):
            return self._request(port, "POST", "/api/game/configure",
                                 {"game_id": g["game_id"], "rounds": rounds},
                                 headers=self._admin())

        # empty / not a list
        self.assertEqual(configure([])[0], 400)
        self.assertEqual(configure(None)[0], 400)
        self.assertEqual(configure("nope")[0], 400)
        # too many rounds
        self.assertEqual(configure([{"category": "c"}] * 13)[0], 400)
        # both category and library_round_id
        status, b = configure([{"category": "c", "library_round_id": 1}])
        self.assertEqual(status, 400)
        self.assertIn("not both", b["errors"][0])
        # unknown key
        status, b = configure([{"category": "c", "nonsense": 1}])
        self.assertEqual(status, 400)
        # empty category
        self.assertEqual(configure([{"category": "   "}])[0], 400)
        # question_count out of bounds / wrong type
        self.assertEqual(configure([{"category": "c", "question_count": 0}])[0], 400)
        self.assertEqual(configure([{"category": "c", "question_count": 41}])[0], 400)
        self.assertEqual(configure([{"category": "c", "question_count": "x"}])[0], 400)
        self.assertEqual(configure([{"category": "c", "question_count": True}])[0], 400)
        # unknown library_round_id
        status, b = configure([{"library_round_id": 99999}])
        self.assertEqual(status, 400)
        self.assertIn("unknown library_round_id", b["errors"][0])
        # neither field
        self.assertEqual(configure([{}])[0], 400)
        # game not in lobby: start the game first
        self._host_action(port, g, "add_question",
                          {"stem": "Q?", "answer": "a",
                           "options": ["a", "b", "c", "d"], "type": "mc"})
        self._host_action(port, g, "start")
        status, b = configure([{"category": "c"}])
        self.assertEqual(status, 400)
        self.assertIn("not in lobby", b["errors"][0])
        # unknown game
        status, b = self._request(port, "POST", "/api/game/configure",
                                  {"game_id": 99999, "rounds": [{"category": "c"}]},
                                  headers=self._admin())
        self.assertEqual(status, 404)

    # ---- delayed generation: round 1 ready while round 2 pending -----------
    def test_round1_ready_while_round2_pending_next_round_safe(self):
        state = {"calls": 0}
        release2 = threading.Event()
        self.addCleanup(release2.set)  # never leave the worker blocked

        def fake(topic, count, **kw):
            state["calls"] += 1
            if state["calls"] >= 2:
                release2.wait()  # block indefinitely until the test releases it
            return {"accepted": [_question(topic, i) for i in range(count)],
                    "rejected": [], "audit": []}

        httpd, port, _ = self._start_app()
        g = self._create(port)
        with mock.patch.object(gen, "generate_questions", fake):
            status, b = self._request(
                port, "POST", "/api/game/configure",
                {"game_id": g["game_id"],
                 "rounds": [{"category": "R1", "question_count": 2},
                            {"category": "R2", "question_count": 2}]},
                headers=self._admin())
            self.assertEqual(status, 202, b)

            # round 1 becomes ready while round 2 is still pending/generating
            s = self._poll_state(
                port, g,
                lambda s: any(r["position"] == 1 and r["status"] == "ready"
                              for r in s.get("rounds", [])))
            r2 = next(r for r in s["rounds"] if r["position"] == 2)
            self.assertIn(r2["status"], ("pending", "generating"))

            # start the game with round 1 and play it out to round_end
            self._host_action(port, g, "start")
            for _ in range(2):
                self._host_action(port, g, "reveal")
                self._host_action(port, g, "next")
            _, s = self._state(port, g)
            self.assertEqual(s["state"], "round_end")

            # next_round against a not-ready round: clear error, state intact
            status, b = self._request(
                port, "POST", "/api/action",
                {"game_id": g["game_id"], "token": g["host_token"],
                 "action": "next_round", "params": {}})
            self.assertEqual(status, 400)
            self.assertIn("not ready", b["error"])
            _, s = self._state(port, g)
            self.assertEqual(s["state"], "round_end")
            self.assertIsNotNone(s["current_round_id"])

            # release round 2 -> it becomes ready -> next_round now succeeds
            release2.set()
            self._poll_state(
                port, g,
                lambda s: any(r["position"] == 2 and r["status"] == "ready"
                              for r in s.get("rounds", [])))
            self._host_action(port, g, "next_round")
            _, s = self._state(port, g)
            self.assertEqual(s["state"], "question")

    # ---- mixed generated/library reuse + deep copy -------------------------
    def test_mixed_generated_library_reuse_deep_copy(self):
        httpd, port, _ = self._start_app()
        before = {r["id"] for r in self._library(port)}
        gA = self._create(port, "A")
        with mock.patch.object(gen, "generate_questions", _instant_stub()):
            self._request(port, "POST", "/api/game/configure",
                          {"game_id": gA["game_id"],
                           "rounds": [{"category": "ScienceMix", "question_count": 3}]},
                          headers=self._admin())
            self._poll_state(port, gA, lambda s: s["rounds"][0]["status"] == "ready")
            # a new library round was created by generation
            new = [r for r in self._library(port) if r["id"] not in before]
            self.assertEqual(len(new), 1)
            lib_id = new[0]["id"]
            self.assertEqual(new[0]["category"], "ScienceMix")
            # mixed configure in game B: reuse the library round + generate another
            gB = self._create(port, "B")
            self._request(port, "POST", "/api/game/configure",
                          {"game_id": gB["game_id"],
                           "rounds": [{"library_round_id": lib_id},
                                      {"category": "HistoryMix", "question_count": 2}]},
                          headers=self._admin())
            sB = self._poll_state(port, gB, lambda s: all(r["status"] == "ready"
                                                          for r in s["rounds"]))
            self.assertEqual(len(sB["rounds"]), 2)
            self.assertEqual(sB["rounds"][0]["library_round_id"], lib_id)
            self.assertEqual(sB["rounds"][0]["question_count"], 3)
            self.assertEqual(sB["rounds"][1]["question_count"], 2)

        # deep-copy: distinct question rows in the `questions` table across both
        # games, and the library round is untouched (library_questions unchanged).
        conn = db.connect(self.db_path)
        try:
            lib_qids = {r["id"] for r in conn.execute(
                "SELECT id FROM library_questions WHERE library_round_id = ?", (lib_id,))}
            a_qids = {r["id"] for r in conn.execute(
                "SELECT id FROM questions WHERE game_id = ?", (gA["game_id"],))}
            b_qids = {r["id"] for r in conn.execute(
                "SELECT id FROM questions WHERE game_id = ?", (gB["game_id"],))}
            self.assertEqual(len(lib_qids), 3)
            self.assertEqual(len(a_qids), 3)
            self.assertEqual(len(b_qids), 5)  # 3 copied + 2 generated
            self.assertTrue(a_qids.isdisjoint(b_qids))
            # content is copied: game A's stems equal the library round's stems
            lib_stems = {r["stem"] for r in conn.execute(
                "SELECT stem FROM library_questions WHERE library_round_id = ?", (lib_id,))}
            a_stems = {r["stem"] for r in conn.execute(
                "SELECT stem FROM questions WHERE game_id = ?", (gA["game_id"],))}
            self.assertEqual(lib_stems, a_stems)
        finally:
            conn.close()

        # delete game A -> library survives, game B untouched
        self._request(port, "POST", "/api/game/delete",
                      {"game_id": gA["game_id"], "confirm": True},
                      headers=self._admin())
        lib_ids = {r["id"] for r in self._library(port)}
        self.assertIn(lib_id, lib_ids)  # the generated library round survives
        _, sB = self._state(port, gB)
        self.assertEqual(len(sB["rounds"]), 2)

    # ---- deletion during generation ----------------------------------------
    def test_delete_during_generation_no_rows_recreated(self):
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)  # never leave the worker blocked

        def fake(topic, count, **kw):
            started.set()
            release.wait()
            return {"accepted": [_question(topic, i) for i in range(count)],
                    "rejected": [], "audit": []}

        httpd, port, _ = self._start_app()
        g = self._create(port)
        with mock.patch.object(gen, "generate_questions", fake):
            status, b = self._request(
                port, "POST", "/api/game/configure",
                {"game_id": g["game_id"], "rounds": [{"category": "Science"}]},
                headers=self._admin())
            self.assertEqual(status, 202, b)
            job_id = b["job_id"]
            self.assertTrue(started.wait(timeout=5), "generation never started")
            # delete the game while generation is in flight
            status, b = self._request(port, "POST", "/api/game/delete",
                                      {"game_id": g["game_id"], "confirm": True},
                                      headers=self._admin())
            self.assertEqual(status, 200, b)
            release.set()
            # wait for the background job to finish
            deadline = time.time() + 10
            while time.time() < deadline:
                _, s = self._request(port, "GET",
                                     f"/api/configure/status?job_id={job_id}",
                                     headers=self._admin())
                if isinstance(s, dict) and s.get("done"):
                    break
                time.sleep(0.05)
        # no game-owned rows were recreated
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM games WHERE id = ?", (g["game_id"],)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE game_id = ?",
                (g["game_id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE game_id = ?",
                (g["game_id"],)).fetchone()[0], 0)
        finally:
            conn.close()

    # ---- restart / interrupted status --------------------------------------
    def test_restart_interrupts_inflight_rounds(self):
        e = engine.GameEngine(self.db_path)
        g = e.create_game("Restart")
        errors, steps = e.configure_game(
            g["game_id"], [{"category": "A"}, {"category": "B"}])
        self.assertEqual(errors, [])
        # round 1 mid-generation, round 2 still pending — then the process dies
        e.set_round_status(steps[0]["round_id"], "generating")
        n = e.interrupt_inflight_rounds()
        self.assertEqual(n, 2)
        conn = db.connect(self.db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM rounds WHERE game_id = ? ORDER BY position",
                (g["game_id"],)).fetchall()]
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["status"], "failed")
            self.assertEqual(r["error"], "interrupted by restart")
        # and the game is still in lobby (never phantom-ready / never started)
        self.assertEqual(e.game_metadata(g["game_id"])["state"], "lobby")

    # ---- QR public origin + room-code bound --------------------------------
    def test_qr_public_origin_and_code_bound(self):
        # unit: normalized origin + join URL (unset -> direct LAN Host)
        saved = os.environ.pop("TRIVIA_PUBLIC_BASE_URL", None)
        try:
            self.assertEqual(server._public_origin(), None)
            self.assertEqual(server._join_url("ABCD"), "http://localhost/join?code=ABCD")
            self.assertEqual(server._join_url("ABCD", "192.168.1.9:8130"),
                             "http://192.168.1.9:8130/join?code=ABCD")
            # normalized origin strips path/trailing slash, keeps scheme+netloc
            os.environ["TRIVIA_PUBLIC_BASE_URL"] = "https://trivia.example.com/base/"
            self.assertEqual(server._public_origin(), "https://trivia.example.com")
            self.assertEqual(server._join_url("ABCD"),
                             "https://trivia.example.com/join?code=ABCD")
        finally:
            if saved is not None:
                os.environ["TRIVIA_PUBLIC_BASE_URL"] = saved
            else:
                os.environ.pop("TRIVIA_PUBLIC_BASE_URL", None)

        # HTTP: /qr returns a PNG for a valid 4-char code, 400 for oversized/odd
        httpd, port, _ = self._start_app()
        g = self._create(port)
        status, body = self._request_raw(port, "GET", f"/qr?code={g['code']}")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(self._request_raw(port, "GET", "/qr?code=" + "A" * 500)[0], 400)
        self.assertEqual(self._request_raw(port, "GET", "/qr?code=AB")[0], 400)
        self.assertEqual(self._request_raw(port, "GET", "/qr?code=AB!D")[0], 400)

        # /api/state exposes the public join_url when TRIVIA_PUBLIC_BASE_URL is set
        with mock.patch.dict(os.environ,
                             {"TRIVIA_PUBLIC_BASE_URL": "https://trivia.example.com"}):
            status, s = self._state(port, g)
            self.assertEqual(status, 200)
            self.assertEqual(s["join_url"],
                             f"https://trivia.example.com/join?code={g['code']}")

    def test_state_join_url_uses_request_host_when_public_unset(self):
        # HTTP: /api/state must advertise the request Host (direct LAN) when
        # TRIVIA_PUBLIC_BASE_URL is unset — regression for a bug where it fell
        # back to http://localhost instead of the LAN address (server.py:678).
        saved = os.environ.pop("TRIVIA_PUBLIC_BASE_URL", None)
        try:
            httpd, port, _ = self._start_app()
            g = self._create(port)
            status, s = self._request(
                port, "GET",
                f"/api/state?game_id={g['game_id']}&role=host&token={g['host_token']}",
                headers={"Host": "192.168.1.9:8130"})
            self.assertEqual(status, 200, s)
            self.assertEqual(s["join_url"],
                             f"http://192.168.1.9:8130/join?code={g['code']}")
        finally:
            if saved is not None:
                os.environ["TRIVIA_PUBLIC_BASE_URL"] = saved
            else:
                os.environ.pop("TRIVIA_PUBLIC_BASE_URL", None)

    # ---- /api/state per-round status, no answer leakage --------------------
    def test_state_exposes_round_status_without_answer_leak(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        with mock.patch.object(gen, "generate_questions", _instant_stub()):
            self._request(port, "POST", "/api/game/configure",
                          {"game_id": g["game_id"],
                           "rounds": [{"category": "Science", "question_count": 3}]},
                          headers=self._admin())
            self._poll_state(port, g, lambda s: s["rounds"][0]["status"] == "ready")
        # host sees status/category/count/library_round_id
        _, hs = self._state(port, g, role="host")
        r = hs["rounds"][0]
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["category"], "Science")
        self.assertEqual(r["question_count"], 3)
        self.assertIn("library_round_id", r)
        # team sees status/category/count but NOT error or library_round_id,
        # and no answer text anywhere in the rounds list
        _, team = self._request(port, "POST", "/api/game/join",
                                {"code": g["code"], "team_name": "T"})
        _, ts = self._state(port, g, role="team", token=team["join_token"])
        tr = ts["rounds"][0]
        self.assertEqual(tr["status"], "ready")
        self.assertEqual(tr["category"], "Science")
        self.assertEqual(tr["question_count"], 3)
        self.assertNotIn("error", tr)
        self.assertNotIn("library_round_id", tr)
        blob = json.dumps(ts["rounds"])
        self.assertNotIn("answer", blob.lower().replace("answered", ""))

    # ---- generation job status is protected (no anonymous metadata) --------
    def test_job_status_protected(self):
        httpd, port, _ = self._start_app()
        g = self._create(port)
        with mock.patch.object(gen, "generate_questions", _instant_stub()):
            status, b = self._request(
                port, "POST", "/api/game/configure",
                {"game_id": g["game_id"], "rounds": [{"category": "c"}]},
                headers=self._admin())
            job_id = b["job_id"]
            self._poll_state(port, g, lambda s: s["rounds"][0]["status"] == "ready")
        # anonymous -> 401
        status, _ = self._request(port, "GET", f"/api/configure/status?job_id={job_id}")
        self.assertEqual(status, 401)
        # wrong game -> 404 (a host of another game must not learn this job exists)
        g2 = self._create(port)
        status, _ = self._request(
            port, "GET",
            f"/api/configure/status?job_id={job_id}&game_id={g2['game_id']}"
            f"&token={g2['host_token']}")
        self.assertEqual(status, 404)
        # matching host token -> 200
        status, b = self._request(
            port, "GET",
            f"/api/configure/status?job_id={job_id}&game_id={g['game_id']}"
            f"&token={g['host_token']}")
        self.assertEqual(status, 200)
        self.assertTrue(b["done"])
        # admin -> 200
        status, b = self._request(port, "GET", f"/api/configure/status?job_id={job_id}",
                                  headers=self._admin())
        self.assertEqual(status, 200)
        # legacy /api/generate/status is also protected
        status, _ = self._request(port, "GET", "/api/generate/status?job_id=nope")
        self.assertEqual(status, 401)


    # ---- console UI static contract (DOM harness) ---------------------------
    def test_console_management_ui_contract(self):
        html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "console.html")
        with open(html_path, "r", encoding="utf-8") as fh:
            text = fh.read()

        # Management UI elements the console flow depends on.
        for frag in ('id="admin-token"', 'id="prev-games"', 'id="prev-games-list"',
                     'id="round-count"', 'id="round-config"', 'id="round-progress"',
                     'id="gate-status"'):
            self.assertIn(frag, text, f"missing {frag}")

        # The management functions are wired.
        for fn in ("refreshManagement", "renderPrevGames", "selectGame",
                   "deleteGame", "loadLibrary", "renderRoundPickers", "configure",
                   "adminHeaders"):
            self.assertIn(fn, text, f"missing function {fn}")

        # Admin token is page-session only: never written to web storage, never
        # in a URL, never in a value= attribute. (Host tokens ARE stored; that
        # is by design — only the admin token must never be.)
        self.assertNotRegex(text, r'setItem\([^)]*[Aa]dmin',
                            "admin token must not be written to storage")
        self.assertNotRegex(text, r'[?&]admin[_-]?token=',
                            "admin token must never appear in a URL")
        self.assertNotRegex(text,
                            r'<input[^>]*id="admin-token"[^>]*\bvalue\s*=',
                            "admin-token input must not carry a value attribute")

        # The served page is byte-identical to the file (no build step drift).
        httpd, port, _ = self._start_app()
        status, served = self._request_raw(port, "GET", "/static/console.html")
        self.assertEqual(status, 200)
        self.assertEqual(served, text.encode("utf-8"))
        self.assertNotIn(ADMIN_TOKEN.encode(), served)


if __name__ == "__main__":
    unittest.main(verbosity=2)
