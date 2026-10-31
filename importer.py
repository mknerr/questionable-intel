"""Question import: pasted or uploaded text -> question rows, with no LLM.

``parse`` auto-detects the format (see docs/IMPORT.md) and returns every row it
could read plus a list of line-numbered problems. It never raises on bad input
and saves nothing; the server decides what to do with the rows. Rows with both a
stem and an answer are ``complete`` and playable as they are (typed answers are
judged by the host); rows with only half are ``needs_answer`` / ``needs_question``
and can be completed by ``gen.complete_questions`` when a model is available.

Detection order: JSON, lettered multiple-choice blocks (Aiken), ``Q:``/``A:``
tagged blocks, tab/CSV tables (Quizlet exports, spreadsheet pastes), bare lines.

Stdlib only. Only the fields listed in the row shape below survive parsing —
anything else in the input (``media_url`` included) is dropped.
"""
from __future__ import annotations

import csv
import html
import io
import json
import math
import random
import re

from engine import norm

MAX_IMPORT_CHARS = 200_000
MAX_IMPORT_QUESTIONS = 100
STEM_MIN, STEM_MAX = 3, 300
ANSWER_MAX = 200
OPTIONS_MIN, OPTIONS_MAX = 2, 6
OPTION_LEN_MAX = 120
POINTS_DEFAULT, POINTS_MIN, POINTS_MAX = 10, 1, 1000
CATEGORY_MAX = 80
# A garbage paste could produce thousands of line errors; keep the list readable.
MAX_ERRORS = 50

EMPTY_MESSAGE = ("Nothing to import. Paste your questions (one per line, a Quizlet "
                 "export, CSV, or JSON) or choose a file.")

_STEM_KEYS = ("stem", "question", "q")
_ANSWER_KEYS = ("answer", "correct_answer", "a", "correct")
_OPTION_KEYS = ("options", "choices")
_CATEGORY_KEYS = ("category", "round")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# "1.", "2)", "Q3:", "Question 4 -" in front of a question line.
# Needs a space after a bare number so "3.14" and "2-3 players" are left alone.
_NUMBERING_RE = re.compile(
    r"^\s*(?:q(?:uestion)?\s*(?:\d+\s*[:.)\-]|:)\s*|\d+\s*[.):\-]\s+)", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*\u2022]\s+")
_INTERROGATIVE_RE = re.compile(
    r"^(?:who|what|when|where|why|which|how|name|in\s+what|is|are|did|does|do|can)\b",
    re.IGNORECASE)

# Lettered option line: "A. x", "A) x", "a) x", "(A) x", optional leading "*".
_OPTION_RE = re.compile(r"^\s*(\*\s*)?\(?([A-Fa-f])[.)]\s+(\S.*?)\s*$")
_ANSWER_LINE_RE = re.compile(
    r"^\s*(?:answer|ans|correct(?:\s+answer)?)\s*[:=\-]\s*(\S.*?)\s*$", re.IGNORECASE)
_TAG_RE = re.compile(
    r"^\s*(q(?:uestion)?\s*\d*|a(?:nswer)?|w(?:rong)?\s*\d*|category|points)"
    r"\s*:\s*(.*?)\s*$", re.IGNORECASE)


