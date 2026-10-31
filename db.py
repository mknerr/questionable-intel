"""SQLite persistence for Questionable Intel (stdlib ``sqlite3``, WAL mode).

Single file ``data/trivia.db``. Totals are always *derived* from
``score_events`` (never stored and incremented blindly) so undo and manual
adjustment are trivially reversible.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL,
    code               TEXT NOT NULL UNIQUE,
    host_token_hash    TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'lobby',
    current_round_id   INTEGER,
    current_question_id INTEGER,
    clock_ends_at      REAL,
    clock_duration     INTEGER NOT NULL DEFAULT 30,
    created_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id),
    name         TEXT NOT NULL,
    join_token   TEXT NOT NULL UNIQUE,
    rejoin_pin   TEXT,
    color_index  INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_teams_game ON teams(game_id);

CREATE TABLE IF NOT EXISTS rounds (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id   INTEGER NOT NULL REFERENCES games(id),
    position  INTEGER NOT NULL,
    title     TEXT,
    category  TEXT,
    status    TEXT NOT NULL DEFAULT 'pending',
    error     TEXT,
    library_round_id INTEGER
);

CREATE TABLE IF NOT EXISTS questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id),
    round_id     INTEGER REFERENCES rounds(id),
    position     INTEGER NOT NULL,
    type         TEXT NOT NULL DEFAULT 'mc',
    stem         TEXT NOT NULL,
    options      TEXT,
    answer       TEXT NOT NULL,
    points       INTEGER NOT NULL DEFAULT 10,
    media_type   TEXT NOT NULL DEFAULT 'none',
    media_url    TEXT,
    source       TEXT NOT NULL DEFAULT 'generated',
    confidence   TEXT,
    status       TEXT NOT NULL DEFAULT 'ready',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_game ON questions(game_id);

CREATE TABLE IF NOT EXISTS answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id   INTEGER NOT NULL REFERENCES questions(id),
    team_id       INTEGER NOT NULL REFERENCES teams(id),
    answer_text   TEXT NOT NULL,
    is_late       INTEGER NOT NULL DEFAULT 0,
    submitted_at  REAL NOT NULL,
    UNIQUE(question_id, team_id)
);

CREATE TABLE IF NOT EXISTS score_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      INTEGER NOT NULL REFERENCES games(id),
    team_id      INTEGER NOT NULL REFERENCES teams(id),
    round_id     INTEGER,
    question_id  INTEGER,
    delta        INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_events_game ON score_events(game_id);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games(id),
    action      TEXT NOT NULL,
    label       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gen_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT,
    game_id         INTEGER,
    topic           TEXT,
    model           TEXT,
    count_requested INTEGER,
    accepted        INTEGER,
    rejected        INTEGER,
    attempt         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    reason          TEXT,
    latency_ms      INTEGER,
    raw_response    TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT NOT NULL,
    title      TEXT,
    source     TEXT NOT NULL DEFAULT 'generated',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_round_id INTEGER NOT NULL REFERENCES library_rounds(id),
    position         INTEGER NOT NULL,
    type             TEXT NOT NULL DEFAULT 'mc',
    stem             TEXT NOT NULL,
    options          TEXT,
    answer           TEXT NOT NULL,
    points           INTEGER NOT NULL DEFAULT 10,
    media_type       TEXT NOT NULL DEFAULT 'none',
    media_url        TEXT,
    source           TEXT NOT NULL DEFAULT 'generated',
    confidence       TEXT,
    status           TEXT NOT NULL DEFAULT 'ready',
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_library_questions_round ON library_questions(library_round_id);
"""

# Columns added for the generation audit trail. `CREATE TABLE IF NOT EXISTS` will
# not add them to a database created before the fix, so `init_db` runs this
# idempotent ALTER migration for pre-existing files.
_GEN_AUDIT_COLUMNS = (
    ("job_id", "TEXT"),
    ("count_requested", "INTEGER"),
    ("accepted", "INTEGER"),
    ("rejected", "INTEGER"),
)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: str) -> None:
    _ensure_permissions(path)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate_gen_audit(conn)
        _migrate_rounds(conn)
        _migrate_teams(conn)
        conn.commit()
    finally:
        conn.close()
    _ensure_permissions(path)


