"""Scoring unit tests: correct/incorrect scoring, manual override,
tie handling, and undo — pure ``unittest``, no network, temp DB per test.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.db_path)
        self.e = engine.GameEngine(self.db_path)
        g = self.e.create_game("Test Night")
        self.game_id = g["game_id"]
        self.host = g["host_token"]
        self.team_a = self.e.join_team(self.game_id, "Alpha")["team_id"]
        self.team_b = self.e.join_team(self.game_id, "Beta")["team_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _add_mc(self, stem, answer, options=None, points=10):
        options = options or ["Wrong1", "Wrong2", "Wrong3", answer]
        return self.e.add_questions(self.game_id, [{
            "stem": stem, "answer": answer, "options": options,
            "type": "mc", "points": points,
        }])[0]

    def test_correct_and_incorrect_scoring(self):
        q = self._add_mc("What is 2+2?", "4", ["1", "2", "3", "4"])
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "4")
        self.e.submit_answer(self.team_b, self.game_id, "3")
        self.e.reveal(self.game_id)
        totals = self.e.team_totals(self.game_id)
        self.assertEqual(totals.get(self.team_a, 0), 10)
        self.assertEqual(totals.get(self.team_b, 0), 0)

    def test_points_value_respected(self):
        q = self._add_mc("2+2", "4", ["1", "2", "3", "4"], points=25)
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "4")
        self.e.reveal(self.game_id)
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 25)

    def test_manual_override_and_undo(self):
        self.e.award(self.game_id, self.team_a, 30, "manual")
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 30)
        self.e.undo(self.game_id)
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 0)

    def test_undo_reveal_and_advance(self):
        q1 = self._add_mc("Q1?", "A", ["A", "B", "C", "D"])
        self._add_mc("Q2?", "A", ["A", "B", "C", "D"])
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "A")
        self.e.reveal(self.game_id)
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 10)
        self.e.next_question(self.game_id)
        self.e.undo(self.game_id)      # undo advance -> back to Q1, revealed
        snap = self.e.snapshot(self.game_id, "host", self.host)
        self.assertEqual(snap["state"], "revealed")
        self.assertEqual(snap["question"]["id"], q1)
        self.e.undo(self.game_id)      # undo reveal -> question open, no score
        snap = self.e.snapshot(self.game_id, "host", self.host)
        self.assertEqual(snap["state"], "question")
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 0)

    def test_numeric_unique_closest_wins(self):
        q = self.e.add_questions(self.game_id, [{
            "stem": "Year of the Moon landing?", "answer": "1969",
            "type": "numeric", "points": 10,
        }])[0]
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "1970")
        self.e.submit_answer(self.team_b, self.game_id, "2000")
        self.e.reveal(self.game_id)
        totals = self.e.team_totals(self.game_id)
        self.assertEqual(totals.get(self.team_a, 0), 10)
        self.assertEqual(totals.get(self.team_b, 0), 0)

    def test_numeric_tie_no_auto_award(self):
        q = self.e.add_questions(self.game_id, [{
            "stem": "Year?", "answer": "1969", "type": "numeric", "points": 10,
        }])[0]
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "1970")
        self.e.submit_answer(self.team_b, self.game_id, "1968")
        self.e.reveal(self.game_id)
        totals = self.e.team_totals(self.game_id)
        self.assertEqual(totals.get(self.team_a, 0), 0)
        self.assertEqual(totals.get(self.team_b, 0), 0)

    def test_free_text_never_autograded(self):
        q = self.e.add_questions(self.game_id, [{
            "stem": "Which band?", "answer": "The Beatles", "type": "text", "points": 10,
        }])[0]
        self.e.start_game(self.game_id)
        self.e.submit_answer(self.team_a, self.game_id, "the beatles")
        self.e.reveal(self.game_id)
        self.assertEqual(self.e.team_totals(self.game_id).get(self.team_a, 0), 0)
        snap = self.e.snapshot(self.game_id, "host", self.host)
        j = {x["team_id"]: x for x in snap["judging"]}
        self.assertTrue(j[self.team_a]["suggested_correct"])
        self.assertEqual(j[self.team_a]["answer"], "the beatles")

    def test_answer_hidden_from_team_before_reveal(self):
        q = self._add_mc("Secret?", "X", ["X", "Y", "Z", "W"])
        self.e.start_game(self.game_id)
        snap = self.e.snapshot(self.game_id, "team", self._team_token(self.team_a))
        self.assertNotIn("answer", snap["question"])
        self.e.reveal(self.game_id)
        snap = self.e.snapshot(self.game_id, "team", self._team_token(self.team_a))
        self.assertIn("answer", snap["question"])
        self.assertEqual(snap["question"]["answer"], "X")

    def _team_token(self, team_id):
        conn = db.connect(self.db_path)
        try:
            return conn.execute("SELECT join_token FROM teams WHERE id = ?",
                                (team_id,)).fetchone()["join_token"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
