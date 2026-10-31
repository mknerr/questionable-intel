"""DeepSeek as the default LLM provider: defaults and provider inference,
per-endpoint request bodies, JSON-mode reply parsing, blank replies, the
missing-key message, and the server-side no-key short circuit.

No network and no real key: the HTTP transport (``netutil.open_no_redirect``)
is replaced by a strict fake that rejects anything DeepSeek's documented API
would not accept.
"""

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
import gen
import netutil
import server
import settings

LLM_ENV = ("TRIVIA_LLM_BASE", "TRIVIA_LLM_MODEL", "TRIVIA_LLM_API_KEY")

DEEPSEEK_KEYS = {"model", "messages", "stream", "temperature", "max_tokens",
                 "response_format", "reasoning_effort"}
OPENAI_KEYS = {"model", "messages", "stream", "temperature", "max_tokens",
               "response_format"}
OLLAMA_KEYS = {"model", "messages", "stream", "temperature", "max_tokens",
               "reasoning_effort", "format", "options", "think"}

GOOD = [
    ("Which city is the capital of France?", "Paris",
     ["Lyon", "Paris", "Nice", "Lille"]),
    ("Which planet is known as the Red Planet?", "Mars",
     ["Venus", "Jupiter", "Saturn", "Mars"]),
    ("Which element has the chemical symbol Au?", "Gold",
     ["Silver", "Iron", "Gold", "Copper"]),
    ("Which ocean lies between Africa and Australia?", "Indian Ocean",
     ["Atlantic Ocean", "Arctic Ocean", "Indian Ocean", "Pacific Ocean"]),
]


def good(i=0, **over):
    stem, answer, options = GOOD[i]
    q = {"stem": stem, "options": list(options), "answer": answer,
         "confidence": "high", "category": "general"}
    q.update(over)
    return q


