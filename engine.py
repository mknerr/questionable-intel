"""Questionable Intel game engine: state machine, scoring, undo.

Totals are derived from ``score_events``; every host action that mutates state
is logged to ``action_log`` so a 5-deep named undo is exact. All
writes are serialised; SQLite runs in WAL mode (see ``db``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time

import db

# Colorblind-safe palette (Okabe–Ito inspired), paired with a shape marker.
COLOR_PALETTE = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # green
    "#D55E00",  # vermillion
    "#CC79A7",  # pink
    "#56B4E9",  # sky blue
    "#F0E442",  # yellow
    "#8B5A2B",  # brown
]
SHAPES = ["◆", "●", "▲", "■", "★", "◆", "●", "▲"]

CODE_ALPHABET = "ABCDEFGHJKMNPQRTUVWXYZ234679"  # no O/0 I/1 L/1 S/5 B/8

STATES = ("lobby", "question", "revealed", "round_end", "final", "finished")


def norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


MAX_ROUNDS = 12
# Teams per game. Joining needs only the room code, which is on the TV, so cap
# how many teams one code can mint.
MAX_TEAMS = 40
# Digits in a team's rejoin PIN. Rejoining from another phone needs the PIN,
# not just the team id the join page lists (anyone with the room code sees
# that list). 6 digits behind the per-IP join limit is ~6 days per guess run.
REJOIN_PIN_DIGITS = 6
DEFAULT_QUESTION_COUNT = 10
MIN_QUESTION_COUNT = 1
MAX_QUESTION_COUNT = 40

ROUND_STATUSES = ("pending", "generating", "ready", "failed")


def validate_rounds_spec(rounds, library_ids: set[int] | None = None) -> list[str]:
    """Validate a ``POST /api/game/configure`` ``rounds`` list; return errors ([] = ok).

    Each entry is exactly one of ``{category:str, question_count?:int}`` (default
    question_count 10, range 1..40) or ``{library_round_id:int}``. Unknown keys,
    malformed values, out-of-bounds counts and unknown library ids are rejected.
    """
    errors: list[str] = []
    if not isinstance(rounds, list) or not rounds:
        return ["rounds: must be a non-empty array"]
    if len(rounds) > MAX_ROUNDS:
        errors.append(f"rounds: at most {MAX_ROUNDS} rounds")
    library_ids = library_ids if library_ids is not None else set()
    for i, entry in enumerate(rounds):
        pos = i + 1
        if not isinstance(entry, dict):
            errors.append(f"round {pos}: must be an object")
            continue
        keys = set(entry.keys())
        has_cat = "category" in entry
        has_lib = "library_round_id" in entry
        if has_cat and has_lib:
            errors.append(f"round {pos}: provide category or library_round_id, not both")
            continue
        if has_cat:
            unknown = keys - {"category", "question_count"}
            if unknown:
                errors.append(f"round {pos}: unknown key(s): {', '.join(sorted(unknown))}")
            cat = entry["category"]
            if not isinstance(cat, str) or not cat.strip():
                errors.append(f"round {pos}: category must be a non-empty string")
            qc = entry.get("question_count", DEFAULT_QUESTION_COUNT)
            if (isinstance(qc, bool) or not isinstance(qc, int)
                    or not (MIN_QUESTION_COUNT <= qc <= MAX_QUESTION_COUNT)):
                errors.append(
                    f"round {pos}: question_count must be an integer "
                    f"{MIN_QUESTION_COUNT}..{MAX_QUESTION_COUNT}")
        elif has_lib:
            if keys != {"library_round_id"}:
                unknown = keys - {"library_round_id"}
                errors.append(f"round {pos}: unknown key(s): {', '.join(sorted(unknown))}")
            lid = entry["library_round_id"]
            if isinstance(lid, bool) or not isinstance(lid, int):
                errors.append(f"round {pos}: library_round_id must be an integer")
            elif lid not in library_ids:
                errors.append(f"round {pos}: unknown library_round_id")
        else:
            errors.append(f"round {pos}: provide category or library_round_id")
    return errors


class GameEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._wlock = threading.Lock()

    # ---- connection helpers -------------------------------------------------
    def _conn(self):
        return db.connect(self.db_path)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def healthcheck(self) -> bool:
        """Return True when a fresh connection can run ``SELECT 1``."""
        try:
            conn = self._conn()
            try:
                conn.execute("SELECT 1")
                return True
            finally:
                conn.close()
        except Exception:
            return False

    # ---- game lifecycle ----------------------------------------------------
    def create_game(self, name: str = "Trivia Night") -> dict:
        with self._wlock:
            conn = self._conn()
            try:
                code = self._new_code(conn)
                host_token = secrets.token_urlsafe(32)
                now = time.time()
                cur = conn.execute(
                    "INSERT INTO games(name, code, host_token_hash, state, created_at) "
                    "VALUES (?, ?, ?, 'lobby', ?)",
                    (name, code, self._hash(host_token), now),
                )
                game_id = cur.lastrowid
                conn.commit()
                return {"game_id": game_id, "code": code, "host_token": host_token,
                        "name": name}
            finally:
                conn.close()

    def _new_code(self, conn) -> str:
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
            row = conn.execute("SELECT 1 FROM games WHERE code = ?", (code,)).fetchone()
            if not row:
                return code
        raise RuntimeError("could not allocate a unique room code")

    def verify_host(self, game_id: int, token: str) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT host_token_hash FROM games WHERE id = ?", (game_id,)).fetchone()
            if not row:
                return False
            return hmac.compare_digest(row["host_token_hash"], self._hash(token))
        finally:
            conn.close()

    def join_team(self, game_id: int, name: str, team_id: int | None = None,
                  pin: str | None = None) -> dict:
        """Create a team, or rejoin one. Rejoining needs the team's PIN: the
        team id alone is public to anyone holding the room code."""
        with self._wlock:
            conn = self._conn()
            try:
                game = conn.execute("SELECT state FROM games WHERE id = ?",
                                    (game_id,)).fetchone()
                if not game:
                    raise ValueError("no such game")
                if team_id is not None:
                    row = conn.execute("SELECT * FROM teams WHERE id = ? AND game_id = ?",
                                       (team_id, game_id)).fetchone()
                    # One message for "no team" and "wrong PIN": no oracle.
                    stored = (row["rejoin_pin"] if row else None) or ""
                    if not stored or not hmac.compare_digest(stored, str(pin or "").strip()):
                        raise ValueError("team or PIN not recognised")
                    return {"team_id": row["id"], "name": row["name"],
                            "join_token": row["join_token"],
                            "rejoin_pin": row["rejoin_pin"], "is_rejoin": True}
                name = (name or "").strip()[:40] or "Team"
                color = conn.execute("SELECT COUNT(*) AS n FROM teams WHERE game_id = ?",
                                     (game_id,)).fetchone()["n"]
                if color >= MAX_TEAMS:
                    raise ValueError("this game is full")
                token = secrets.token_urlsafe(24)
                pin = f"{secrets.randbelow(10 ** REJOIN_PIN_DIGITS):0{REJOIN_PIN_DIGITS}d}"
                cur = conn.execute(
                    "INSERT INTO teams(game_id, name, join_token, rejoin_pin, color_index, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (game_id, name, token, pin, color % len(COLOR_PALETTE), time.time()),
                )
                conn.commit()
                return {"team_id": cur.lastrowid, "name": name, "join_token": token,
                        "rejoin_pin": pin, "is_rejoin": False}
            finally:
                conn.close()

    def list_teams(self, game_id: int) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, name, color_index FROM teams WHERE game_id = ? ORDER BY id",
                (game_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def rename_team(self, game_id: int, team_id: int, name: str) -> bool:
        with self._wlock:
            conn = self._conn()
            try:
                name = (name or "").strip()[:40] or "Team"
                cur = conn.execute("UPDATE teams SET name = ? WHERE id = ? AND game_id = ?",
                                   (name, team_id, game_id))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def team_by_token(self, token: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM teams WHERE join_token = ?", (token,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def game_by_code(self, code: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM games WHERE code = ?",
                               (code.strip().upper(),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def game_exists(self, game_id: int) -> bool:
        conn = self._conn()
        try:
            return conn.execute("SELECT 1 FROM games WHERE id = ?",
                                (game_id,)).fetchone() is not None
        finally:
            conn.close()

    # ---- rounds & questions ------------------------------------------------
    def ensure_round(self, game_id: int, position: int = 1, title: str | None = None) -> int:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id FROM rounds WHERE game_id = ? AND position = ?",
                (game_id, position)).fetchone()
            if row:
                return row["id"]
            cur = conn.execute(
                "INSERT INTO rounds(game_id, position, title) VALUES (?, ?, ?)",
                (game_id, position, title))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _insert_questions(self, conn, game_id: int, questions: list[dict],
                          round_id: int) -> list[int]:
        """Insert question rows into *round_id* on the open *conn*. No locking,
        no commit — the caller owns the transaction and lock."""
        ids = []
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS m FROM questions WHERE round_id = ?",
            (round_id,)).fetchone()["m"]
        for q in questions:
            pos += 1
            qtype = q.get("type", "mc")
            options = json.dumps(q.get("options")) if q.get("options") else None
            cur = conn.execute(
                "INSERT INTO questions(game_id, round_id, position, type, stem, "
                "options, answer, points, media_type, media_url, source, confidence, "
                "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ready', ?)",
                (game_id, round_id, pos, qtype, q["stem"], options, q["answer"],
                 int(q.get("points", 10)), q.get("media_type", "none"),
                 q.get("media_url"), q.get("source", "authored"),
                 q.get("confidence"), time.time()),
            )
            ids.append(cur.lastrowid)
        return ids

    def add_questions(self, game_id: int, questions: list[dict],
                      round_id: int | None = None) -> list[int]:
        """Insert authored/generated questions into a round; return ids."""
        with self._wlock:
            conn = self._conn()
            try:
                if round_id is None:
                    round_id = self.ensure_round(game_id, 1)
                ids = self._insert_questions(conn, game_id, questions, round_id)
                # A round that has gained questions is playable (legacy paths
                # that seed round 1 without going through configure).
                conn.execute("UPDATE rounds SET status = 'ready' WHERE id = ? "
                             "AND status != 'ready'", (round_id,))
                conn.commit()
                return ids
            finally:
                conn.close()

    def add_question(self, game_id: int, question: dict) -> str | None:
        """Add one authored question as a logged, undoable host action."""
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if not g:
                    return "no such game"
                round_id = g["current_round_id"] or self.ensure_round(game_id, 1)
                ids = self._insert_questions(conn, game_id, [question], round_id)
                conn.execute("UPDATE rounds SET status = 'ready' WHERE id = ? "
                             "AND status != 'ready'", (round_id,))
                self._log(conn, game_id, "add_question", "added a question",
                          {"question_ids": ids})
                conn.commit()
                return None
            finally:
                conn.close()

    def _current_question(self, conn, game_id: int) -> dict | None:
        g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not g or g["current_question_id"] is None:
            return None
        q = conn.execute("SELECT * FROM questions WHERE id = ?",
                         (g["current_question_id"],)).fetchone()
        return dict(q) if q else None

    def _question_at(self, conn, round_id: int, after_position: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM questions WHERE round_id = ? AND position > ? AND status != 'voided' "
            "ORDER BY position LIMIT 1", (round_id, after_position)).fetchone()
        return dict(row) if row else None

    def _first_question(self, conn, round_id: int) -> dict | None:
        row = conn.execute(
            "SELECT * FROM questions WHERE round_id = ? AND status != 'voided' "
            "ORDER BY position LIMIT 1", (round_id,)).fetchone()
        return dict(row) if row else None

    def _question_count(self, conn, round_id: int) -> int:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE round_id = ? AND status != 'voided'",
            (round_id,)).fetchone()["n"]

    def _open_question(self, conn, game_id: int, question_id: int, duration: int) -> None:
        conn.execute(
            "UPDATE games SET state = 'question', current_question_id = ?, "
            "clock_ends_at = ?, clock_duration = ?, clock_paused_left = NULL WHERE id = ?",
            (question_id, time.time() + duration, duration, game_id))

    def _log(self, conn, game_id: int, action: str, label: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO action_log(game_id, action, label, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (game_id, action, label, json.dumps(payload), time.time()))

    # ---- host actions ------------------------------------------------------
    def start_game(self, game_id: int) -> str | None:
        """Begin the game: open round 1, first question. Returns error or None."""
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if not g:
                    return "no such game"
                if g["state"] != "lobby":
                    return "already started"
                round_id = self.ensure_round(game_id, 1)
                first = self._first_question(conn, round_id)
                if not first:
                    return "no questions yet"
                conn.execute("UPDATE games SET current_round_id = ? WHERE id = ?",
                             (round_id, game_id))
                self._open_question(conn, game_id, first["id"], g["clock_duration"])
                self._log(conn, game_id, "start", "started the game",
                          {"prev_state": "lobby"})
                conn.commit()
                return None
            finally:
                conn.close()

    def reveal(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] != "question":
                    return "not in question state"
                q = self._current_question(conn, game_id)
                if not q:
                    return "no current question"
                prev_state = g["state"]
                prev_clock = g["clock_ends_at"]
                event_ids = self._auto_score(conn, game_id, q)
                conn.execute("UPDATE games SET state = 'revealed', clock_ends_at = NULL "
                             "WHERE id = ?", (game_id,))
                self._log(conn, game_id, "reveal", "revealed the answer",
                          {"question_id": q["id"], "score_event_ids": event_ids,
                           "prev_state": prev_state, "prev_clock": prev_clock})
                conn.commit()
                return None
            finally:
                conn.close()

    def _auto_score(self, conn, game_id: int, q: dict) -> list[int]:
        """Award points for MC/numeric on reveal. Returns created event ids."""
        teams = conn.execute("SELECT * FROM teams WHERE game_id = ?", (game_id,)).fetchall()
        answers = conn.execute(
            "SELECT * FROM answers WHERE question_id = ? AND is_late = 0",
            (q["id"],)).fetchall()
        ans_by_team = {a["team_id"]: a["answer_text"] for a in answers}
        event_ids = []
        if q["type"] == "mc":
            correct = norm(q["answer"])
            for t in teams:
                if t["id"] in ans_by_team and norm(ans_by_team[t["id"]]) == correct:
                    event_ids.append(self._score(conn, game_id, t["id"], q, q["points"],
                                                 "auto_correct"))
        elif q["type"] == "numeric":
            true_val = _to_float(q["answer"])
            dist = {}
            for t in teams:
                if t["id"] in ans_by_team:
                    v = _to_float(ans_by_team[t["id"]])
                    if v is not None and true_val is not None:
                        dist[t["id"]] = abs(v - true_val)
            if dist:
                best = min(dist.values())
                winners = [tid for tid, d in dist.items() if d == best]
                if len(winners) == 1:
                    event_ids.append(self._score(conn, game_id, winners[0], q, q["points"],
                                                 "auto_numeric"))
                # tie -> host awards manually
        # text: never auto-graded
        return event_ids

    def _score(self, conn, game_id: int, team_id: int, q: dict, delta: int,
               reason: str) -> int:
        cur = conn.execute(
            "INSERT INTO score_events(game_id, team_id, round_id, question_id, delta, "
            "reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (game_id, team_id, q["round_id"], q["id"], delta, reason, time.time()))
        return cur.lastrowid

    def award(self, game_id: int, team_id: int, delta: int, reason: str = "manual") -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if not conn.execute("SELECT 1 FROM teams WHERE id = ? AND game_id = ?",
                                    (team_id, game_id)).fetchone():
                    return "no such team"
                q = self._current_question(conn, game_id)
                qid = q["id"] if q else None
                round_id = g["current_round_id"]
                event_id = self._score(conn, game_id, team_id,
                                       {"round_id": round_id, "id": qid}, delta, reason)
                team = conn.execute("SELECT name FROM teams WHERE id = ?", (team_id,)).fetchone()
                name = team["name"] if team else "?"
                self._log(conn, game_id, "award",
                          f"awarded {delta:+d} to {name}",
                          {"score_event_id": event_id})
                conn.commit()
                return None
            finally:
                conn.close()

    def next_question(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] not in ("revealed", "question"):
                    return "cannot advance now"
                q = self._current_question(conn, game_id)
                nxt = self._question_at(conn, g["current_round_id"], q["position"])
                if nxt:
                    prev = {"question_id": q["id"], "state": g["state"],
                            "clock": g["clock_ends_at"], "round_id": g["current_round_id"]}
                    self._open_question(conn, game_id, nxt["id"], g["clock_duration"])
                    self._log(conn, game_id, "advance", "advanced to next question", prev)
                else:
                    prev = {"question_id": q["id"], "state": g["state"],
                            "clock": g["clock_ends_at"], "round_id": g["current_round_id"]}
                    conn.execute("UPDATE games SET state = 'round_end', clock_ends_at = NULL "
                                 "WHERE id = ?", (game_id,))
                    self._log(conn, game_id, "round_end", "ended the round", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def skip(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] not in ("question", "revealed"):
                    return "cannot skip now"
                q = self._current_question(conn, game_id)
                nxt = self._question_at(conn, g["current_round_id"], q["position"])
                prev = {"question_id": q["id"], "state": g["state"],
                        "clock": g["clock_ends_at"], "round_id": g["current_round_id"]}
                if nxt:
                    self._open_question(conn, game_id, nxt["id"], g["clock_duration"])
                else:
                    conn.execute("UPDATE games SET state = 'round_end', clock_ends_at = NULL "
                                 "WHERE id = ?", (game_id,))
                self._log(conn, game_id, "skip", "skipped the question", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def void(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] not in ("question", "revealed"):
                    return "cannot void now"
                q = self._current_question(conn, game_id)
                nxt = self._question_at(conn, g["current_round_id"], q["position"])
                prev = {"question_id": q["id"], "prev_status": q["status"], "state": g["state"],
                        "clock": g["clock_ends_at"], "round_id": g["current_round_id"]}
                conn.execute("UPDATE questions SET status = 'voided' WHERE id = ?", (q["id"],))
                if nxt:
                    self._open_question(conn, game_id, nxt["id"], g["clock_duration"])
                else:
                    conn.execute("UPDATE games SET state = 'round_end', clock_ends_at = NULL "
                                 "WHERE id = ?", (game_id,))
                self._log(conn, game_id, "void", "voided the question", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def next_round(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] != "round_end":
                    return "not at round end"
                # Advance by POSITION, not by round id: configure assigns positions
                # 1..N while ids are globally auto-incremented, so they diverge.
                cur_pos = None
                if g["current_round_id"]:
                    cur = conn.execute(
                        "SELECT position FROM rounds WHERE id = ?",
                        (g["current_round_id"],)).fetchone()
                    cur_pos = cur["position"] if cur else None
                nxt_round = conn.execute(
                    "SELECT * FROM rounds WHERE game_id = ? AND position > ? "
                    "ORDER BY position LIMIT 1",
                    (game_id, cur_pos)).fetchone() if cur_pos is not None else None
                prev = {"round_id": g["current_round_id"], "question_id": g["current_question_id"],
                        "state": g["state"], "clock": g["clock_ends_at"]}
                if nxt_round:
                    # Never advance into a round that is not yet ready. The
                    # state and current question are left untouched on error.
                    if nxt_round["status"] != "ready":
                        return (f"round {nxt_round['position']} is not ready yet"
                                if nxt_round["status"] in ("pending", "generating")
                                else f"round {nxt_round['position']} failed to build")
                    first = self._first_question(conn, nxt_round["id"])
                    if not first:
                        return "next round has no questions"
                    conn.execute("UPDATE games SET current_round_id = ? WHERE id = ?",
                                 (nxt_round["id"], game_id))
                    self._open_question(conn, game_id, first["id"], g["clock_duration"])
                    self._log(conn, game_id, "next_round", "started the next round", prev)
                else:
                    conn.execute("UPDATE games SET state = 'final', clock_ends_at = NULL "
                                 "WHERE id = ?", (game_id,))
                    self._log(conn, game_id, "final", "ended the game", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def finish(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("UPDATE games SET state = 'finished', clock_ends_at = NULL "
                             "WHERE id = ?", (game_id,))
                conn.commit()
                return None
            finally:
                conn.close()

    def set_points(self, game_id: int, points: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                q = self._current_question(conn, game_id)
                if not q:
                    return "no current question"
                prev_points = q["points"]
                conn.execute("UPDATE questions SET points = ? WHERE id = ?",
                             (points, q["id"]))
                self._log(conn, game_id, "set_points", f"set points to {points}",
                          {"question_id": q["id"], "prev_points": prev_points})
                conn.commit()
                return None
            finally:
                conn.close()

    def adlib(self, game_id: int, stem: str, answer: str, options: list[str] | None = None,
              points: int = 10, qtype: str = "mc") -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                round_id = g["current_round_id"] or self.ensure_round(game_id, 1)
                qid = self._insert_questions(conn, game_id, [{
                    "stem": stem, "answer": answer, "options": options,
                    "points": points, "type": qtype, "source": "authored",
                }], round_id)[0]
                conn.execute("UPDATE rounds SET status = 'ready' WHERE id = ? "
                             "AND status != 'ready'", (round_id,))
                prev = {"question_id": g["current_question_id"], "state": g["state"],
                        "clock": g["clock_ends_at"], "round_id": round_id}
                self._open_question(conn, game_id, qid, g["clock_duration"])
                self._log(conn, game_id, "adlib", "added an ad-lib question",
                          {"question_id": qid, "prev": prev})
                conn.commit()
                return None
            finally:
                conn.close()

    # ---- clock -------------------------------------------------------------
    def start_clock(self, game_id: int, duration: int | None = None) -> str | None:
        """Start the clock: for *duration* seconds if given (which also becomes the
        game's question length), else what a pause left, else the question length."""
        if duration is not None and duration <= 0:
            return "duration must be positive"
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                length = duration if duration is not None else g["clock_duration"]
                d = duration if duration is not None else (g["clock_paused_left"] or length)
                prev = self._clock_fields(g)
                conn.execute("UPDATE games SET clock_ends_at = ?, clock_duration = ?, "
                             "clock_paused_left = NULL WHERE id = ?",
                             (time.time() + d, length, game_id))
                self._log(conn, game_id, "start_clock", "started the clock", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def pause_clock(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["clock_ends_at"] is None:
                    return "clock not running"
                # The time left is kept apart from clock_duration, which is the
                # length of every later question. An expired clock leaves nothing.
                remaining = g["clock_ends_at"] - time.time()
                prev = self._clock_fields(g)
                conn.execute("UPDATE games SET clock_ends_at = NULL, clock_paused_left = ? "
                             "WHERE id = ?", (remaining if remaining > 0 else None, game_id))
                self._log(conn, game_id, "pause_clock", "paused the clock", prev)
                conn.commit()
                return None
            finally:
                conn.close()

    def extend_clock(self, game_id: int, seconds: int = 30) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                # Extend from now when expired or paused; the past would still be late.
                now = time.time()
                ends = g["clock_ends_at"]
                base = ends if ends and ends > now else now + (g["clock_paused_left"] or 0)
                prev = self._clock_fields(g)
                conn.execute("UPDATE games SET clock_ends_at = ?, clock_paused_left = NULL "
                             "WHERE id = ?", (base + seconds, game_id))
                self._log(conn, game_id, "extend_clock", f"extended the clock by {seconds}s",
                          prev)
                conn.commit()
                return None
            finally:
                conn.close()

    @staticmethod
    def _clock_fields(g) -> dict:
        """The clock columns, as an undo payload."""
        return {"clock_ends_at": g["clock_ends_at"], "clock_duration": g["clock_duration"],
                "clock_paused_left": g["clock_paused_left"]}

    # ---- answering ---------------------------------------------------------
    def submit_answer(self, team_id: int, game_id: int, answer_text: str) -> dict:
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if g["state"] != "question":
                    return {"ok": False, "reason": "closed"}
                q = self._current_question(conn, game_id)
                if not q:
                    return {"ok": False, "reason": "closed"}
                is_late = (g["clock_ends_at"] is not None and time.time() > g["clock_ends_at"])
                now = time.time()
                conn.execute(
                    "INSERT INTO answers(question_id, team_id, answer_text, is_late, "
                    "submitted_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(question_id, team_id) DO UPDATE SET "
                    "answer_text = excluded.answer_text, is_late = excluded.is_late, "
                    "submitted_at = excluded.submitted_at",
                    (q["id"], team_id, answer_text[:500], 1 if is_late else 0, now))
                conn.commit()
                return {"ok": True, "is_late": is_late, "question_id": q["id"]}
            finally:
                conn.close()

    # ---- generation audit ---------------------------------------------
    def record_generation(self, job_id: str, game_id: int, topic: str, model: str,
                          count: int, accepted: int, rejected: int,
                          audit_rows: list[dict], error: str | None = None,
                          base_host: str | None = None,
                          provider: str | None = None) -> None:
        """Persist one generation job's audit trail: a summary row (job id, game
        id, topic, model, requested/accepted/rejected) plus one row per
        batch/retry/rejected attempt with its reason and latency.

        ``provider`` and the ``base_url`` host are appended to the summary
        row's ``reason`` as plain provenance — no key, no path."""
        provenance = []
        if provider:
            provenance.append(f"provider={provider}")
        if base_host:
            provenance.append(f"base_host={base_host}")
        suffix = (" " + " ".join(provenance)) if provenance else ""
        with self._wlock:
            conn = self._conn()
            try:
                now = time.time()
                total_latency = sum(int(r.get("latency_ms") or 0) for r in audit_rows)
                conn.execute(
                    "INSERT INTO gen_audit(job_id, game_id, topic, model, count_requested, "
                    "accepted, rejected, attempt, status, reason, latency_ms, raw_response, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, game_id, topic, model, count, accepted, rejected,
                     0, "error" if error else "summary",
                     (error or "summary") + suffix,
                     total_latency, "", now))
                for r in audit_rows:
                    conn.execute(
                        "INSERT INTO gen_audit(job_id, game_id, topic, model, "
                        "count_requested, accepted, rejected, attempt, status, reason, "
                        "latency_ms, raw_response, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, game_id, topic, model, count, accepted, rejected,
                         int(r.get("attempt", 0) or 0), r.get("status", ""),
                         r.get("reason") or "", int(r.get("latency_ms") or 0),
                         r.get("raw_response") or "", now))
                conn.commit()
            finally:
                conn.close()

    # ---- undo --------------------------------------------------------------
    def undo(self, game_id: int) -> str | None:
        with self._wlock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM action_log WHERE game_id = ? ORDER BY id DESC LIMIT 1",
                    (game_id,)).fetchone()
                if not row:
                    return "nothing to undo"
                payload = json.loads(row["payload"])
                action = row["action"]
                if action in ("advance", "skip", "round_end"):
                    self._restore_position(conn, game_id, payload)
                elif action == "next_round":
                    self._restore_position(conn, game_id, payload)
                elif action == "final":
                    self._restore_position(conn, game_id, payload)
                elif action == "start":
                    conn.execute("UPDATE games SET state = 'lobby', current_question_id = NULL, "
                                 "clock_ends_at = NULL WHERE id = ?", (game_id,))
                elif action == "reveal":
                    for eid in payload.get("score_event_ids", []):
                        conn.execute("DELETE FROM score_events WHERE id = ?", (eid,))
                    conn.execute("UPDATE games SET state = ?, clock_ends_at = ? WHERE id = ?",
                                 (payload.get("prev_state", "question"),
                                  payload.get("prev_clock"), game_id))
                elif action == "award":
                    conn.execute("DELETE FROM score_events WHERE id = ?",
                                 (payload.get("score_event_id"),))
                elif action == "void":
                    conn.execute("UPDATE questions SET status = ? WHERE id = ?",
                                 (payload.get("prev_status", "ready"), payload["question_id"]))
                    self._restore_position(conn, game_id, payload)
                elif action == "adlib":
                    self._delete_questions(conn, [payload.get("question_id")])
                    prev = payload.get("prev", {})
                    conn.execute("UPDATE games SET state = ?, current_question_id = ?, "
                                 "clock_ends_at = ? WHERE id = ?",
                                 (prev.get("state", "lobby"), prev.get("question_id"),
                                  prev.get("clock"), game_id))
                elif action == "set_points":
                    conn.execute("UPDATE questions SET points = ? WHERE id = ?",
                                 (payload.get("prev_points"), payload.get("question_id")))
                elif action in ("start_clock", "pause_clock", "extend_clock"):
                    conn.execute("UPDATE games SET clock_ends_at = ?, clock_duration = ?, "
                                 "clock_paused_left = ? WHERE id = ?",
                                 (payload.get("clock_ends_at"),
                                  payload.get("clock_duration"),
                                  payload.get("clock_paused_left"), game_id))
                elif action == "add_question":
                    self._delete_questions(conn, payload.get("question_ids", []))
                conn.execute("DELETE FROM action_log WHERE id = ?", (row["id"],))
                conn.commit()
                return None
            finally:
                conn.close()

    @staticmethod
    def _delete_questions(conn, question_ids: list) -> None:
        """Delete questions plus the answers and score events that reference
        them. Answers carry a foreign key, so deleting a question a team has
        already answered would otherwise fail the whole undo."""
        for qid in question_ids:
            conn.execute("DELETE FROM answers WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM score_events WHERE question_id = ?", (qid,))
            conn.execute("DELETE FROM questions WHERE id = ?", (qid,))

    def _restore_position(self, conn, game_id: int, payload: dict) -> None:
        conn.execute("UPDATE games SET state = ?, current_question_id = ?, "
                     "current_round_id = ?, clock_ends_at = ? WHERE id = ?",
                     (payload.get("state", "lobby"), payload.get("question_id"),
                      payload.get("round_id"), payload.get("clock"), game_id))

    # ---- scoring queries ---------------------------------------------------
    def team_totals(self, game_id: int) -> dict[int, int]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT team_id, SUM(delta) AS total FROM score_events WHERE game_id = ? "
                "GROUP BY team_id", (game_id,)).fetchall()
            return {r["team_id"]: r["total"] for r in rows}
        finally:
            conn.close()

    def round_totals(self, game_id: int) -> dict[int, dict[int, int]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT team_id, round_id, SUM(delta) AS total FROM score_events "
                "WHERE game_id = ? GROUP BY team_id, round_id", (game_id,)).fetchall()
            out: dict[int, dict[int, int]] = {}
            for r in rows:
                out.setdefault(r["team_id"], {})[r["round_id"]] = r["total"]
            return out
        finally:
            conn.close()

    # ---- snapshot ----------------------------------------------------------
    def snapshot(self, game_id: int, role: str, token: str) -> dict | None:
        conn = self._conn()
        try:
            g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if not g:
                return None
            if role == "team":
                team = self.team_by_token(token)
                if not team or team["game_id"] != game_id:
                    return None
            else:
                if not self.verify_host(game_id, token):
                    return None
            return self._snapshot(conn, g, role, team_id=(team["id"] if role == "team" else None))
        finally:
            conn.close()

    def _snapshot(self, conn, g, role: str, team_id: int | None) -> dict:
        reveal = g["state"] in ("revealed", "round_end", "final", "finished")
        teams = [dict(r) for r in conn.execute(
            "SELECT id, name, color_index, rejoin_pin FROM teams WHERE game_id = ? "
            "ORDER BY id", (g["id"],)).fetchall()]
        totals = self.team_totals(g["id"])
        my_pin = None
        for t in teams:
            t["total"] = totals.get(t["id"], 0)
            if t["id"] == team_id:
                my_pin = t["rejoin_pin"]
            if role != "host":
                # PINs are what make rejoining safe; only the host sees them all.
                t.pop("rejoin_pin")

        q = self._current_question(conn, g["id"])
        qpub = None
        if q:
            qpub = {
                "id": q["id"], "type": q["type"], "stem": q["stem"],
                "options": json.loads(q["options"]) if q["options"] else None,
                "points": q["points"], "media_type": q["media_type"],
                "media_url": q["media_url"],
                "position": q["position"],
            }
            if role == "host" or reveal:
                qpub["answer"] = q["answer"]

        clock_ends = g["clock_ends_at"]
        remaining = None if clock_ends is None else max(0, int(clock_ends - time.time()))
        locked = (g["state"] == "question" and clock_ends is not None
                  and time.time() > clock_ends)

        answered_count = 0
        if q:
            answered_count = conn.execute(
                "SELECT COUNT(*) AS n FROM answers WHERE question_id = ? AND is_late = 0",
                (q["id"],)).fetchone()["n"]

        round_q_count = self._question_count(conn, g["current_round_id"]) if g["current_round_id"] else 0

        reveal_results = None
        if reveal and q:
            se = conn.execute(
                "SELECT team_id, delta FROM score_events WHERE question_id = ? AND delta != 0",
                (q["id"],)).fetchall()
            byteam = {r["team_id"]: r["delta"] for r in se}
            reveal_results = [
                {"team_id": t["id"], "name": t["name"], "delta": byteam.get(t["id"], 0),
                 "correct": byteam.get(t["id"], 0) > 0}
                for t in teams
            ]

        rounds = [dict(r) for r in conn.execute(
            "SELECT r.id, r.position, r.title, r.category, r.status, r.error, "
            "r.library_round_id, "
            "(SELECT COUNT(*) FROM questions q WHERE q.round_id = r.id "
            " AND q.status != 'voided') AS question_count "
            "FROM rounds r WHERE r.game_id = ? ORDER BY r.position",
            (g["id"],)).fetchall()]
        # Round status/category/count are safe for every role; the error string
        # (which may carry internal detail) is host-only.
        if role != "host":
            for r in rounds:
                r.pop("error", None)
                r.pop("library_round_id", None)

        snap = {
            "game_id": g["id"], "name": g["name"], "code": g["code"],
            "state": g["state"], "clock_remaining": remaining, "clock_running": clock_ends is not None,
            "locked": locked, "teams": teams, "question": qpub,
            "answered_count": answered_count, "round_question_count": round_q_count,
            "current_round_id": g["current_round_id"],
            "reveal_results": reveal_results,
            "rounds": rounds,
            "round_totals": self.round_totals(g["id"]),
        }

        if role == "host":
            snap["judging"] = self._judging_view(conn, g["id"], q) if q else None
            snap["host_token_ok"] = True
        elif role == "team":
            if q:
                ans = conn.execute(
                    "SELECT answer_text, is_late FROM answers WHERE question_id = ? AND team_id = ?",
                    (q["id"], team_id)).fetchone()
                snap["my_answer"] = dict(ans) if ans else None
            snap["my_team_id"] = team_id
            snap["my_pin"] = my_pin
            if reveal and q:
                my_delta = conn.execute(
                    "SELECT COALESCE(SUM(delta),0) AS d FROM score_events "
                    "WHERE question_id = ? AND team_id = ?", (q["id"], team_id)).fetchone()["d"]
                snap["my_delta"] = my_delta
                snap["my_correct"] = my_delta > 0
        return snap

    def _judging_view(self, conn, game_id: int, q: dict) -> list[dict]:
        teams = conn.execute("SELECT id, name FROM teams WHERE game_id = ? ORDER BY id",
                             (game_id,)).fetchall()
        ans = {a["team_id"]: a["answer_text"] for a in conn.execute(
            "SELECT * FROM answers WHERE question_id = ?", (q["id"],)).fetchall()}
        correct = norm(q["answer"])
        out = []
        for t in teams:
            text = ans.get(t["id"], "")
            suggestion = norm(text) == correct and bool(correct)
            out.append({"team_id": t["id"], "name": t["name"], "answer": text,
                        "suggested_correct": suggestion})
        return out

    # ---- management APIs ----------------------------------------------------
    def list_games(self) -> list[dict]:
        conn = self._conn()
        try:
            games = conn.execute("SELECT * FROM games ORDER BY id DESC").fetchall()
            out = []
            for g in games:
                rc = conn.execute(
                    "SELECT COUNT(*) AS n FROM rounds WHERE game_id = ?",
                    (g["id"],)).fetchone()["n"]
                qc = conn.execute(
                    "SELECT COUNT(*) AS n FROM questions WHERE game_id = ? "
                    "AND status != 'voided'", (g["id"],)).fetchone()["n"]
                # Last host action (start/reveal/award/...); None until play starts.
                last = conn.execute(
                    "SELECT MAX(created_at) AS t FROM action_log WHERE game_id = ?",
                    (g["id"],)).fetchone()["t"]
                out.append({"id": g["id"], "name": g["name"], "code": g["code"],
                            "state": g["state"], "created_at": g["created_at"],
                            "last_active_at": last,
                            "round_count": rc, "question_count": qc})
            return out
        finally:
            conn.close()

    def _game_meta(self, conn, g) -> dict:
        rounds = [dict(r) for r in conn.execute(
            "SELECT r.id, r.position, r.title, r.category, r.status, r.error, "
            "r.library_round_id, "
            "(SELECT COUNT(*) FROM questions q WHERE q.round_id = r.id "
            " AND q.status != 'voided') AS question_count "
            "FROM rounds r WHERE r.game_id = ? ORDER BY r.position",
            (g["id"],)).fetchall()]
        return {"game_id": g["id"], "name": g["name"], "code": g["code"],
                "state": g["state"], "created_at": g["created_at"],
                "rounds": rounds}

    def game_metadata(self, game_id: int) -> dict | None:
        conn = self._conn()
        try:
            g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if not g:
                return None
            return self._game_meta(conn, g)
        finally:
            conn.close()

    def select_game(self, game_id: int) -> dict | None:
        """Rotate the host token; return metadata plus the new host token once."""
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if not g:
                    return None
                token = secrets.token_urlsafe(32)
                conn.execute("UPDATE games SET host_token_hash = ? WHERE id = ?",
                             (self._hash(token), game_id))
                # The deadline is an absolute time, so a question left open has
                # usually expired by the time it is reopened. Start it again.
                now = time.time()
                if g["state"] == "question" and (g["clock_ends_at"] or 0) <= now:
                    left = g["clock_paused_left"] or g["clock_duration"]
                    conn.execute("UPDATE games SET clock_ends_at = ?, clock_paused_left = NULL "
                                 "WHERE id = ?", (now + left, game_id))
                conn.commit()
                meta = self._game_meta(conn, g)
                meta["host_token"] = token
                return meta
            finally:
                conn.close()

    def delete_game(self, game_id: int) -> bool:
        """Transactionally delete only game-owned rows; library rows survive.

        Returns True when the game existed and was deleted.
        """
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT id FROM games WHERE id = ?",
                                 (game_id,)).fetchone()
                if not g:
                    return False
                # FK-safe order: children before parents.
                conn.execute("DELETE FROM action_log WHERE game_id = ?", (game_id,))
                conn.execute(
                    "DELETE FROM answers WHERE question_id IN "
                    "(SELECT id FROM questions WHERE game_id = ?)", (game_id,))
                conn.execute("DELETE FROM score_events WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM questions WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM rounds WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM teams WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
                conn.commit()
                return True
            finally:
                conn.close()

    # ---- round library ------------------------------------------------------
    def create_library_round(self, category: str, questions: list[dict],
                             title: str | None = None, source: str = "generated") -> int:
        """Store generated questions as an immutable library round; return its id."""
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO library_rounds(category, title, source, created_at) "
                    "VALUES (?, ?, ?, ?)", (category, title, source, time.time()))
                lr_id = cur.lastrowid
                for i, q in enumerate(questions):
                    options = json.dumps(q.get("options")) if q.get("options") else None
                    conn.execute(
                        "INSERT INTO library_questions(library_round_id, position, "
                        "type, stem, options, answer, points, media_type, media_url, "
                        "source, confidence, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'ready', ?)",
                        (lr_id, i + 1, q.get("type", "mc"), q["stem"], options,
                         q["answer"], int(q.get("points", 10)),
                         q.get("media_type", "none"), q.get("media_url"),
                         q.get("source", source), q.get("confidence"), time.time()))
                conn.commit()
                return lr_id
            finally:
                conn.close()

    def list_library_rounds(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM library_rounds ORDER BY id DESC").fetchall()
            out = []
            for r in rows:
                qc = conn.execute(
                    "SELECT COUNT(*) AS n FROM library_questions WHERE library_round_id = ?",
                    (r["id"],)).fetchone()["n"]
                preview = [dict(q) for q in conn.execute(
                    "SELECT stem, answer FROM library_questions "
                    "WHERE library_round_id = ? ORDER BY position LIMIT 3",
                    (r["id"],)).fetchall()]
                out.append({"id": r["id"], "category": r["category"],
                            "title": r["title"], "source": r["source"],
                            "created_at": r["created_at"], "question_count": qc,
                            "preview": preview})
            return out
        finally:
            conn.close()

    def _copy_library_questions(self, conn, game_id: int, round_id: int,
                                library_round_id: int) -> int:
        qs = conn.execute(
            "SELECT * FROM library_questions WHERE library_round_id = ? ORDER BY position",
            (library_round_id,)).fetchall()
        for q in qs:
            conn.execute(
                "INSERT INTO questions(game_id, round_id, position, type, stem, "
                "options, answer, points, media_type, media_url, source, confidence, "
                "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ready', ?)",
                (game_id, round_id, q["position"], q["type"], q["stem"], q["options"],
                 q["answer"], q["points"], q["media_type"], q["media_url"],
                 q["source"], q["confidence"], time.time()))
        return len(qs)

    def attach_library_round(self, game_id: int, round_id: int,
                             library_round_id: int, category: str) -> int | None:
        """Deep-copy a library round into a game round and mark it ready.

        The copy and the ``ready`` status commit together. Returns the copied
        question count, or None when the game no longer exists (deleted while
        the configuration job was running — no rows are recreated).
        """
        with self._wlock:
            conn = self._conn()
            try:
                if not conn.execute("SELECT 1 FROM games WHERE id = ?",
                                    (game_id,)).fetchone():
                    return None
                n = self._copy_library_questions(conn, game_id, round_id, library_round_id)
                conn.execute(
                    "UPDATE rounds SET status = 'ready', error = NULL, category = ?, "
                    "library_round_id = ? WHERE id = ?",
                    (category, library_round_id, round_id))
                conn.commit()
                return n
            finally:
                conn.close()

    def set_round_status(self, round_id: int, status: str, error: str | None = None) -> None:
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("UPDATE rounds SET status = ?, error = ? WHERE id = ?",
                             (status, error, round_id))
                conn.commit()
            finally:
                conn.close()

    def interrupt_inflight_rounds(self) -> int:
        """On restart, any round stuck in ``pending``/``generating`` was orphaned
        by the process exit and must be explicit, never phantom-ready."""
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE rounds SET status = 'failed', error = 'interrupted by restart' "
                    "WHERE status IN ('pending', 'generating')")
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def configure_game(self, game_id: int, rounds_spec: list) -> tuple[list[str], list[dict] | None]:
        """Validate then transactionally replace pre-game rounds; returns
        ``(errors, steps)``. ``errors`` is empty on success and ``steps`` carries
        one dict per round: ``round_id``, ``position``, ``category``,
        ``library_round_id`` and ``question_count``."""
        conn = self._conn()
        try:
            lib_ids = {r["id"] for r in conn.execute("SELECT id FROM library_rounds")}
        finally:
            conn.close()
        errors = validate_rounds_spec(rounds_spec, lib_ids)
        if errors:
            return errors, None
        with self._wlock:
            conn = self._conn()
            try:
                g = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
                if not g:
                    return ["no such game"], None
                if g["state"] != "lobby":
                    return ["game is not in lobby"], None
                # Replace prior pre-game configuration (order matters for FKs).
                conn.execute(
                    "DELETE FROM answers WHERE question_id IN "
                    "(SELECT id FROM questions WHERE game_id = ?)", (game_id,))
                conn.execute("DELETE FROM score_events WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM questions WHERE game_id = ?", (game_id,))
                conn.execute("DELETE FROM rounds WHERE game_id = ?", (game_id,))
                steps = []
                for i, entry in enumerate(rounds_spec):
                    pos = i + 1
                    if "library_round_id" in entry:
                        lr = conn.execute("SELECT * FROM library_rounds WHERE id = ?",
                                          (entry["library_round_id"],)).fetchone()
                        category = lr["category"]
                        library_round_id = lr["id"]
                        question_count = None
                    else:
                        category = entry["category"].strip()
                        library_round_id = None
                        question_count = entry.get("question_count", DEFAULT_QUESTION_COUNT)
                    cur = conn.execute(
                        "INSERT INTO rounds(game_id, position, title, category, status, "
                        "error, library_round_id) VALUES (?,?,?,?, 'pending', NULL, ?)",
                        (game_id, pos, category, category, library_round_id))
                    steps.append({"round_id": cur.lastrowid, "position": pos,
                                  "category": category,
                                  "library_round_id": library_round_id,
                                  "question_count": question_count})
                conn.commit()
                return [], steps
            finally:
                conn.close()


def _to_float(s: str) -> float | None:
    try:
        return float((s or "").strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None
