"""Console game management: the games list carries timestamps, and the
console can get from a game back to the start screen."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ListGamesTimestampsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.db_path)
        self.e = engine.GameEngine(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_game_has_created_but_no_last_active(self):
        self.e.create_game("G")
        (g,) = self.e.list_games()
        self.assertIsInstance(g["created_at"], float)
        self.assertIsNone(g["last_active_at"])

    def test_host_action_sets_last_active(self):
        gid = self.e.create_game("G")["game_id"]
        other = self.e.create_game("Other")["game_id"]
        self.assertIsNone(self.e.adlib(gid, "Capital of Australia?", "Canberra"))
        games = {g["id"]: g for g in self.e.list_games()}
        self.assertGreaterEqual(games[gid]["last_active_at"], games[gid]["created_at"])
        # Activity in one game never leaks into another's row.
        self.assertIsNone(games[other]["last_active_at"])


class ConsolePageTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(STATIC, "console.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_game_screen_links_back_to_management(self):
        self.assertIn('onclick="showGate()"', self.html)
        self.assertIn('id="gate-resume"', self.html)

    def test_admin_token_is_a_password_manager_field(self):
        self.assertIn('autocomplete="current-password"', self.html)
        self.assertIn('autocomplete="username"', self.html)
        # Still never persisted by the page itself.
        self.assertNotRegex(self.html, r"localStorage\.setItem\([^)]*[aA]dmin")


if __name__ == "__main__":
    unittest.main()