def _ensure_permissions(path: str) -> None:
    """``data/`` is created 0700 and ``trivia.db*`` are 0600, so the
    provider key stored in the DB (plaintext, accepted) is not world-readable.

    Runs before and after initialisation (the file and its WAL/SHM sidecars are
    only created once ``connect`` opens them). Only ever touches the database's
    own directory — never anything outside ``data/``.
    """
    data_dir = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)
        except OSError:
            pass


def _migrate_gen_audit(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(gen_audit)")}
    for name, decl in _GEN_AUDIT_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE gen_audit ADD COLUMN {name} {decl}")


# Round columns added for the management/round-library pass. ``CREATE TABLE IF
# NOT EXISTS`` does not alter a pre-existing ``rounds`` table, so these are
# added idempotently for older DB files (same pattern as ``_migrate_gen_audit``).
_ROUND_COLUMNS = (
    ("category", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("error", "TEXT"),
    ("library_round_id", "INTEGER"),
)


def _migrate_teams(conn: sqlite3.Connection) -> None:
    """Add ``teams.rejoin_pin`` to pre-existing DBs. Teams created before the
    column existed keep a NULL PIN, which never matches: they can still play
    from the phone that joined, but cannot rejoin from another device."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(teams)")}
    if "rejoin_pin" not in existing:
        conn.execute("ALTER TABLE teams ADD COLUMN rejoin_pin TEXT")


def _migrate_rounds(conn: sqlite3.Connection) -> None:
    """Idempotent rounds migration for pre-existing DBs.

    1. Add the round ``category``/``status``/``error``/``library_round_id``
       columns if they are missing.
    2. Backfill ``status``: a round that already has questions is effectively
       ``ready``; the rest stay ``pending``.
    3. Deduplicate any legacy ``(game_id, position)`` pairs (``ensure_round``
       did not enforce uniqueness) by merging into the lowest-id row, so the
       unique index can be created without losing questions.
    4. Create the ``UNIQUE(game_id, position)`` index.
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(rounds)")}
    for name, decl in _ROUND_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE rounds ADD COLUMN {name} {decl}")

    # Backfill status for rounds that already carry questions.
    conn.execute(
        "UPDATE rounds SET status = 'ready' "
        "WHERE status = 'pending' AND id IN ("
        "  SELECT DISTINCT round_id FROM questions WHERE round_id IS NOT NULL)")
    # A round that has questions but a NULL/empty status (legacy) is ready too.
    conn.execute(
        "UPDATE rounds SET status = 'ready' "
        "WHERE (status IS NULL OR status = '') AND id IN ("
        "  SELECT DISTINCT round_id FROM questions WHERE round_id IS NOT NULL)")

    # Deduplicate before the unique index: keep the lowest id, remap all
    # references onto it, drop the rest.
    dups = conn.execute(
        "SELECT game_id, position FROM rounds "
        "GROUP BY game_id, position HAVING COUNT(*) > 1").fetchall()
    for d in dups:
        rows = conn.execute(
            "SELECT id FROM rounds WHERE game_id = ? AND position = ? ORDER BY id",
            (d["game_id"], d["position"])).fetchall()
        keep = rows[0]["id"]
        for extra in rows[1:]:
            rid = extra["id"]
            conn.execute("UPDATE questions SET round_id = ? WHERE round_id = ?",
                         (keep, rid))
            conn.execute("UPDATE score_events SET round_id = ? WHERE round_id = ?",
                         (keep, rid))
            conn.execute("UPDATE games SET current_round_id = ? "
                         "WHERE current_round_id = ?", (keep, rid))
            conn.execute("DELETE FROM rounds WHERE id = ?", (rid,))

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_game_position "
        "ON rounds(game_id, position)")