class _Sink:
    """Collects line errors, capped so a garbage paste stays readable."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._extra = 0

    def add(self, line, message: str) -> None:
        if len(self._items) < MAX_ERRORS:
            self._items.append({"line": line, "message": message})
        else:
            self._extra += 1

    def empty(self) -> bool:
        return not self._items

    def result(self) -> list[dict]:
        out = list(self._items)
        if self._extra:
            out.append({"line": None, "message": f"...and {self._extra} more problems."})
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse(text: str, *, swap: bool = False) -> dict:
    """Parse *text* into ``{"format", "questions", "errors"}``. Never raises."""
    sink = _Sink()
    fmt, questions = "none", []
    try:
        if not isinstance(text, str):
            sink.add(None, EMPTY_MESSAGE)
        else:
            text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
            if len(text) > MAX_IMPORT_CHARS:
                sink.add(None, f"That is {len(text):,} characters; the limit is "
                               f"{MAX_IMPORT_CHARS:,}. Split it into smaller imports.")
            elif not text.strip():
                sink.add(None, EMPTY_MESSAGE)
            else:
                fmt, rows = _read(text, sink)
                questions = _finalize(rows, swap, sink)
                if not questions and sink.empty():
                    sink.add(None, "No questions found in that text.")
    except Exception:  # pragma: no cover - last-resort guard: bad paste, never a traceback
        questions = []
        sink.add(None, "Couldn't read that text. Check the format against the examples.")
    return {"format": fmt, "questions": questions, "errors": sink.result()}


def counts(questions: list[dict]) -> dict:
    """Row counts by status, for the preview and the save response."""
    out = {"total": len(questions), "ready": 0, "needs_answer": 0, "needs_question": 0}
    for q in questions:
        if q["status"] == "complete":
            out["ready"] += 1
        else:
            out[q["status"]] += 1
    return out


# ---------------------------------------------------------------------------
# Format detection / readers. Each reader returns raw rows:
#   {stem, answer, options, labels, wrong, type, points, category, line, swappable}
# which _finalize validates. ``options`` keep their order; ``wrong`` (with an
# answer) becomes shuffled options.
# ---------------------------------------------------------------------------
def _read(text: str, sink: _Sink) -> tuple[str, list[dict]]:
    stripped = text.strip()
    if stripped[:1] in ("[", "{"):
        got = _read_json(text, stripped, sink)
        if got is not None:
            return got
    lines = text.split("\n")
    for reader in (_read_lettered, _read_tagged, _read_delimited):
        got = reader(text, lines, sink)
        if got is not None:
            return got
    return "lines", _read_bare(lines)


def _row(**kw) -> dict:
    row = {"stem": None, "answer": None, "options": None, "labels": None, "wrong": None,
           "type": None, "points": None, "category": None, "line": None,
           "swappable": False}
    row.update(kw)
    return row


# ---- (a) JSON ---------------------------------------------------------------
def _looks_like_json(s: str) -> bool:
    # "[Music] Who painted...": a bracketed tag in prose is not a JSON attempt.
    return s[0] == "{" or not re.match(r"\[[^\]\n]{1,40}\]\s+\S", s)


def _read_json(text: str, stripped: str, sink: _Sink):
    try:
        data = json.loads(stripped)
    except (ValueError, RecursionError) as e:
        if not _looks_like_json(stripped):
            return None
        if isinstance(e, json.JSONDecodeError):
            lead = text[:len(text) - len(text.lstrip())].count("\n")
            sink.add(e.lineno + lead, f"This looks like JSON but could not be read: {e.msg} "
                           f"(column {e.colno}).")
        else:
            sink.add(None, "This looks like JSON but is nested too deeply to read.")
        return "json", []
    items = None
    if isinstance(data, dict):
        for key in ("questions", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None and any(k in data for k in _STEM_KEYS):
            items = [data]
    elif isinstance(data, list):
        items = data
    if items is None:
        sink.add(None, 'JSON must be an array of questions, or an object with a '
                       '"questions" (or "results") array.')
        return "json", []
    rows = []
    for i, item in enumerate(items, 1):
        row = _json_item(i, item, sink)
        if row is not None:
            rows.append(row)
    return "json", rows


def _pick(item: dict, keys):
    for k in keys:
        if item.get(k) is not None:
            return item[k]
    return None


def _as_text(v):
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _json_item(i: int, item, sink: _Sink):
    if not isinstance(item, dict):
        sink.add(None, f"Question {i}: expected an object, found {type(item).__name__}.")
        return None
    # Open Trivia DB shape: HTML-escaped strings, wrong answers listed apart.
    otdb = isinstance(item.get("incorrect_answers"), list)
    fix = html.unescape if otdb else (lambda s: s)

    def text_of(keys):
        v = _as_text(_pick(item, keys))
        return fix(v) if v is not None else None

    row = _row(stem=text_of(_STEM_KEYS), answer=text_of(_ANSWER_KEYS),
               type=_as_text(item.get("type")), points=item.get("points"),
               category=text_of(_CATEGORY_KEYS))
    raw_opts = _pick(item, _OPTION_KEYS)
    if isinstance(raw_opts, dict):  # {"A": "x", "B": "y"}
        row["labels"] = [str(k).strip().upper() for k in raw_opts]
        raw_opts = list(raw_opts.values())
    if isinstance(raw_opts, list):
        row["options"] = [fix(t) for t in (_as_text(o) for o in raw_opts) if t is not None]
    elif raw_opts is not None:
        sink.add(None, f"Question {i}: options must be a list.")
        return None
    elif otdb:
        row["wrong"] = [fix(t) for t in (_as_text(o) for o in item["incorrect_answers"])
                        if t is not None]
    return row


# ---- (b) lettered multiple-choice blocks (Moodle Aiken) -------------------------
def _read_lettered(text: str, lines: list[str], sink: _Sink):
    prev = None
    found = False
    for line in lines:
        m = _OPTION_RE.match(line)
        if m and prev is not None and prev.upper() == "A" and m.group(2).upper() == "B":
            found = True
            break
        prev = m.group(2) if m else None
    if not found:
        return None

    rows: list[dict] = []
    cur: dict | None = None

    def new(no):
        return {"line": no, "stem": [], "options": [], "labels": [], "star": [],
                "answer": None, "blank": False}

    def flush():
        nonlocal cur
        c, cur = cur, None
        if c is None:
            return
        stem = " ".join(c["stem"]).strip() or None
        if stem is None and not c["options"]:
            return
        if stem is None:
            sink.add(c["line"], "Options with no question above them; skipped.")
            return
        if not c["options"] and c["answer"] is None:
            # Lines with no choices under them, in a paste mixed with lettered
            # blocks: rows of their own, not one long question.
            rows.extend(_bare_row(c["line"] + i, l) for i, l in enumerate(c["stem"]))
            return
        answer = c["answer"]
        if len(c["star"]) > 1:
            sink.add(c["line"], "More than one option is marked with *; skipped.")
            return
        if c["star"]:
            starred = c["options"][c["star"][0]]
            if answer is not None and _resolve(answer, c["options"], c["labels"]) != starred:
                sink.add(c["line"], "The * marker and the ANSWER line disagree; skipped.")
                return
            answer = starred
        rows.append(_row(stem=stem, answer=answer, line=c["line"],
                         options=c["options"] or None,
                         labels=c["labels"] if c["options"] else None))

    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            if cur is not None:
                cur["blank"] = True
            continue
        mo = _OPTION_RE.match(line)
        ma = _ANSWER_LINE_RE.match(line)
        if mo:
            letter = mo.group(2).upper()
            if cur is not None and cur["options"] and letter <= cur["labels"][-1]:
                flush()  # the letters restarted: a new block with no question line
            if cur is None:
                cur = new(no)
            cur["blank"] = False
            if mo.group(1):
                cur["star"].append(len(cur["options"]))
            cur["options"].append(mo.group(3))
            cur["labels"].append(letter)
        elif ma and cur is not None and cur["answer"] is None:
            cur["answer"] = ma.group(1)
            flush()
        elif ma:
            sink.add(no, "An ANSWER line with no question above it; skipped.")
        else:
            numbered = bool(_NUMBERING_RE.match(line))
            if cur is not None and (cur["options"] or cur["answer"] is not None
                                    or cur["blank"] or (numbered and cur["stem"])
                                    or (cur["stem"] and cur["stem"][-1].endswith("?"))):
                flush()
            if cur is None:
                cur = new(no)
            cur["stem"].append(_NUMBERING_RE.sub("", line, count=1))
    flush()
    return "lettered", rows


# ---- (c) tagged blocks ----------------------------------------------------------
def _continues(cur: dict, raw: str, line: str) -> bool:
    """Whether an untagged line wraps the Q:/A: field above it. Indented or
    lower-case lines do; so does anything after a question not yet finished. A
    fresh capitalized line after an answer or a ``?`` is a row of its own."""
    field = cur["last"]
    if field not in ("stem", "answer") or not cur[field]:
        return False
    if raw[:1].isspace() or line[:1].islower():
        return True
    return field == "stem" and not cur[field].endswith("?")


def _read_tagged(text: str, lines: list[str], sink: _Sink):
    nonblank = [l for l in lines if l.strip()]
    tags = [_TAG_RE.match(l) for l in nonblank]
    core = [m for m in tags if m and m.group(1).strip().lower()[0] in "qa"]
    if not core:
        return None

    rows: list[dict] = []
    cur: dict | None = None
    sticky_category = None

    def new(no):
        return {"line": no, "stem": None, "answer": None, "wrong": [],
                "category": sticky_category, "points": None, "last": None}

    def flush():
        nonlocal cur
        c, cur = cur, None
        if c is None or (c["stem"] is None and c["answer"] is None):
            return
        rows.append(_row(stem=c["stem"], answer=c["answer"], wrong=c["wrong"] or None,
                         category=c["category"], points=c["points"], line=c["line"]))

    for no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            flush()
            continue
        m = _TAG_RE.match(line)
        if not m:
            if cur is not None and _continues(cur, raw, line):
                cur[cur["last"]] += " " + line  # continuation of a wrapped line
            else:  # a bare question or answer mixed in among the blocks
                flush()
                rows.append(_bare_row(no, line))
            continue
        tag, val = m.group(1).strip().lower(), m.group(2)
        if tag == "category":
            if cur is not None and (cur["stem"] or cur["answer"]):
                cur["category"] = val or None
            else:
                sticky_category = val or None
        elif tag == "points":
            if cur is not None:
                cur["points"] = val
        elif tag[0] == "q":
            flush()
            cur = new(no)
            cur["stem"], cur["last"] = val or None, "stem"
        elif tag[0] == "a":
            if cur is None or cur["answer"] is not None:
                flush()
                cur = new(no)
            cur["answer"], cur["last"] = val or None, "answer"
        else:  # wrong
            if cur is None:
                sink.add(no, "A Wrong: line needs a Q:/A: block above it; skipped.")
            elif val:
                cur["wrong"].append(val)
                cur["last"] = None
    flush()
    return "tagged", rows


# ---- (d) tab / comma tables -----------------------------------------------------
_STEM_HEADERS = {"question", "stem", "q", "term", "prompt"}
_ANSWER_HEADERS = {"answer", "correct", "correct answer", "a", "definition"}
_CATEGORY_HEADERS = {"category", "round", "topic"}
_WRONG_HEADER_RE = re.compile(r"(?:wrong|incorrect)(?: answers?)?\s*\d*")
_OPTION_HEADER_RE = re.compile(r"(?:option|choice)s?\s*([a-f]|\d+)")


def _read_delimited(text: str, lines: list[str], sink: _Sink):
    nonblank = [(no, l) for no, l in enumerate(lines, 1) if l.strip()]
    tabbed = [x for x in nonblank if "\t" in x[1]]
    table = None
    fmt = "tsv"
    if tabbed:  # people don't type tabs into prose, so even one tab means columns
        if len(nonblank) == 1 and nonblank[0][1].count(";") >= 2:
            # Quizlet's "semicolon between cards" export: one line of term<TAB>def;...
            table = [(nonblank[0][0], card.split("\t")) for card in nonblank[0][1].split(";")
                     if card.strip()]
        else:
            table = [(no, l.split("\t")) for no, l in nonblank]
    else:
        fmt = "csv"
        table = _csv_table(text, lines)
        if table is None:
            return None
    table = [(no, [c.strip() for c in cells]) for no, cells in table]
    table = [(no, cells) for no, cells in table if any(cells)]
    if not table:
        return None
    header = _header_map(table[0][1])
    if header is None and fmt == "csv" and not _csv_plausible(table, lines):
        return None
    if header is not None:
        table = table[1:]
        return fmt, [r for r in (_table_row_headed(no, c, header) for no, c in table) if r]
    return fmt, [_table_row_plain(no, c) for no, c in table]


def _csv_table(text: str, lines: list[str]):
    try:
        reader = csv.reader(io.StringIO(text))
        table, last = [], 0
        for cells in reader:
            if cells:
                table.append((last + 1, cells))
            last = reader.line_num
    except csv.Error:
        return None
    if not any(len(c) >= 2 for _, c in table):
        return None
    return table


def _csv_plausible(table, lines: list[str]) -> bool:
    """A headerless CSV needs every row to have the same >= 2 columns, and must
    not be prose: one question that happens to contain a comma is not a table."""
    widths = {len(cells) for _, cells in table}
    if len(widths) != 1 or min(widths) < 2:
        return False
    prose = 0
    if len(table) == 1 and ", " in lines[table[0][0] - 1]:
        return False  # a lone sentence with a comma is a question, not a table
    for no, _ in table:
        line = lines[no - 1].strip() if 0 < no <= len(lines) else ""
        if '"' not in line and ", " in line and (
                line.endswith(("?", ".", "!")) or len(line.split()) >= 8):
            prose += 1
    return 2 * prose < len(table)


def _header_map(cells: list[str]):
    """Column roles from a header row, or None when it isn't one."""
    names = [" ".join(re.sub(r"[^a-z0-9]+", " ", c.lower()).split()) for c in cells]
    letters = {n for n in names if len(n) == 1 and n in "abcdef"}
    bare_options = len(letters) >= 2  # a,b,c,d columns; a lone "a" means "answer"
    h = {"stem": None, "answer": None, "wrong": [], "options": [], "category": None,
         "type": None, "points": None}
    for i, n in enumerate(names):
        mo = _OPTION_HEADER_RE.fullmatch(n)
        if bare_options and n in letters:
            h["options"].append((i, n.upper()))
        elif mo:
            lab = mo.group(1)
            h["options"].append((i, lab.upper() if lab.isalpha() else chr(64 + min(int(lab), 26))))
        elif n in _STEM_HEADERS and h["stem"] is None:
            h["stem"] = i
        elif n in _ANSWER_HEADERS and h["answer"] is None:
            h["answer"] = i
        elif _WRONG_HEADER_RE.fullmatch(n):
            h["wrong"].append(i)
        elif n in _CATEGORY_HEADERS and h["category"] is None:
            h["category"] = i
        elif n == "type" and h["type"] is None:
            h["type"] = i
        elif n in ("points", "point") and h["points"] is None:
            h["points"] = i
    recognised = sum(1 for k in ("stem", "answer", "category", "type", "points")
                     if h[k] is not None) + len(h["wrong"]) + len(h["options"])
    if len(cells) < 2 or recognised < 2 or (h["stem"] is None and h["answer"] is None):
        return None
    return h


