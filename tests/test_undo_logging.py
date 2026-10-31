"""Undo-logging tests: set_points, clock actions, ad-lib and add_question
are each logged to ``action_log`` and are independently undoable (they no longer
make Undo jump back past them to an earlier action).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine


class UndoLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.db_path)
        self.e = engine.GameEngine(self.db_path)
        g = self.e.create_game("Test Night")
        self.game_id = g["game_id"]
        self.host = g["host_token"]
        # Low-level seed (not a logged host action) then start, mirroring how a
        # host pre-seeds questions and begins the game.
        self.e.add_questions(self.game_id, [{
            "stem": "Which planet is known as the Red Planet?",
            "answer": "Mars", "options": ["Venus", "Mars", "Jupiter", "Saturn"],
            "type": "mc", "points": 10,
        }])
        self.e.start_game(self.game_id)

    def tearDown(self):
        self.tmp.cleanup()

    def _snap(self):
        return self.e.snapshot(self.game_id, "host", self.host)

    def _log_actions(self):
        conn = db.connect(self.db_path)
        try:
            return [r["action"] for r in conn.execute(
                "SELECT action FROM action_log WHERE game_id = ? ORDER BY id",
                (self.game_id,))]
        finally:
            conn.close()

    def test_points_override_then_undo(self):
        self.e.set_points(self.game_id, 50)
        self.assertEqual(self._snap()["question"]["points"], 50)
        self.assertIn("set_points", self._log_actions())
        self.e.undo(self.game_id)
        snap = self._snap()
        self.assertEqual(snap["question"]["points"], 10)
        self.assertEqual(snap["state"], "question")

    def test_clock_start_then_undo(self):
        self.e.pause_clock(self.game_id)              # stop the running clock
        self.assertFalse(self._snap()["clock_running"])
        self.e.start_clock(self.game_id, 30)          # start it again
        self.assertTrue(self._snap()["clock_running"])
        self.assertIn("start_clock", self._log_actions())
        self.e.undo(self.game_id)
        snap = self._snap()
        self.assertFalse(snap["clock_running"])
        self.assertEqual(snap["state"], "question")

    def test_adlib_question_then_undo(self):
        self.e.adlib(self.game_id, "What is the capital of France?", "Paris",
                     ["London", "Berlin", "Madrid", "Paris"], 10, "mc")
        snap = self._snap()
        self.assertEqual(snap["state"], "question")
        self.assertEqual(snap["question"]["stem"], "What is the capital of France?")
        self.e.undo(self.game_id)
        snap = self._snap()
        # Reverted to the previously-open question, still in question state.
        self.assertEqual(snap["question"]["stem"], "Which planet is known as the Red Planet?")

    def test_add_question_then_undo(self):
        before = self._snap()
        self.e.add_question(self.game_id, {
            "stem": "Which gas do plants absorb?", "answer": "Carbon dioxide",
            "options": ["Oxygen", "Nitrogen", "Carbon dioxide", "Argon"],
            "type": "mc", "points": 10,
        })
        self.assertIn("add_question", self._log_actions())
        self.e.undo(self.game_id)
        self.assertEqual(self._snap()["round_question_count"],
                         before["round_question_count"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
