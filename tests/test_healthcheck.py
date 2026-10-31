"""Healthcheck regression: ``GET /healthz`` returns 200 and runs ``SELECT 1``.

Spins a real ``ThreadingHTTPServer`` and asserts the endpoint reports a healthy
DB, plus that ``GameEngine.healthcheck`` returns True for a live DB and False
for a path that cannot be opened.
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
import engine
import server


class HealthcheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(cls.db_path)
        app = server.App(cls.db_path)
        cls.httpd = server.make_server(app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_healthz_returns_200_ok(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_engine_healthcheck_true_for_live_db(self):
        e = engine.GameEngine(self.db_path)
        self.assertTrue(e.healthcheck())

    def test_engine_healthcheck_false_for_unopenable_db(self):
        bad = engine.GameEngine(os.path.join(self.tmp.name, "missing", "x.db"))
        self.assertFalse(bad.healthcheck())


if __name__ == "__main__":
    unittest.main(verbosity=2)