def _cell(cells, i):
    return cells[i] if i is not None and i < len(cells) else None


def _table_row_headed(no, cells, h):
    row = _row(stem=_cell(cells, h["stem"]), answer=_cell(cells, h["answer"]),
               category=_cell(cells, h["category"]), type=_cell(cells, h["type"]),
               points=_cell(cells, h["points"]), line=no)
    if h["options"]:
        opts = [(_cell(cells, i), lab) for i, lab in h["options"]]
        opts = [(o, lab) for o, lab in opts if o]
        if opts:
            row["options"] = [o for o, _ in opts]
            row["labels"] = [lab for _, lab in opts]
    elif h["wrong"]:
        row["wrong"] = [w for w in (_cell(cells, i) for i in h["wrong"]) if w] or None
    row["swappable"] = (h["stem"] is not None and h["answer"] is not None
                        and not row["options"] and not row["wrong"])
    if not any((row["stem"], row["answer"])):
        return None
    return row


def _table_row_plain(no, cells):
    if len(cells) == 1:
        return _bare_row(no, cells[0])
    wrong = [c for c in cells[2:] if c] or None
    return _row(stem=cells[0] or None, answer=cells[1] or None, wrong=wrong, line=no,
                swappable=wrong is None)


# ---- (e) bare lines ---------------------------------------------------------------
def _read_bare(lines: list[str]) -> list[dict]:
    return [_bare_row(no, l) for no, l in enumerate(lines, 1) if l.strip()]