def reply(content):
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class _EnvCase(unittest.TestCase):
    """Starts each test with no TRIVIA_LLM_* variables and a fresh DB."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in LLM_ENV:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)

    def _set_rows(self, **rows):
        conn = db.connect(self.db_path)
        try:
            for k, v in rows.items():
                conn.execute(
                    "INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?)",
                    ("llm." + k, v, time.time()))
            conn.commit()
        finally:
            conn.close()


class DefaultsAndInferenceTest(_EnvCase):
    def test_module_defaults(self):
        self.assertEqual(gen.DEFAULT_BASE, "https://api.deepseek.com")
        self.assertEqual(gen.DEFAULT_MODEL, "deepseek-flash")

    def test_default_resolve_is_deepseek(self):
        r = settings.resolve(self.db_path)
        self.assertEqual(r["provider"], "deepseek")
        self.assertEqual(r["base_url"], "https://api.deepseek.com")
        self.assertEqual(r["model"], "deepseek-flash")
        self.assertIsNone(r["api_key"])
        self.assertEqual(r["source"], {"provider": "default", "base_url": "default",
                                       "model": "default", "api_key": "none"})

    def test_presets_order_and_no_personal_address(self):
        self.assertEqual([p["id"] for p in settings.PRESETS],
                         ["deepseek", "ollama", "openai", "custom"])
        by_id = {p["id"]: p for p in settings.PRESETS}
        self.assertEqual(by_id["deepseek"]["label"], "DeepSeek")
        self.assertEqual(by_id["deepseek"]["base_url"], "https://api.deepseek.com")
        self.assertEqual(by_id["deepseek"]["model_hint"], "deepseek-flash")
        self.assertEqual(by_id["ollama"]["base_url"],
                         "http://host.docker.internal:11434/v1")
        self.assertEqual(by_id["ollama"]["model_hint"], "qwen3.5:9b")
        self.assertNotIn("192.168.1.50", json.dumps(settings.PRESETS))

    def test_provider_error_lists_every_preset(self):
        errs = settings.validate({"provider": "nope"})
        self.assertEqual(len(errs), 1)
        for pid in ("deepseek", "ollama", "openai", "custom"):
            self.assertIn(pid, errs[0])

    def test_env_base_for_unknown_server_reports_custom_and_needs_no_key(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://myollama:11434/v1"
        r = settings.resolve(self.db_path)
        self.assertEqual(r["provider"], "custom")
        self.assertEqual(r["source"]["provider"], "default")
        self.assertEqual(r["source"]["base_url"], "env")
        self.assertEqual(r["model"], gen.DEFAULT_MODEL)  # nothing better is known
        self.assertIsNone(settings.missing_key_error(r))

    def test_env_base_matching_a_preset_infers_it_and_its_model(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://host.docker.internal:11434/v1/"
        r = settings.resolve(self.db_path)
        self.assertEqual(r["provider"], "ollama")
        self.assertEqual(r["source"]["provider"], "default")
        self.assertEqual(r["model"], "qwen3.5:9b")
        self.assertEqual(r["source"]["model"], "default")
        self.assertIsNone(settings.missing_key_error(r))

    def test_db_base_matching_openai_uses_openai_model_hint(self):
        self._set_rows(base_url="https://api.openai.com/v1")
        r = settings.resolve(self.db_path)
        self.assertEqual(r["provider"], "openai")
        self.assertEqual(r["model"], "gpt-4o-mini")
        self.assertEqual(r["source"]["base_url"], "db")

    def test_explicit_model_always_wins_over_the_fallback(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://host.docker.internal:11434/v1"
        os.environ["TRIVIA_LLM_MODEL"] = "env-model"
        self.assertEqual(settings.resolve(self.db_path)["model"], "env-model")
        self._set_rows(model="db-model")
        r = settings.resolve(self.db_path)
        self.assertEqual((r["model"], r["source"]["model"]), ("db-model", "db"))

    def test_db_provider_row_is_authoritative(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://myollama:11434/v1"
        self._set_rows(provider="ollama")
        r = settings.resolve(self.db_path)
        self.assertEqual((r["provider"], r["source"]["provider"]), ("ollama", "db"))

    def test_public_view_is_consistent_with_resolve(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://host.docker.internal:11434/v1"
        v = settings.public_view(settings.resolve(self.db_path))
        self.assertEqual((v["provider"], v["model"]), ("ollama", "qwen3.5:9b"))
        self.assertEqual(v["source"]["provider"], "default")
        self.assertEqual(v["defaults"], {"provider": "deepseek",
                                         "base_url": "https://api.deepseek.com",
                                         "model": "deepseek-flash"})
        self.assertFalse(v["api_key_set"])
        self.assertNotIn("api_key", v)
        self.assertEqual([p["id"] for p in v["presets"]][0], "deepseek")


class MissingKeyErrorTest(unittest.TestCase):
    def test_deepseek_without_key(self):
        msg = settings.missing_key_error(
            {"base_url": "https://api.deepseek.com", "api_key": None})
        self.assertIsInstance(msg, str)
        for needle in ("DeepSeek", "API key", "platform.deepseek.com",
                       "AI settings", "TRIVIA_LLM_API_KEY", "paste", "import"):
            self.assertIn(needle, msg)

    def test_openai_without_key(self):
        msg = settings.missing_key_error(
            {"base_url": "https://api.openai.com/v1", "api_key": ""})
        self.assertIn("OpenAI", msg)
        self.assertIn("AI settings", msg)
        self.assertIn("TRIVIA_LLM_API_KEY", msg)
        self.assertIn("import", msg)

    def test_blank_key_counts_as_missing(self):
        self.assertIsNotNone(settings.missing_key_error(
            {"base_url": "https://api.deepseek.com", "api_key": "   "}))

    def test_key_set_gives_none(self):
        for base in ("https://api.deepseek.com", "https://api.openai.com/v1"):
            self.assertIsNone(settings.missing_key_error(
                {"base_url": base, "api_key": "k"}))

    def test_other_hosts_give_none(self):
        for base in ("http://host.docker.internal:11434/v1",
                     "http://192.168.1.50:11434/v1",
                     "https://api.deepseek.com.evil.test/v1",
                     "https://evil.test/api.deepseek.com",
                     "https://example.org/v1", ""):
            self.assertIsNone(settings.missing_key_error(
                {"base_url": base, "api_key": None}), base)

    def test_host_match_ignores_case_and_trailing_slash(self):
        self.assertIsNotNone(settings.missing_key_error(
            {"base_url": "HTTPS://API.DeepSeek.com/", "api_key": None}))


class RequestBodyTest(unittest.TestCase):
    def _sent(self, base, api_key="k"):
        seen = {}

        def fake_open(url, *, data=None, headers=None, timeout=5.0, max_bytes=0):
            seen.update(url=url, body=json.loads(data), headers=headers)
            return 200, reply('{"questions": []}')
        with mock.patch("netutil.open_no_redirect", side_effect=fake_open):
            out = gen._chat([{"role": "user", "content": "give JSON"}],
                            "the-model", base, api_key, 10, 777)
        self.assertEqual(out, '{"questions": []}')
        return seen

    def test_deepseek_body_is_exactly_the_documented_fields(self):
        seen = self._sent("https://api.deepseek.com")
        self.assertEqual(seen["url"], "https://api.deepseek.com/chat/completions")
        b = seen["body"]
        self.assertEqual(set(b), DEEPSEEK_KEYS)
        self.assertEqual(b["model"], "the-model")
        self.assertIs(b["stream"], False)
        self.assertEqual(b["temperature"], 0.4)
        self.assertEqual(b["max_tokens"], 777)
        self.assertEqual(b["response_format"], {"type": "json_object"})
        self.assertEqual(b["reasoning_effort"], "none")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer k")

    def test_deepseek_matched_by_hostname_not_case_or_slash(self):
        for base in ("https://API.DEEPSEEK.COM/", "https://api.deepseek.com:443"):
            self.assertEqual(set(self._sent(base)["body"]), DEEPSEEK_KEYS, base)

    def test_openai_body_is_exactly_the_plain_fields(self):
        seen = self._sent("https://api.openai.com/v1")
        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        b = seen["body"]
        self.assertEqual(set(b), OPENAI_KEYS)
        self.assertEqual(b["response_format"], {"type": "json_object"})
        self.assertEqual(b["max_tokens"], 777)
        self.assertNotIn("reasoning_effort", b)

    def test_ollama_and_custom_bodies_keep_the_legacy_fields(self):
        for base in ("http://host.docker.internal:11434/v1",
                     "http://192.168.1.50:11434/v1",
                     "https://llm.example.org/v1",
                     # substring tricks must not select a hosted-API body
                     "https://api.deepseek.com.evil.test/v1",
                     "https://api.openai.com.evil.test/v1",
                     "https://evil.test/api.deepseek.com/v1",
                     "http://localhost:11434/v1?h=api.openai.com"):
            b = self._sent(base)["body"]
            self.assertEqual(set(b), OLLAMA_KEYS, base)
            self.assertEqual(b["format"], "json")
            self.assertEqual(b["options"], {"num_predict": 777, "temperature": 0.4})
            self.assertIs(b["think"], False)
            self.assertEqual(b["reasoning_effort"], "none")
            self.assertNotIn("response_format", b)

    def test_ollama_body_is_byte_for_byte_the_historical_one(self):
        legacy = {
            "model": "m", "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.4, "stream": False, "format": "json",
            "options": {"num_predict": 55, "temperature": 0.4},
            "max_tokens": 55, "reasoning_effort": "none", "think": False}
        now = gen._request_body([{"role": "user", "content": "x"}], "m",
                                "http://host.docker.internal:11434/v1", 55)
        self.assertEqual(json.dumps(now), json.dumps(legacy))

    def test_no_authorization_header_without_a_key(self):
        seen = self._sent("http://192.168.1.50:11434/v1", api_key=None)
        self.assertNotIn("Authorization", seen["headers"])


class ParsingTest(unittest.TestCase):
    def setUp(self):
        orig = gen._chat
        self.addCleanup(setattr, gen, "_chat", orig)

    def _patch_chat(self, fn):
        gen._chat = fn

    def test_question_items_shapes(self):
        q = good(0)
        self.assertEqual(gen._question_items([q]), [q])
        self.assertEqual(gen._question_items({"questions": [q]}), [q])
        self.assertEqual(gen._question_items({"questions": []}), [])
        self.assertEqual(gen._question_items(q), [q])
        self.assertEqual(gen._one_question({"questions": [q]}), q)
        self.assertEqual(gen._one_question([q]), q)
        self.assertEqual(gen._one_question(q), q)

    def test_wrapper_and_bare_array_and_lone_object_all_generate(self):
        shapes = {
            "wrapper": lambda qs: {"questions": qs},
            "bare": lambda qs: qs,
            "lone": lambda qs: qs[0],
        }
        for name, shape in shapes.items():
            n = 1 if name == "lone" else 3
            self._patch_chat(lambda *a, **k: json.dumps(shape([good(i) for i in range(n)])))
            out = gen.generate_questions("general", n, max_calls=1)
            self.assertEqual(len(out["accepted"]), n, name)
            self.assertEqual(out["shortfall"], 0, name)

    def test_wrapper_in_markdown_fence(self):
        self._patch_chat(lambda *a, **k:
                         "```json\n" + json.dumps({"questions": [good(0)]}) + "\n```")
        self.assertEqual(len(gen.generate_questions("general", 1)["accepted"]), 1)

    def test_empty_wrapper_is_a_shortfall_not_a_crash(self):
        self._patch_chat(lambda *a, **k: '{"questions": []}')
        out = gen.generate_questions("general", 2, max_calls=3)
        self.assertEqual(out["accepted"], [])
        self.assertEqual(out["shortfall"], 2)

    def test_corrective_retry_accepts_wrapper_with_one_object(self):
        bad = good(0, answer="Berlin")  # not among the options
        replies = [json.dumps({"questions": [bad]}),
                   json.dumps({"questions": [good(0)]})]
        seen = []

        def fake(messages, *a, **k):
            seen.append(messages[-1]["content"])
            return replies[len(seen) - 1]
        self._patch_chat(fake)
        out = gen.generate_questions("general", 1)
        self.assertEqual(len(out["accepted"]), 1)
        self.assertEqual(out["accepted"][0]["answer"], "Paris")
        self.assertEqual(len(seen), 2)

    def test_corrective_retry_accepts_a_bare_object(self):
        replies = [json.dumps([good(0, answer="Berlin")]), json.dumps(good(0))]
        n = []
        self._patch_chat(lambda *a, **k: replies[len(n)] + (n.append(1) or ""))
        self.assertEqual(len(gen.generate_questions("general", 1)["accepted"]), 1)

    def test_prompts_ask_for_a_questions_object(self):
        msgs = []
        self._patch_chat(lambda m, *a, **k: msgs.append(m) or '{"questions": []}')
        gen.generate_questions("general", 2, max_calls=2)
        for m in msgs:
            text = " ".join(x["content"] for x in m)
            self.assertIn("JSON", text)
            self.assertIn('"questions"', text)


class BlankContentTest(unittest.TestCase):
    def _chat_with(self, content):
        resp = json.dumps({"choices": [{"message": {"content": content},
                                        "finish_reason": "length"}]}).encode()
        with mock.patch("netutil.open_no_redirect", return_value=(200, resp)):
            return gen._chat([{"role": "user", "content": "x"}], "m",
                             "https://api.deepseek.com", "k", 10, 100)

    def test_blank_and_null_content_raise_empty_reply(self):
        for content in ("", "   \n\t  ", None, 12, ["a"]):
            with self.assertRaises(gen.EmptyReplyError, msg=repr(content)):
                self._chat_with(content)

    def test_missing_content_key_is_still_a_keyerror(self):
        resp = json.dumps({"choices": [{"message": {}}]}).encode()
        with mock.patch("netutil.open_no_redirect", return_value=(200, resp)):
            with self.assertRaises(KeyError):
                gen._chat([], "m", "https://api.deepseek.com", "k", 10, 100)

    def test_blank_reply_is_a_logged_failed_call_not_a_crash(self):
        for content in ("          ", None):
            resp = reply(content)
            with mock.patch("netutil.open_no_redirect", return_value=(200, resp)):
                out = gen.generate_questions(
                    "general", 2, base="https://api.deepseek.com", api_key="k")
            self.assertEqual(out["accepted"], [])
            self.assertEqual(out["shortfall"], 2)
            errors = [a for a in out["audit"] if a["status"] == "error"]
            self.assertTrue(errors, content)
            self.assertIn("EmptyReplyError", errors[0]["reason"])
            self.assertNotIn("AttributeError", json.dumps(out["audit"]))

    def test_blank_retry_reply_does_not_crash_either(self):
        replies = [json.dumps({"questions": [good(0, answer="Berlin")]}), "   "]
        calls = []

        def fake(*a, **k):
            calls.append(1)
            r = replies[min(len(calls) - 1, 1)]
            if not r.strip():
                raise gen.EmptyReplyError("model returned an empty reply")
            return r
        with mock.patch.object(gen, "_chat", fake):
            out = gen.generate_questions("general", 1, max_calls=3)
        self.assertEqual(out["accepted"], [])
        self.assertTrue(any(a["status"] == "error" for a in out["audit"]))


class StrictDeepSeekFake:
    """Stands in for ``netutil.open_no_redirect`` and behaves like a picky
    DeepSeek: unknown params, a wrong model id, a missing key, or JSON mode
    without the word "json" in the prompt are recorded as violations (and the
    latter blanks the reply with spaces, as the real API does)."""

    def __init__(self, questions):
        self.questions = questions
        self.violations = []
        self.calls = 0

    def __call__(self, url, *, data=None, headers=None, timeout=5.0, max_bytes=0):
        self.calls += 1
        body = json.loads(data)
        if url != "https://api.deepseek.com/chat/completions":
            self.violations.append(f"url {url}")
        extra = set(body) - DEEPSEEK_KEYS
        if extra:
            self.violations.append(f"unknown params {sorted(extra)}")
        if body.get("model") not in ("deepseek-flash", "deepseek-v4-pro"):
            self.violations.append(f"model {body.get('model')!r}")
        if (headers or {}).get("Authorization") != "Bearer test-key":
            self.violations.append("missing/incorrect bearer key")
        if body.get("reasoning_effort") not in ("none", "low", "high", "max"):
            self.violations.append("bad reasoning_effort")
        if not isinstance(body.get("max_tokens"), int):
            self.violations.append("max_tokens")
        rf = body.get("response_format")
        if rf != {"type": "json_object"}:
            self.violations.append("response_format")
        prompt = " ".join(m["content"] for m in body["messages"]).lower()
        if "json" not in prompt:
            self.violations.append("json mode without 'json' in prompt")
            return 200, reply(" " * 50)
        return 200, reply(json.dumps({"questions": self.questions}))


class StrictFakeEndToEndTest(unittest.TestCase):
    def test_generate_questions_against_strict_deepseek(self):
        fake = StrictDeepSeekFake([good(i) for i in range(3)])
        with mock.patch("netutil.open_no_redirect", side_effect=fake):
            out = gen.generate_questions("general knowledge", 3, api_key="test-key")
        self.assertEqual(fake.violations, [])
        self.assertEqual(len(out["accepted"]), 3)
        self.assertEqual(out["shortfall"], 0)
        self.assertEqual(out["rejected"], [])
        self.assertEqual(fake.calls, 1)
        self.assertEqual({a["model"] for a in out["audit"]}, {"deepseek-flash"})

    def test_strict_fake_does_flag_the_old_ollama_body(self):
        fake = StrictDeepSeekFake([])
        body = gen._request_body([{"role": "user", "content": "json"}],
                                 "deepseek-flash", "http://192.168.1.50:11434/v1", 10)
        fake("https://api.deepseek.com/chat/completions",
             data=json.dumps(body).encode(), headers={"Authorization": "Bearer test-key"})
        self.assertTrue(any("unknown params" in v for v in fake.violations))

    def test_retry_and_topup_prompts_also_satisfy_json_mode(self):
        # Answer 1 is bad -> corrective retry; shortfall -> top-up. Every one of
        # those requests must pass the strict checks.
        qs = [good(0, answer="Berlin")]
        fake = StrictDeepSeekFake(qs)
        with mock.patch("netutil.open_no_redirect", side_effect=fake):
            gen.generate_questions("general knowledge", 2, api_key="test-key")
        self.assertGreaterEqual(fake.calls, 3)
        self.assertEqual(fake.violations, [])


class ServerNoKeyTest(_EnvCase):
    """With DeepSeek selected and no key, the server must fail the job/round
    with the readable message and never reach the generator or the network."""

    def setUp(self):
        super().setUp()
        self.app = server.App(self.db_path)
        self.game = self.app.engine.create_game("G")
        self.chat_calls = []
        self.net_calls = []
        p1 = mock.patch.object(gen, "_chat",
                               side_effect=lambda *a, **k: self.chat_calls.append(a) or "{}")
        p2 = mock.patch("netutil.open_no_redirect",
                        side_effect=lambda *a, **k: self.net_calls.append(a))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def _wait(self, fn, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.02)
        self.fail("timed out")

    def _db_scalar(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def test_generation_job_carries_message_and_makes_no_call(self):
        expected = settings.missing_key_error(settings.resolve(self.db_path))
        self.assertIn("DeepSeek", expected)
        job = self.app.start_generation(self.game["game_id"], "science", 3)
        status = self._wait(lambda: (lambda s: s if s and s["done"] else None)(
            self.app.job_status(job)))
        self.assertEqual(status["error"], expected)
        self.assertEqual(status["accepted"], 0)
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.net_calls, [])
        # the failure is audited with the message, and no key was stored
        rows = self._wait(lambda: self._db_scalar(
            "SELECT status, reason FROM gen_audit WHERE job_id = ?", (job,)))
        self.assertEqual(rows[0][0], "error")
        self.assertIn(expected, rows[0][1])
        self.assertEqual(self._db_scalar(
            "SELECT 1 FROM app_settings WHERE key = 'llm.api_key'"), [])

    def test_configure_round_fails_with_message_and_makes_no_call(self):
        expected = settings.missing_key_error(settings.resolve(self.db_path))
        errors, steps = self.app.engine.configure_game(
            self.game["game_id"], [{"category": "Space", "question_count": 2},
                                   {"category": "Food", "question_count": 2}])
        self.assertEqual(errors, [])
        job = self.app.start_configure(self.game["game_id"], steps)
        status = self._wait(lambda: (lambda s: s if s and s["done"] else None)(
            self.app.configure_job_status(job)))
        for pos in ("1", "2"):
            self.assertEqual(status["rounds"][pos]["status"], "failed")
            self.assertEqual(status["rounds"][pos]["error"], expected)
        stored = self._db_scalar("SELECT status, error FROM rounds WHERE game_id = ?",
                                 (self.game["game_id"],))
        self.assertEqual({r[0] for r in stored}, {"failed"})
        self.assertEqual({r[1] for r in stored}, {expected})
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.net_calls, [])

    def test_with_a_key_the_generator_is_reached(self):
        os.environ["TRIVIA_LLM_API_KEY"] = "test-key"
        job = self.app.start_generation(self.game["game_id"], "science", 3)
        self._wait(lambda: (lambda s: s if s and s["done"] else None)(
            self.app.job_status(job)))
        self.assertTrue(self.chat_calls)

    def test_local_base_url_needs_no_key(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://host.docker.internal:11434/v1"
        job = self.app.start_generation(self.game["game_id"], "science", 3)
        self._wait(lambda: (lambda s: s if s and s["done"] else None)(
            self.app.job_status(job)))
        self.assertTrue(self.chat_calls)
        # ...and the model sent is the Ollama preset's, not deepseek-flash
        self.assertEqual(self.chat_calls[0][1], "qwen3.5:9b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
