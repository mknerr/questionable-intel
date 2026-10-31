"""Question-import HTTP API, offline: admin gating, parse/save, library rounds
playable through /api/game/configure, and the AI paths with ``gen._chat``
monkeypatched (settings endpoints reject loopback, so no real model is used)."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import gen
import importer
import server

ADMIN_TOKEN = "import-test-admin-token-1234567"

ENDPOINTS = (("POST", "/api/import/parse"), ("POST", "/api/import/save"),
             ("POST", "/api/import/ai"), ("GET", "/api/import/status?job_id=x"))

CAPITALS = "Capital of France\tParis\nCapital of Germany\tBerlin\nCapital of Italy\tRome"


class ImportApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(cls.db_path)
        cls._saved_admin = os.environ.get("TRIVIA_ADMIN_TOKEN")
        os.environ["TRIVIA_ADMIN_TOKEN"] = ADMIN_TOKEN
        # The default provider needs a key; the model itself is always stubbed.
        cls._saved_key = os.environ.get("TRIVIA_LLM_API_KEY")
        os.environ["TRIVIA_LLM_API_KEY"] = "test-key"
        cls.app = server.App(cls.db_path)
        cls.httpd = server.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if cls._saved_admin is None:
            os.environ.pop("TRIVIA_ADMIN_TOKEN", None)
        else:
            os.environ["TRIVIA_ADMIN_TOKEN"] = cls._saved_admin
        if cls._saved_key is None:
            os.environ.pop("TRIVIA_LLM_API_KEY", None)
        else:
            os.environ["TRIVIA_LLM_API_KEY"] = cls._saved_key
        cls.tmp.cleanup()

    def setUp(self):
        self._orig_chat = gen._chat
        self.addCleanup(lambda: setattr(gen, "_chat", self._orig_chat))
        gen._chat = lambda *a, **k: self.fail("the model must not be called here")

    # ---- harness -------------------------------------------------------------
    def req(self, method, path, body=None, admin=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"X-Admin-Token": ADMIN_TOKEN} if admin is True else (
                {"X-Admin-Token": admin} if admin else {})
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                return resp.status, json.loads(payload)
            except ValueError:
                return resp.status, payload.decode("utf-8", "replace")
        finally:
            conn.close()

    def library(self):
        status, b = self.req("GET", "/api/library/rounds")
        self.assertEqual(status, 200, b)
        return b["rounds"]

    def wait_job(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, st = self.req("GET", f"/api/import/status?job_id={job_id}")
            self.assertEqual(status, 200, st)
            if st["done"]:
                return st
            time.sleep(0.03)
        self.fail("import job did not finish")

    def start_ai(self, body):
        status, b = self.req("POST", "/api/import/ai", body)
        self.assertEqual(status, 202, b)
        return b["job_id"]

    # ---- admin gate ------------------------------------------------------------
    def test_every_endpoint_is_admin_gated_before_parsing(self):
        for method, path in ENDPOINTS:
            for admin in (False, "wrong-token-wrong-token-1"):
                # A garbage body must still get 403, not a 400: auth comes first.
                status, b = self.req(method, path, raw=b"not json" if method == "POST" else None,
                                     admin=admin)
                self.assertEqual(status, 403, (path, admin, b))

    def test_locked_when_no_admin_token_is_configured(self):
        saved = os.environ.pop("TRIVIA_ADMIN_TOKEN")
        try:
            status, b = self.req("POST", "/api/import/parse", {"text": "x"}, admin=ADMIN_TOKEN)
            self.assertEqual(status, 403)
            self.assertIn("locked", b["error"])
        finally:
            os.environ["TRIVIA_ADMIN_TOKEN"] = saved

    # ---- request validation ------------------------------------------------------
    def test_bad_bodies_get_a_helpful_400(self):
        for raw in (b"not json", b"[1,2]", b"{}", b'{"text": 5}', b""):
            status, b = self.req("POST", "/api/import/parse", raw=raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("text", b["error"])

    def test_oversize_body_is_refused_with_a_message(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(("POST /api/import/parse HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       f"X-Admin-Token: {ADMIN_TOKEN}\r\nContent-Length: {300 * 1024}\r\n\r\n").encode())
            reply = b""
            while True:  # the server closes after refusing, so read to EOF
                chunk = s.recv(8192)
                if not chunk:
                    break
                reply += chunk
        finally:
            s.close()
        self.assertIn(b" 400 ", reply.split(b"\r\n")[0])
        self.assertIn(b"too much text", reply)

    def test_import_accepts_bodies_beyond_the_normal_limit_but_others_do_not(self):
        text = "\n".join(f"Question number {i} of the quiz?\tAnswer {i}" for i in range(1800))
        self.assertGreater(len(text), server.MAX_BODY)
        status, b = self.req("POST", "/api/import/parse", {"text": text})
        self.assertEqual(status, 200, b)
        self.assertEqual(len(b["questions"]), importer.MAX_IMPORT_QUESTIONS)
        # Everything else keeps the 64 KB cap: the oversized body is ignored.
        status, b = self.req("POST", "/api/game/create", {"name": "x", "pad": "p" * 70_000})
        self.assertEqual(status, 201)
        self.assertEqual(b["name"], "Trivia Night")

    # ---- parse -------------------------------------------------------------------
    def test_parse_reports_format_counts_and_saves_nothing(self):
        before = len(self.library())
        status, b = self.req("POST", "/api/import/parse", {
            "text": "Q: Who wrote Hamlet?\nA: Shakespeare\n\nQ: Largest ocean?\n\nA: Everest"})
        self.assertEqual(status, 200, b)
        self.assertEqual(b["format"], "tagged")
        self.assertEqual(b["counts"], {"total": 3, "ready": 1, "needs_answer": 1, "needs_question": 1})
        self.assertEqual(len(self.library()), before)

    def test_parse_swap(self):
        status, b = self.req("POST", "/api/import/parse", {"text": CAPITALS, "swap": True})
        self.assertEqual((b["questions"][0]["stem"], b["questions"][0]["answer"]),
                         ("Paris", "Capital of France"))

    def test_blank_text_is_a_message_not_an_error_status(self):
        status, b = self.req("POST", "/api/import/parse", {"text": "  "})
        self.assertEqual(status, 200)
        self.assertEqual(b["questions"], [])
        self.assertIn("Paste", b["errors"][0]["message"])

    # ---- save ----------------------------------------------------------------------
    def test_save_creates_an_attachable_library_round_and_a_game_plays_a_text_question(self):
        status, b = self.req("POST", "/api/import/save", {"text": CAPITALS, "category": "Capitals"})
        self.assertEqual(status, 201, b)
        (rnd,) = b["rounds"]
        self.assertEqual((rnd["category"], rnd["question_count"]), ("Capitals", 3))
        self.assertEqual(b["skipped"], 0)
        listed = next(r for r in self.library() if r["id"] == rnd["id"])
        self.assertEqual((listed["source"], listed["question_count"]), ("imported", 3))

        status, g = self.req("POST", "/api/game/create", {"name": "Imported"})
        status, team = self.req("POST", "/api/game/join", {"code": g["code"], "team_name": "Owls"}, admin=False)
        status, c = self.req("POST", "/api/game/configure",
                             {"game_id": g["game_id"], "rounds": [{"library_round_id": rnd["id"]}]})
        self.assertEqual(status, 202, c)
        self.wait_state(g, lambda s: s["rounds"] and s["rounds"][0]["status"] == "ready")

        self.action(g, "start")
        s = self.wait_state(g, lambda s: s["question"] is not None)
        self.assertEqual(s["question"]["stem"], "Capital of France")
        self.assertFalse(s["question"].get("options"))
        status, _ = self.req("POST", "/api/answer",
                             {"join_token": team["join_token"], "answer_text": "paris"}, admin=False)
        self.assertEqual(status, 200)
        self.action(g, "reveal")
        s = self.wait_state(g, lambda s: s["state"] == "revealed")
        # A text question is never auto-scored: it waits for the host to judge.
        self.assertEqual([t["total"] for t in s["teams"]], [0])
        self.assertEqual(s["judging"][0]["answer"], "paris")
        self.assertTrue(s["judging"][0]["suggested_correct"])
        self.action(g, "award", {"team_id": team["team_id"], "delta": 10, "reason": "judged"})
        s = self.wait_state(g, lambda s: s["teams"][0]["total"] == 10)

    def test_save_groups_by_row_category_then_request_category_then_default(self):
        text = ("question,answer,category,points\n"
                "Who wrote Hamlet?,Shakespeare,Books,30\n"
                "Who wrote Emma?,Austen,Books,\n"
                "Largest ocean on Earth?,Pacific,,\n")
        status, b = self.req("POST", "/api/import/save", {"text": text, "category": "Mixed bag"})
        self.assertEqual(status, 201, b)
        self.assertEqual([(r["category"], r["question_count"]) for r in b["rounds"]],
                         [("Books", 2), ("Mixed bag", 1)])
        status, b = self.req("POST", "/api/import/save", {"text": CAPITALS})
        self.assertEqual(b["rounds"][0]["category"], "Imported")
        books = next(r for r in self.library() if r["category"] == "Books")
        conn = db.connect(self.db_path)
        try:
            pts = [r["points"] for r in conn.execute(
                "SELECT points FROM library_questions WHERE library_round_id = ? ORDER BY position",
                (books["id"],))]
        finally:
            conn.close()
        self.assertEqual(pts, [30, 10])

    def test_save_skips_incomplete_rows_and_counts_them(self):
        text = "Q: Who wrote Hamlet?\nA: Shakespeare\n\nQ: Largest ocean?\n\nA: Everest"
        status, b = self.req("POST", "/api/import/save", {"text": text, "category": "Mixed"})
        self.assertEqual(status, 201, b)
        self.assertEqual((b["rounds"][0]["question_count"], b["skipped"]), (1, 2))

    def test_include_incomplete_saves_unanswered_rows_as_typed_questions(self):
        text = "Q: Who wrote Hamlet?\nA: Shakespeare\n\nQ: Largest ocean?\n\nA: Everest"
        status, b = self.req("POST", "/api/import/save",
                             {"text": text, "category": "Loose ends", "include_incomplete": True})
        self.assertEqual((b["rounds"][0]["question_count"], b["skipped"]), (2, 1))
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT type, answer, options FROM library_questions "
                               "WHERE stem = 'Largest ocean?'").fetchone()
        finally:
            conn.close()
        self.assertEqual((row["type"], row["answer"], row["options"]), ("text", "", None))

    def test_save_with_nothing_savable_is_a_400_that_says_why(self):
        before = len(self.library())
        status, b = self.req("POST", "/api/import/save", {"text": "Who wrote Hamlet?"})
        self.assertEqual(status, 400)
        self.assertIn("Nothing to save", b["error"])
        status, b = self.req("POST", "/api/import/save", {"text": ""})
        self.assertEqual(status, 400)
        self.assertIn("Paste", b["error"])
        self.assertEqual(len(self.library()), before)

    def test_save_ignores_client_supplied_rows(self):
        evil = {"stem": "Injected question?", "answer": "x", "status": "complete", "type": "text"}
        status, b = self.req("POST", "/api/import/save",
                             {"text": CAPITALS, "category": "Trusted", "questions": [evil], "rows": [evil]})
        self.assertEqual(b["rounds"][0]["question_count"], 3)
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM library_questions WHERE stem = 'Injected question?'").fetchone())
        finally:
            conn.close()

    def test_media_url_in_imported_json_is_not_stored(self):
        text = json.dumps([{"stem": "Who wrote Hamlet?", "answer": "Shakespeare",
                            "media_url": "http://evil.example/x.png", "media_type": "image"}])
        status, b = self.req("POST", "/api/import/save", {"text": text, "category": "Media"})
        self.assertEqual(status, 201, b)
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT media_type, media_url FROM library_questions "
                               "WHERE library_round_id = ?", (b["rounds"][0]["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual((row["media_type"], row["media_url"]), ("none", None))

    # ---- AI: fill ---------------------------------------------------------------------
    def fake_model(self, fn, calls=None):
        """Install a ``_chat`` that answers with ``fn(items) -> reply object``;
        the items are read back out of the prompt, as a real model would."""
        def chat(messages, model, base, api_key, timeout, num_predict):
            lines = messages[-1]["content"].split("\n")
            payload = json.loads(lines[next(i for i, l in enumerate(lines) if l.startswith("<<<")) + 1])
            if calls is not None:
                calls.append({"messages": messages, "items": payload})
            return fn(payload)
        gen._chat = chat

    def test_fill_completes_the_gaps_and_keeps_host_text_verbatim(self):
        text = ("Q: Which planet is known as the Red Planet?\n\n"           # needs answer
                "A: Mount Everest\n\n"                                     # needs question
                "Q: What is the capital of France?\nA: Paris\n\n"          # complete, typed
                "Q: Which planet is the largest in our solar system?\n"     # complete, has options
                "A: Jupiter\nW: Mars\nW: Venus")

        def reply(items):
            out = []
            for it in items:
                if "stem" in it and "answer" not in it:
                    out.append({"id": it["id"], "stem": it["stem"], "answer": "Mars",
                                "options": ["Venus", "Mars", "Jupiter", "Saturn"],
                                "confidence": "high"})
                elif "answer" in it and "stem" not in it:
                    out.append({"id": it["id"], "stem": "Which mountain is the tallest on Earth above sea level?",
                                "answer": it["answer"], "confidence": "high",
                                "options": ["K2", "Mount Everest", "Kilimanjaro", "Denali"]})
                else:
                    out.append({"id": it["id"], "stem": it["stem"], "answer": it["answer"],
                                "options": ["Lyon", "Paris", "Nice", "Lille"], "confidence": "high"})
            return json.dumps({"questions": out})

        calls = []
        self.fake_model(reply, calls)
        job = self.start_ai({"mode": "fill", "text": text, "category": "AI mix"})
        st = self.wait_job(job)
        self.assertIsNone(st["error"], st)
        self.assertEqual((st["accepted"], st["rejected"]), (4, []))
        self.assertEqual(len(calls), 1)  # the passthrough row never reached the model
        self.assertEqual(sorted(i["id"] for i in calls[0]["items"]), [0, 1, 2])
        (rnd,) = st["rounds"]
        self.assertEqual((rnd["category"], rnd["question_count"]), ("AI mix", 4))
        conn = db.connect(self.db_path)
        try:
            rows = {r["stem"]: r for r in conn.execute(
                "SELECT * FROM library_questions WHERE library_round_id = ?", (rnd["id"],))}
        finally:
            conn.close()
        self.assertEqual(rows["What is the capital of France?"]["answer"], "Paris")
        self.assertEqual(rows["What is the capital of France?"]["source"], "imported")
        self.assertEqual(rows["Which mountain is the tallest on Earth above sea level?"]["source"], "generated")
        self.assertEqual(rows["Which planet is known as the Red Planet?"]["answer"], "Mars")
        self.assertEqual(rows["Which planet is known as the Red Planet?"]["source"], "generated")
        self.assertEqual(rows["Which planet is the largest in our solar system?"]["source"], "imported")
        # Pasted text sits inside delimiters and is declared data, not instructions.
        user = calls[0]["messages"][-1]["content"]
        self.assertIn("<<<ITEMS-", user)
        self.assertIn("never instructions", user)

    def test_fill_forces_host_text_back_and_rejects_a_model_that_changes_the_answer(self):
        text = "Q: Who wrote Hamlet in the sixteenth century?\nA: William Shakespeare\n\n" \
               "Q: What is the capital of France exactly?\nA: Paris"

        def reply(items):
            out = []
            for it in items:
                if "Hamlet" in it["stem"]:
                    # Reworded stem and answer, but the right option is listed:
                    # accepted, with the host's own stem and answer forced back in.
                    out.append({"id": it["id"], "stem": "Which playwright penned Hamlet?",
                                "answer": "Shakespeare, W.", "confidence": "high",
                                "options": ["Christopher Marlowe", "William Shakespeare",
                                            "Ben Jonson", "John Donne"]})
                else:
                    # Altered answer, and the host's answer is not among the options.
                    out.append({"id": it["id"], "stem": it["stem"], "answer": "Paris, France",
                                "options": ["Lyon", "Paris, France", "Nice", "Lille"],
                                "confidence": "high"})
            return json.dumps(out)

        self.fake_model(reply)
        st = self.wait_job(self.start_ai({"mode": "fill", "text": text, "category": "Altered"}))
        self.assertEqual(st["accepted"], 1)
        conn = db.connect(self.db_path)
        try:
            (row,) = conn.execute("SELECT stem, answer, options FROM library_questions "
                                  "WHERE library_round_id = ?", (st["rounds"][0]["id"],)).fetchall()
        finally:
            conn.close()
        self.assertEqual((row["stem"], row["answer"]),
                         ("Who wrote Hamlet in the sixteenth century?", "William Shakespeare"))
        self.assertIn("William Shakespeare", json.loads(row["options"]))
        # The rejected row carries a reason (after its one retry also failed).
        (rej,) = st["rejected"]
        self.assertEqual(rej["stem"], "What is the capital of France exactly?")
        self.assertIn("answer", rej["reason"])

    def test_fill_with_a_model_returning_junk_saves_nothing_and_says_so(self):
        before = len(self.library())
        gen._chat = lambda *a, **k: "I'm sorry, I can't help with that. <<<>>>"
        st = self.wait_job(self.start_ai({"mode": "fill", "text": "Q: Largest ocean on Earth?",
                                          "category": "Junk"}))
        self.assertTrue(st["error"])
        self.assertEqual((st["accepted"], st["rounds"]), (0, []))
        self.assertEqual(len(self.library()), before)
        for bad in ('{"questions": "nope"}', "[1, 2, 3]", "null", '[{"id": 99}]'):
            gen._chat = lambda *a, _bad=bad, **k: _bad
            st = self.wait_job(self.start_ai({"mode": "fill", "text": "Q: Largest ocean on Earth?"}))
            self.assertEqual(st["accepted"], 0, bad)
            self.assertEqual(st["rounds"], [], bad)
        self.assertEqual(len(self.library()), before)

    def test_fill_when_the_model_is_unreachable_reports_it(self):
        def boom(*a, **k):
            raise gen.netutil.NetError("connection refused")
        gen._chat = boom
        st = self.wait_job(self.start_ai({"mode": "fill", "text": "Q: Largest ocean on Earth?"}))
        self.assertIn("could not complete", st["error"])
        self.assertEqual(st["accepted"], 0)

    def test_fill_needs_nothing_from_the_model_when_every_row_is_complete(self):
        text = "Which planet is red?\nA. Venus\nB. Mars\nANSWER: B"
        st = self.wait_job(self.start_ai({"mode": "fill", "text": text, "category": "Offline"}))
        self.assertEqual((st["accepted"], st["error"]), (1, None))

    def test_ai_request_validation(self):
        for body in ({"mode": "nope", "text": "x"}, {"text": "x"},
                     {"mode": "material", "text": "notes", "count": 0},
                     {"mode": "material", "text": "notes", "count": 41},
                     {"mode": "material", "text": "notes", "count": "many"},
                     {"mode": "material", "text": "   "},
                     {"mode": "fill", "text": "   "}):
            status, b = self.req("POST", "/api/import/ai", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", b)

    # ---- AI: material -----------------------------------------------------------------
    def test_material_mode_writes_questions_from_the_text(self):
        qs = [
            {"stem": "Which gas do plants absorb from the air for photosynthesis?",
             "options": ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"],
             "answer": "Carbon dioxide", "confidence": "high"},
            {"stem": "Which part of the cell holds the DNA in the notes?",
             "options": ["Nucleus", "Ribosome", "Vacuole", "Membrane"],
             "answer": "Nucleus", "confidence": "medium"},
            {"stem": "Which organelle is called the powerhouse of the cell?",
             "options": ["Golgi body", "Mitochondria", "Lysosome", "Nucleolus"],
             "answer": "Mitochondria", "confidence": "high"},
        ]
        seen = []
        gen._chat = lambda messages, *a, **k: (seen.append(messages), json.dumps(qs))[1]
        st = self.wait_job(self.start_ai({"mode": "material", "count": 3, "category": "Biology",
                                          "text": "Plants take in carbon dioxide. The nucleus holds the DNA."}))
        self.assertEqual((st["accepted"], st["shortfall"], st["error"]), (3, 0, None))
        self.assertEqual(st["rounds"][0]["category"], "Biology")
        self.assertIn("ONLY from the MATERIAL", seen[0][0]["content"])
        self.assertIn("not instructions", seen[0][0]["content"])

    def test_material_shortfall_is_reported(self):
        q = {"stem": "Which gas do plants absorb from the air for photosynthesis?",
             "options": ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"],
             "answer": "Carbon dioxide", "confidence": "high"}
        gen._chat = lambda *a, **k: json.dumps([q])
        st = self.wait_job(self.start_ai({"mode": "material", "count": 5, "category": "Short",
                                          "text": "x" * (gen.MAX_MATERIAL_CHARS + 10)}))
        self.assertEqual((st["accepted"], st["shortfall"], st["truncated"]), (1, 4, True))

    def test_material_prompt_asks_for_the_object_json_mode_requires(self):
        seen = []
        gen._chat = lambda messages, *a, **k: (seen.append(messages), "{}")[1]
        self.wait_job(self.start_ai({"mode": "material", "count": 2, "text": "Some notes."}))
        self.assertIn('{"questions": [...]}', seen[0][1]["content"])
        self.assertNotIn("JSON array", seen[0][1]["content"])

    def test_ai_without_a_key_says_so_and_never_calls_the_model(self):
        # setUp's _chat fails the test if called.
        saved = os.environ.pop("TRIVIA_LLM_API_KEY")
        self.addCleanup(os.environ.__setitem__, "TRIVIA_LLM_API_KEY", saved)
        for body in ({"mode": "fill", "text": "Q: Largest ocean on Earth?"},
                     {"mode": "material", "count": 2, "text": "Some notes."}):
            st = self.wait_job(self.start_ai(body))
            self.assertIn("needs a DeepSeek API key", st["error"])
            self.assertIn("without AI", st["error"])
            self.assertEqual(st["accepted"], 0)

    def test_complete_rows_need_no_key_even_through_the_ai_path(self):
        saved = os.environ.pop("TRIVIA_LLM_API_KEY")
        self.addCleanup(os.environ.__setitem__, "TRIVIA_LLM_API_KEY", saved)
        st = self.wait_job(self.start_ai({
            "mode": "fill", "category": "No key",
            "text": "Which planet is largest?\nA) Mars\nB) Jupiter\nC) Venus\nANSWER: B"}))
        self.assertEqual((st["error"], st["accepted"]), (None, 1))

    # ---- job isolation -----------------------------------------------------------------
    def test_import_jobs_do_not_leak_through_other_status_endpoints(self):
        gen._chat = lambda *a, **k: "junk"
        job = self.start_ai({"mode": "fill", "text": "Q: Largest ocean on Earth?"})
        self.wait_job(job)
        status, b = self.req("GET", f"/api/generate/status?job_id={job}")
        self.assertEqual(status, 404, b)
        status, b = self.req("GET", f"/api/configure/status?job_id={job}")
        self.assertEqual(status, 404, b)
        self.assertIsNone(self.app.job_status(job))
        self.assertIsNone(self.app.configure_job_status(job))
        # ... and a configure/generation job is not an import job.
        status, g = self.req("POST", "/api/game/create", {"name": "x"})
        other = self.app.start_generation(g["game_id"], "t", 1)
        self.assertEqual(self.req("GET", f"/api/import/status?job_id={other}")[0], 404)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.req("GET", "/api/import/status?job_id=nope")[0], 404)
        self.assertEqual(self.req("GET", "/api/import/status")[0], 404)

    def test_import_runs_are_audited_without_the_pasted_text(self):
        marker = "zxqv-private-marker"
        gen._chat = lambda *a, **k: f"junk {marker}"
        job = self.start_ai({"mode": "fill", "text": f"Q: Largest ocean {marker}?"})
        self.wait_job(job)
        deadline, rows = time.time() + 5, []
        while time.time() < deadline and not rows:
            conn = db.connect(self.db_path)
            try:
                rows = conn.execute("SELECT topic, status, raw_response, reason FROM gen_audit "
                                    "WHERE job_id = ?", (job,)).fetchall()
            finally:
                conn.close()
            time.sleep(0.05)
        self.assertTrue(rows)
        self.assertEqual({r["topic"] for r in rows}, {"import:fill"})
        self.assertFalse(any(marker in (r["raw_response"] or "") for r in rows))

    # ---- helpers for the game flow -------------------------------------------------------
    def action(self, g, action, params=None):
        status, b = self.req("POST", "/api/action", {"game_id": g["game_id"], "token": g["host_token"],
                                                      "action": action, "params": params or {}}, admin=False)
        self.assertEqual(status, 200, (action, b))

    def wait_state(self, g, pred, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, s = self.req("GET", f"/api/state?game_id={g['game_id']}&role=host"
                                        f"&token={g['host_token']}", admin=False)
            if status == 200 and pred(s):
                return s
            time.sleep(0.05)
        self.fail("state predicate not met")


if __name__ == "__main__":
    unittest.main()