def _bare_row(no: int, line: str) -> dict:
    s = _BULLET_RE.sub("", _NUMBERING_RE.sub("", line.strip(), count=1), count=1).strip()
    if s.endswith("?") or _INTERROGATIVE_RE.match(s) or len(s.split()) >= 8:
        return _row(stem=s, line=no)
    return _row(answer=s, line=no)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _clean(v):
    if v is None:
        return None
    s = " ".join(_CONTROL_RE.sub("", str(v)).split())
    return s or None


def _points(v) -> int:
    if isinstance(v, bool) or v is None:
        return POINTS_DEFAULT
    try:
        n = int(float(str(v).strip()))
    except (ValueError, OverflowError):
        return POINTS_DEFAULT
    return max(POINTS_MIN, min(POINTS_MAX, n))


_TYPE_ALIASES = {
    "mc": "mc", "multiple": "mc", "multiple choice": "mc", "multiplechoice": "mc",
    "mcq": "mc", "boolean": "mc", "true false": "mc", "tf": "mc",
    "text": "text", "short": "text", "free": "text", "free text": "text",
    "open": "text", "typed": "text",
    "numeric": "numeric", "number": "numeric", "numerical": "numeric",
}


def _norm_type(v):
    if v is None:
        return None
    key = " ".join(re.sub(r"[^a-z]+", " ", str(v).lower()).split())
    return _TYPE_ALIASES.get(key)


