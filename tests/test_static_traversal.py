"""Path-traversal regression: ``/static/`` must never serve a file outside
the static directory.

Spins a real ``ThreadingHTTPServer`` on an ephemeral port and sends raw request
lines with ``..`` segments. ``http.client`` does not normalise the path the way
``urllib``/``curl`` do, so the traversal reaches ``_serve_static`` intact.
"""

import http.client
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import server


class StaticTraversalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "trivia.db")
        db.init_db(db_path)
        app = server.App(db_path)
        cls.httpd = server.make_server(app, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, raw_path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", raw_path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_traversal_paths_all_404(self):
        paths = (
            "/static/../data/trivia.db",
            "/static/../app.py",
            "/static/../db.py",
            "/static/../../../../../../../etc/passwd",
            "/static/..%2fdata%2ftrivia.db",
            "/static/%2e%2e/data/trivia.db",
            # extra hardening cases: null byte (must not crash realpath),
            # backslash, and a double-encoded traversal.
            "/static/style.css%00",
            "/static/%00",
            "/static/..\\..\\etc/passwd",
            "/static/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        )
        for path in paths:
            status, _ = self._get(path)
            self.assertEqual(status, 404, f"{path!r} must be 404, got {status}")

    def test_legit_static_file_still_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/static/style.css")
            resp = conn.getresponse()
            body = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "text/css")
            self.assertGreater(len(body), 1000)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
