"""Settings unit tests: resolution precedence,
validation table, and the SSRF reject table. No HTTP server, no generation —
deterministic except the ``nip.io`` case, which resolves to ``169.254.169.254``
here (DNS path).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine
import gen
import settings


class SettingsResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self._saved = {k: os.environ.get(k) for k in
                       ("TRIVIA_LLM_BASE", "TRIVIA_LLM_MODEL", "TRIVIA_LLM_API_KEY")}
        for k in ("TRIVIA_LLM_BASE", "TRIVIA_LLM_MODEL", "TRIVIA_LLM_API_KEY"):
            os.environ.pop(k, None)
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Precedence: db > env > default
    def test_precedence_default_when_nothing_set(self):
        r = settings.resolve(self.db_path)
        self.assertEqual(r["provider"], "deepseek")
        self.assertEqual(r["base_url"], gen.DEFAULT_BASE)
        self.assertEqual(r["model"], gen.DEFAULT_MODEL)
        self.assertIsNone(r["api_key"])
        self.assertEqual(r["source"]["provider"], "default")
        self.assertEqual(r["source"]["base_url"], "default")
        self.assertEqual(r["source"]["model"], "default")
        self.assertEqual(r["source"]["api_key"], "none")

    def test_precedence_env_over_default(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://env.example/v1"
        os.environ["TRIVIA_LLM_MODEL"] = "env-model"
        r = settings.resolve(self.db_path)
        self.assertEqual(r["base_url"], "http://env.example/v1")
        self.assertEqual(r["model"], "env-model")
        self.assertEqual(r["source"]["base_url"], "env")
        self.assertEqual(r["source"]["model"], "env")

    def test_precedence_db_over_env(self):
        os.environ["TRIVIA_LLM_BASE"] = "http://env.example/v1"
        os.environ["TRIVIA_LLM_MODEL"] = "env-model"
        # RFC1918 literal so save() validates without a DNS lookup.
        settings.save(self.db_path, {"base_url": "http://192.168.1.55:11434/v1",
                                     "model": "db-model"})
        r = settings.resolve(self.db_path)
        self.assertEqual(r["base_url"], "http://192.168.1.55:11434/v1")
        self.assertEqual(r["model"], "db-model")
        self.assertEqual(r["source"]["base_url"], "db")
        self.assertEqual(r["source"]["model"], "db")

    def test_precedence_api_key_db_over_env(self):
        os.environ["TRIVIA_LLM_API_KEY"] = "env-key"
        r = settings.resolve(self.db_path)
        self.assertEqual(r["api_key"], "env-key")
        self.assertEqual(r["source"]["api_key"], "env")
        settings.save(self.db_path, {"api_key": "db-key"})
        r = settings.resolve(self.db_path)
        self.assertEqual(r["api_key"], "db-key")
        self.assertEqual(r["source"]["api_key"], "db")

    # writing an empty api_key deletes the override (clear via empty).
    def test_clear_api_key_via_empty_string(self):
        settings.save(self.db_path, {"api_key": "fake-key-clear-me"})
        self.assertEqual(settings.resolve(self.db_path)["api_key"], "fake-key-clear-me")
        settings.save(self.db_path, {"api_key": ""})
        self.assertIsNone(settings.resolve(self.db_path)["api_key"])
        self.assertEqual(settings.resolve(self.db_path)["source"]["api_key"], "none")

    # public_view exposes api_key_set but never the key itself.
    def test_public_view_never_contains_key(self):
        settings.save(self.db_path, {"api_key": "fake-key-secret-value"})
        view = settings.public_view(settings.resolve(self.db_path))
        self.assertNotIn("api_key", view)
        self.assertTrue(view["api_key_set"])
        self.assertIn("presets", view)
        self.assertIn("defaults", view)


class SettingsValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self.addCleanup(self.tmp.cleanup)

    # Validation table: each malformed URL is rejected with a field error.
    def test_validate_rejects_bad_base_urls(self):
        cases = [
            ("ftp://x", "scheme"),
            ("javascript:alert(1)", "scheme"),
            ("http://user:pass@example.com/v1", "userinfo"),
            ("http://example.com/v1?x=1", "query"),
            ("http://" + ("a" * 500) + ".com/v1", "longer than 500"),
        ]
        for url, needle in cases:
            errs = settings.validate({"base_url": url})
            self.assertTrue(errs, f"{url!r} should be rejected")
            self.assertTrue(any(needle in e for e in errs),
                            f"{url!r} -> {errs} (missing {needle!r})")

    def test_validate_rejects_unknown_key(self):
        errs = settings.validate({"bogus": "x"})
        self.assertTrue(any("unknown key: bogus" in e for e in errs))

    def test_validate_provider_must_be_known(self):
        errs = settings.validate({"provider": "not-a-preset"})
        self.assertTrue(any("provider" in e for e in errs))

    def test_validate_api_key_length_and_controls(self):
        self.assertTrue(settings.validate({"api_key": "k" * 501}))
        self.assertTrue(settings.validate({"api_key": "a\nb"}))

    def test_validate_cleartext_http_rejected_for_public_host_with_key(self):
        # 8.8.8.8 is a public (non-RFC1918) address; a bearer key must not cross
        # the internet in the clear (cleartext rule).
        errs = settings.validate({"base_url": "http://8.8.8.8/v1", "api_key": "fake-key-x"})
        self.assertTrue(any("http not allowed" in e for e in errs), errs)
        # https is fine with a key.
        self.assertEqual(settings.validate({"base_url": "https://8.8.8.8/v1",
                                            "api_key": "fake-key-x"}), [])


class SettingsSSRFTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self.addCleanup(self.tmp.cleanup)

    # SSRF reject table. RFC1918 is allowed.
    def test_ssrf_rejects_blocked_targets(self):
        blocked = [
            "http://169.254.169.254/v1",
            "http://127.0.0.1:8130/v1",
            "http://[::1]/v1",
            "http://0.0.0.0:80/",
            "http://[fe80::1]/v1",
            "http://169.254.169.254.nip.io/v1",
        ]
        for url in blocked:
            errs = settings.validate({"base_url": url})
            self.assertTrue(errs, f"{url!r} should be rejected")

    def test_ssrf_ip_literals_report_blocked_target(self):
        for url in ("http://169.254.169.254/v1", "http://127.0.0.1:8130/v1",
                    "http://[::1]/v1", "http://0.0.0.0:80/", "http://[fe80::1]/v1"):
            errs = settings.validate({"base_url": url})
            self.assertTrue(any("blocked target" in e for e in errs),
                            f"{url!r} -> {errs}")

    def test_ssrf_accepts_rfc1918_target(self):
        # The whole point of the app: an on-LAN Ollama endpoint.
        self.assertEqual(settings.validate({"base_url": "http://192.168.1.50:11434/v1"}),
                         [])
        # RFC1918 http + key is allowed (cleartext rule only guards public hosts).
        self.assertEqual(settings.validate({"base_url": "http://192.168.1.50:11434/v1",
                                            "api_key": "fake-key-x"}), [])


class SettingsPermissionsTest(unittest.TestCase):
    # data/ is created 0700 and trivia.db* 0600 at init.
    def test_data_dir_0700_and_db_0600(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "data", "trivia.db")
        db.init_db(db_path)
        self.addCleanup(tmp.cleanup)

        data_dir = os.path.dirname(db_path)
        self.assertEqual(os.stat(data_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(db_path).st_mode & 0o777, 0o600)
        for sidecar in ("trivia.db-wal", "trivia.db-shm"):
            p = os.path.join(data_dir, sidecar)
            if os.path.exists(p):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, sidecar)


class AuditProvenanceTest(unittest.TestCase):
    # the audit summary reason records provider + base host (no key, no path).
    def test_summary_reason_has_provider_and_base_host(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, "trivia.db")
        db.init_db(db_path)
        self.addCleanup(tmp.cleanup)

        e = engine.GameEngine(db_path)
        e.record_generation(job_id="j1", game_id=1, topic="science", model="m",
                            count=3, accepted=1, rejected=0, audit_rows=[],
                            base_host="192.168.1.50:11434", provider="ollama")
        conn = db.connect(db_path)
        try:
            row = conn.execute(
                "SELECT reason FROM gen_audit WHERE status = 'summary'").fetchone()
        finally:
            conn.close()
        self.assertIn("provider=ollama", row["reason"])
        self.assertIn("base_host=192.168.1.50:11434", row["reason"])
        # no key, no path
        self.assertNotIn("fake-key", row["reason"])
        self.assertNotIn("/v1", row["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