def _to_number(s: str):
    try:
        n = float(s.strip().replace(",", ""))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _resolve(answer: str, options: list[str], labels=None):
    """The option the answer names: by text (``engine.norm``), else a letter
    reference (``B``, ``(b)``, ``B.``, ``B) text``). None when it names none."""
    na = norm(answer)
    if na:
        hits = [o for o in options if norm(o) == na]
        if len(hits) == 1:
            return hits[0]
    a = answer.strip()
    m = re.fullmatch(r"\(?([A-Fa-f])\)?[.:)]?", a)
    if m is None:
        m = re.match(r"\(?([A-Fa-f])[.)]\s+(.+)$", a)
        if m is not None:  # "B) Paris": trust the text if it is an option
            rest = norm(m.group(2))
            hits = [o for o in options if norm(o) == rest]
            return hits[0] if len(hits) == 1 else None
        return None
    letter = m.group(1).upper()
    idx = labels.index(letter) if labels and letter in labels else (
        None if labels else ord(letter) - 65)
    return options[idx] if idx is not None and 0 <= idx < len(options) else None


def _finalize(rows: list[dict], swap: bool, sink: _Sink) -> list[dict]:
    questions: list[dict] = []
    seen: set[str] = set()
    over = 0
    for raw in rows:
        q = _build(raw, swap, sink)
        if q is None:
            continue
        key = ("q:" + norm(q["stem"])) if q["stem"] else ("a:" + norm(q["answer"]))
        if key in seen:
            sink.add(q["line"], "Duplicate of an earlier question; skipped.")
            continue
        seen.add(key)
        if len(questions) >= MAX_IMPORT_QUESTIONS:
            over += 1
            continue
        questions.append(q)
    if over:
        sink.add(None, f"Only the first {MAX_IMPORT_QUESTIONS} questions were kept; "
                       f"{over} more were dropped. Split it into several imports.")
    return questions


