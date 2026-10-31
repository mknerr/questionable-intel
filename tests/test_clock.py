"""The question clock: pause keeps the question length, reopening a game restarts
a stale clock, and databases damaged by the old pause are repaired on start."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import engine


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "trivia.db")
        db.init_db(self.path)
        self.e = engine.GameEngine(self.path)
        g = self.e.create_game("Clock")
        self.gid, self.token = g["game_id"], g["host_token"]
        self.e.add_questions(self.gid, [
            {"stem": f"Question number {i}?", "answer": "a",
             "options": ["a", "b", "c", "d"], "type": "mc"} for i in range(3)])
        self.e.start_game(self.gid)

    def snap(self):
        return self.e.snapshot(self.gid, "host", self.token)

    def row(self):
        conn = db.connect(self.path)
        try:
            return dict(conn.execute("SELECT * FROM games WHERE id = ?", (self.gid,)).fetchone())
        finally:
            conn.close()

    def set_clock(self, **cols):
        conn = db.connect(self.path)
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE games SET {sets} WHERE id = ?", (*cols.values(), self.gid))
        conn.commit()
        conn.close()

    def expire(self):
        self.set_clock(clock_ends_at=time.time() - 3600)

    def reopen(self):
        self.token = self.e.select_game(self.gid)["host_token"]

    def assert_counting(self, low=28, high=30):
        s = self.snap()
        self.assertTrue(s["clock_running"])
        self.assertTrue(low <= s["clock_remaining"] <= high, s["clock_remaining"])

    # ---- reopening ---------------------------------------------------------
    def test_reopening_restarts_an_expired_clock(self):
        self.expire()
        self.assertEqual(self.snap()["clock_remaining"], 0)
        self.reopen()
        self.assert_counting()
        self.assertFalse(self.snap().get("locked"))

    def test_answers_are_on_time_after_reopening(self):
        team = self.e.join_team(self.gid, "Testers")
        self.expire()
        self.reopen()
        tid = self.e.team_by_token(team["join_token"])["id"]
        self.assertFalse(self.e.submit_answer(tid, self.gid, "a")["is_late"])

    def test_reopening_leaves_a_running_clock_alone(self):
        ends = time.time() + 12
        self.set_clock(clock_ends_at=ends)
        self.reopen()
        self.assertEqual(self.row()["clock_ends_at"], ends)

    def test_reopening_resumes_a_paused_clock(self):
        self.set_clock(clock_ends_at=time.time() + 12)
        self.e.pause_clock(self.gid)
        self.reopen()
        self.assert_counting(10, 12)
        self.assertEqual(self.row()["clock_duration"], 30)

    def test_reopening_does_not_start_a_clock_outside_a_question(self):
        self.e.reveal(self.gid)
        self.reopen()
        self.assertFalse(self.snap()["clock_running"])

    # ---- pause, start, extend ----------------------------------------------
    def test_pause_keeps_the_length_of_later_questions(self):
        self.set_clock(clock_ends_at=time.time() + 5)
        self.e.pause_clock(self.gid)
        self.assertEqual(self.row()["clock_duration"], 30)
        self.e.reveal(self.gid)
        self.e.next_question(self.gid)
        self.assert_counting()

    def test_start_resumes_a_pause_once(self):
        self.set_clock(clock_ends_at=time.time() + 12)
        self.e.pause_clock(self.gid)
        self.assertFalse(self.snap()["clock_running"])
        self.e.start_clock(self.gid)
        self.assert_counting(10, 12)
        self.e.pause_clock(self.gid)
        self.e.reveal(self.gid)
        self.e.next_question(self.gid)
        self.assert_counting()
        self.assertIsNone(self.row()["clock_paused_left"])

    def test_pausing_an_expired_clock_then_starting_gives_a_full_clock(self):
        self.expire()
        self.e.pause_clock(self.gid)
        self.e.start_clock(self.gid)
        self.assert_counting()

    def test_extending_an_expired_clock_counts_from_now(self):
        self.expire()
        self.e.extend_clock(self.gid, 30)
        self.assert_counting()

    def test_extending_a_paused_clock_adds_to_what_was_left(self):
        self.set_clock(clock_ends_at=time.time() + 10)
        self.e.pause_clock(self.gid)
        self.e.extend_clock(self.gid, 30)
        self.assert_counting(38, 40)

    def test_undo_restores_a_paused_clock(self):
        self.set_clock(clock_ends_at=time.time() + 12)
        self.e.pause_clock(self.gid)
        self.e.start_clock(self.gid)
        self.e.undo(self.gid)
        self.assertFalse(self.snap()["clock_running"])
        self.assertTrue(10 <= self.row()["clock_paused_left"] <= 12)

    def test_explicit_duration_sets_the_question_length_but_not_zero(self):
        self.e.start_clock(self.gid, 45)
        self.assertEqual(self.row()["clock_duration"], 45)
        self.assertIsNotNone(self.e.start_clock(self.gid, 0))
        self.assertEqual(self.row()["clock_duration"], 45)


class ClockMigrationTest(unittest.TestCase):
    """A database written before ``clock_paused_left`` existed."""

    def migrated(self, *games):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "trivia.db")
        db.init_db(path)
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE games DROP COLUMN clock_paused_left")
        for i, (state, ends, duration) in enumerate(games):
            conn.execute("INSERT INTO games(name, code, host_token_hash, state, clock_ends_at, "
                         "clock_duration, created_at) VALUES ('g', ?, 'h', ?, ?, ?, 0)",
                         (f"C{i}", state, ends, duration))
        conn.commit()
        conn.close()
        db.init_db(path)
        conn = db.connect(path)
        rows = [dict(r) for r in conn.execute("SELECT * FROM games ORDER BY id")]
        conn.close()
        return path, rows

    def test_game_stuck_at_zero_seconds_is_repaired(self):
        _, rows = self.migrated(("question", time.time() - 300, 0))
        self.assertEqual(rows[0]["clock_duration"], 30)
        self.assertIsNone(rows[0]["clock_paused_left"])

    def test_old_paused_game_keeps_its_time_left_and_gets_the_default_length(self):
        _, rows = self.migrated(("question", None, 12), ("revealed", None, 12), ("lobby", None, 30))
        self.assertEqual((rows[0]["clock_paused_left"], rows[0]["clock_duration"]), (12, 30))
        self.assertEqual((rows[1]["clock_paused_left"], rows[1]["clock_duration"]), (None, 30))
        self.assertEqual((rows[2]["clock_paused_left"], rows[2]["clock_duration"]), (None, 30))

    def test_repair_runs_only_once(self):
        path, _ = self.migrated(("lobby", None, 30))
        conn = sqlite3.connect(path)
        conn.execute("UPDATE games SET clock_duration = 45")
        conn.commit()
        conn.close()
        db.init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT clock_duration FROM games").fetchone()[0], 45)
        conn.close()


if __name__ == "__main__":
    unittest.main()