def _build(raw: dict, swap: bool, sink: _Sink):
    line = raw.get("line")

    def bad(msg):
        sink.add(line, msg)
        return None

    stem, answer = _clean(raw.get("stem")), _clean(raw.get("answer"))
    if stem is None and answer is None:
        return bad("No question or answer found in this row.")
    if swap and raw.get("swappable") and stem and answer:
        stem, answer = answer, stem
    if stem is not None and not STEM_MIN <= len(stem) <= STEM_MAX:
        return bad(f"Question must be {STEM_MIN}-{STEM_MAX} characters (this one is {len(stem)}).")
    if answer is not None and len(answer) > ANSWER_MAX:
        return bad(f"Answer is longer than {ANSWER_MAX} characters.")

    options = raw.get("options")
    labels = raw.get("labels")
    shuffle = False
    if options is not None:
        options = [o for o in (_clean(o) for o in options) if o]
    wrong = [w for w in (_clean(w) for w in raw.get("wrong") or []) if w]
    if options is None and wrong:
        if answer is None:
            sink.add(line, "Wrong answers were given without a correct answer; ignored.")
        else:
            options, shuffle = [answer] + wrong, True
    if options is not None:
        if not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
            return bad(f"Multiple choice needs {OPTIONS_MIN}-{OPTIONS_MAX} options "
                       f"(found {len(options)}).")
        if any(len(o) > OPTION_LEN_MAX for o in options):
            return bad(f"An option is longer than {OPTION_LEN_MAX} characters.")
        keys = [norm(o) for o in options]
        if "" in keys or len(set(keys)) != len(keys):
            return bad("Options must be different from each other.")
        if answer is not None:
            resolved = _resolve(answer, options, None if shuffle else labels)
            if resolved is None:
                return bad("The answer does not match any of the options.")
            answer = resolved
        if shuffle:
            random.shuffle(options)

    qtype = _norm_type(raw.get("type"))
    if qtype == "mc" and not options:
        return bad("Type is multiple choice but no options were given.")
    if qtype == "numeric":
        if options:
            return bad("A numeric question cannot have options.")
        if answer is not None and _to_number(answer) is None:
            return bad("A numeric question needs a number as its answer.")
    if qtype == "text":
        options = None
    qtype = qtype or ("mc" if options else "text")

    category = _clean(raw.get("category"))
    return {
        "stem": stem, "answer": answer, "options": options, "type": qtype,
        "points": _points(raw.get("points")),
        "category": category[:CATEGORY_MAX] if category else None,
        "status": ("complete" if stem and answer
                   else "needs_answer" if stem else "needs_question"),
        "line": line,
    }
