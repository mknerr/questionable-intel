"""HTTP layer for Questionable Intel (stdlib ``http.server`` / ``ThreadingHTTPServer``).

Three surfaces, distinct by role:
  * ``/console``  — host console (host-token gated)
  * ``/stage``    — the projector/TV surface (host-token gated, answer withheld pre-reveal)
  * ``/join``, ``/play`` — the phone surface (room code / team token)

State-changing endpoints are POST + token. The correct answer is withheld
from team/stage roles until reveal.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import engine
import gen
import importer
import netutil
import qr
import settings

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Applied to every response: defense-in-depth security headers. The
# console/stage/join/play pages use inline <script> and inline event handlers,
# so script-src/style-src must keep 'unsafe-inline'; every other source is
# same-origin. XSS is still neutralised at the sink (see console.html esc()).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _parse_int(value) -> int | None:
    """Coerce caller-supplied input to int; return None when it is not an integer.

    Used by every endpoint that reads an int from a query param or JSON body so
    malformed input becomes a 400 instead of an uncaught ValueError.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_host(base: str | None) -> str | None:
    """The host (hostname[:port]) of a base URL, for audit provenance. No key,
    no path, no scheme."""
    try:
        parts = urllib.parse.urlsplit(base or "")
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        return f"{host}:{port}"
    return host


def _parse_models(raw: bytes) -> list[str]:
    """Extract up to 200 model ids from an OpenAI ``{"data":[{"id":..}]}`` or
    Ollama ``{"models":[{"name":..}]}`` response. Each id truncated to 100 chars,
    non-strings dropped."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return []
    ids: list[str] = []
    if isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("id"), str):
                    ids.append(it["id"])
        if not ids:
            models = data.get("models")
            if isinstance(models, list):
                for it in models:
                    if isinstance(it, dict) and isinstance(it.get("name"), str):
                        ids.append(it["name"])
    out: list[str] = []
    for i in ids:
        if isinstance(i, str):
            out.append(i[:100])
        if len(out) >= 200:
            break
    return out


def _public_origin() -> str | None:
    """The normalized origin of ``TRIVIA_PUBLIC_BASE_URL`` (scheme + netloc,
    no path/query/fragment, no trailing slash) when set and syntactically valid;
    otherwise None (caller falls back to direct-LAN ``Host``)."""
    raw = (os.environ.get("TRIVIA_PUBLIC_BASE_URL") or "").strip()
    if not raw:
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _join_url(code: str, host: str | None = None) -> str:
    """The public join URL for a room code, using the configured public origin
    when set and the request ``Host`` (direct LAN) otherwise."""
    origin = _public_origin() or f"http://{host or 'localhost'}"
    return f"{origin}/join?code={code}"

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
# Finished jobs stay pollable this long, then are dropped (they hold the
# generated questions, so keeping them forever grows memory per game).
_JOB_TTL = 3600


def _prune_jobs() -> None:
    """Drop finished jobs older than ``_JOB_TTL``. Caller holds ``_JOBS_LOCK``."""
    cutoff = time.time() - _JOB_TTL
    for jid in [j for j, v in _JOBS.items() if v["done"] and v.get("created", cutoff) < cutoff]:
        del _JOBS[jid]

# Global concurrency cap for /api/settings/test. At most 2 in-flight
# outbound probes; a third returns 503 "test in progress" without connecting.
_TEST_SEM = threading.BoundedSemaphore(2)

# One generation call at a time (a local model is a single shared GPU): at
# most one question-generation call runs across the legacy /api/generate path
# and the /api/game/configure background jobs.
_GEN_SEM = threading.BoundedSemaphore(1)

# Per-game "configuration epoch": the game_id -> latest configure job_id. A
# background job only continues writing while it is still the current epoch for
# its game; a newer configure (or a delete) supersedes it and the stale job
# stops without recreating rows.
_GAME_EPOCH: dict[int, str] = {}
_EPOCH_LOCK = threading.Lock()

MAX_BODY = 64 * 1024
# The import endpoints take pasted question lists, which dwarf a normal action.
IMPORT_MAX_BODY = 256 * 1024
# One saved round never holds more than this many imported questions.
IMPORT_ROUND_MAX = 200

# Per-client rate-limit budgets (per minute, keyed on the resolved client IP).
# Join and answer are split into separate buckets: a burst of joins must
# not starve answer submissions, and answers (the hot path during a round) get
# a higher budget than joins. Both are far above the old single 30/min bucket.
JOIN_RATE_LIMIT = 60
ANSWER_RATE_LIMIT = 120


def _parse_trusted_proxies(raw: str) -> set[str]:
    """Parse ``TRIVIA_TRUSTED_PROXIES`` (comma-separated IPs or CIDRs) into a set.

    Empty input -> empty set, which means "trust no proxy" (the safe default:
    ``X-Forwarded-For`` is ignored and the rate limiter keys on the direct peer).
    """
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


def _proxy_networks(entries) -> list:
    """IP / CIDR strings -> networks. Unparseable entries are dropped."""
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(e, strict=False))
        except ValueError:
            pass
    return nets


class _RateLimiter:
    def __init__(self, per_minute: int = 30):
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < 60]
            if len(hits) >= self.per_minute:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True


class App:
    def __init__(self, db_path: str, *, join_rate_limit: int = JOIN_RATE_LIMIT,
                 answer_rate_limit: int = ANSWER_RATE_LIMIT,
                 settings_rate_limit: int = 10,
                 trusted_proxies: set[str] | None = None):
        self.engine = engine.GameEngine(db_path)
        self.join_ratelimit = _RateLimiter(per_minute=join_rate_limit)
        self.answer_ratelimit = _RateLimiter(per_minute=answer_rate_limit)
        self.settings_ratelimit = _RateLimiter(per_minute=settings_rate_limit)
        self.trusted_proxies = (
            trusted_proxies if trusted_proxies is not None
            else _parse_trusted_proxies(os.environ.get("TRIVIA_TRUSTED_PROXIES", ""))
        )
        self._proxy_nets = _proxy_networks(self.trusted_proxies)

    def is_trusted_proxy(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._proxy_nets)

    # ---- generation jobs ---------------------------------------------------
    def start_generation(self, game_id: int, topic: str, count: int,
                         *, adlib: bool = False) -> str:
        job_id = engine.secrets.token_urlsafe(16)
        with _JOBS_LOCK:
            _prune_jobs()
            _JOBS[job_id] = {"created": time.time(),
                             "game_id": game_id, "topic": topic, "count": count,
                             "done": False, "accepted": 0, "rejected": 0,
                             "error": None, "adlib": adlib}
        t = threading.Thread(target=self._run_generation, args=(job_id,), daemon=True)
        t.start()
        return job_id

    def _run_generation(self, job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS[job_id]
        accepted: list = []
        rejected: list = []
        audit_rows: list = []
        error: str | None = None
        # ONE immutable snapshot resolved at job start, passed to both
        # the generator and the audit writer. A save landing mid-job must not
        # change which model actually ran, or the provenance recorded for it.
        snapshot = {"provider": "", "base_url": "",
                    "model": "", "api_key": None}
        try:
            snapshot = settings.resolve(self.engine.db_path)
            # Fail early with a readable message instead of a doomed 401 call.
            no_key = settings.missing_key_error(snapshot)
            if no_key:
                raise settings.MissingKeyError(no_key)
            # One generation call at a time: serialize generation across the
            # legacy path and the configure background jobs.
            with _GEN_SEM:
                result = gen.generate_questions(
                    job["topic"], job["count"], model=snapshot["model"],
                    base=snapshot["base_url"], api_key=snapshot["api_key"])
            accepted = result["accepted"]
            rejected = result["rejected"]
            audit_rows = result["audit"]
            if accepted:
                self.engine.add_questions(job["game_id"], accepted)
        except Exception as e:  # surface, never crash the thread
            error = (str(e) if isinstance(e, settings.MissingKeyError)
                     else f"{type(e).__name__}: {e}")
        finally:
            with _JOBS_LOCK:
                j = _JOBS[job_id]
                j["done"] = True
                if error:
                    j["error"] = error
                else:
                    j["accepted"] = len(accepted)
                    j["rejected"] = len(rejected)
                    j["questions"] = accepted
            # Persist the audit trail regardless of outcome. The
            # summary row records the SAME snapshot the generator used.
            try:
                self.engine.record_generation(
                    job_id=job_id, game_id=job["game_id"], topic=job["topic"],
                    model=snapshot["model"], count=job["count"],
                    accepted=len(accepted), rejected=len(rejected),
                    audit_rows=audit_rows, error=error,
                    base_host=_base_host(snapshot["base_url"]),
                    provider=snapshot["provider"])
            except Exception:
                pass

    def job_status(self, job_id: str) -> dict | None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j or j.get("kind") in ("configure", "import"):
                return None
            return {k: v for k, v in j.items()
                    if k not in ("questions", "audit", "created")}

    # ---- configuration jobs (/api/game/configure) --------------------------
    def start_configure(self, game_id: int, steps: list[dict]) -> str:
        """Register a configure job and run it in a background thread (position
        order). Returns the job id."""
        job_id = engine.secrets.token_urlsafe(16)
        with _JOBS_LOCK:
            _prune_jobs()
            _JOBS[job_id] = {
                "created": time.time(),
                "kind": "configure", "game_id": game_id, "steps": steps,
                "done": False, "error": None,
                "rounds": {s["position"]: {"status": "pending", "error": None,
                                           "question_count": 0} for s in steps},
            }
        with _EPOCH_LOCK:
            _GAME_EPOCH[game_id] = job_id
        threading.Thread(target=self._run_configure, args=(job_id,), daemon=True).start()
        return job_id

    def _configure_current(self, game_id: int, job_id: str) -> bool:
        with _EPOCH_LOCK:
            return _GAME_EPOCH.get(game_id) == job_id

    def _run_configure(self, job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS[job_id]
        game_id = job["game_id"]
        e = self.engine
        for step in job["steps"]:
            pos = step["position"]
            rid = step["round_id"]
            if not self._configure_current(game_id, job_id) or not e.game_exists(game_id):
                break  # superseded by a newer configure or a delete
            if step["library_round_id"]:
                n = e.attach_library_round(game_id, rid, step["library_round_id"],
                                           step["category"])
                with _JOBS_LOCK:
                    if n is None:
                        job["rounds"][pos] = {"status": "cancelled", "error": None,
                                              "question_count": 0}
                    else:
                        job["rounds"][pos] = {"status": "ready", "error": None,
                                              "question_count": n}
                if n is None:
                    break
                continue
            e.set_round_status(rid, "generating")
            with _JOBS_LOCK:
                job["rounds"][pos]["status"] = "generating"
            try:
                snapshot = settings.resolve(e.db_path)
                no_key = settings.missing_key_error(snapshot)
                if no_key:
                    raise settings.MissingKeyError(no_key)
                with _GEN_SEM:  # one generation call at a time
                    result = gen.generate_questions(
                        step["category"], step["question_count"],
                        model=snapshot["model"], base=snapshot["base_url"],
                        api_key=snapshot["api_key"])
                accepted = result["accepted"]
                if not accepted:
                    raise RuntimeError(
                        f"generation produced no questions "
                        f"(shortfall {result.get('shortfall')})")
                # Re-check after the (slow) generation: a configure or delete
                # may have landed mid-flight; never write to a superseded game.
                if not self._configure_current(game_id, job_id) or not e.game_exists(game_id):
                    break
                lr_id = e.create_library_round(step["category"], accepted)
                n = e.attach_library_round(game_id, rid, lr_id, step["category"])
                with _JOBS_LOCK:
                    if n is None:
                        job["rounds"][pos] = {"status": "cancelled", "error": None,
                                              "question_count": 0}
                    else:
                        job["rounds"][pos] = {"status": "ready", "error": None,
                                              "question_count": n}
                if n is None:
                    break
            except Exception as ex:  # mark the round failed, then continue
                err = (str(ex) if isinstance(ex, settings.MissingKeyError)
                       else f"{type(ex).__name__}: {ex}")
                if self._configure_current(game_id, job_id) and e.game_exists(game_id):
                    e.set_round_status(rid, "failed", err)
                with _JOBS_LOCK:
                    job["rounds"][pos] = {"status": "failed", "error": err,
                                          "question_count": 0}
        with _JOBS_LOCK:
            job["done"] = True

    def configure_job_status(self, job_id: str) -> dict | None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j or j.get("kind") != "configure":
                return None
            return {"job_id": job_id, "game_id": j["game_id"], "done": j["done"],
                    "error": j.get("error"),
                    "rounds": {str(k): dict(v) for k, v in j["rounds"].items()}}

    # ---- question import (/api/import/*) -----------------------------------
    def save_import_rounds(self, questions: list[dict], default_category: str) -> list[dict]:
        """Store questions as immutable library rounds, one per category (a
        question's own ``category`` wins over *default_category*), at most
        ``IMPORT_ROUND_MAX`` each. Returns ``[{id, category, question_count}]``."""
        groups: dict[str, list[dict]] = {}
        for q in questions:
            cat = (q.get("category") or default_category or "Imported")
            groups.setdefault(str(cat)[:importer.CATEGORY_MAX], []).append(q)
        rounds = []
        for cat, qs in groups.items():
            for i in range(0, len(qs), IMPORT_ROUND_MAX):
                chunk = qs[i:i + IMPORT_ROUND_MAX]
                lr_id = self.engine.create_library_round(cat, chunk, source="imported")
                rounds.append({"id": lr_id, "category": cat, "question_count": len(chunk)})
        return rounds

    def start_import(self, mode: str, *, text: str, category: str, count: int,
                     items: list[dict]) -> str:
        job_id = engine.secrets.token_urlsafe(16)
        with _JOBS_LOCK:
            _prune_jobs()
            _JOBS[job_id] = {
                "created": time.time(), "kind": "import", "mode": mode,
                "text": text, "category": category, "count": count, "items": items,
                "truncated": mode == "material" and len(text) > gen.MAX_MATERIAL_CHARS,
                "done": False, "error": None, "accepted": 0, "rejected": [],
                "rounds": [], "shortfall": 0,
            }
        threading.Thread(target=self._run_import, args=(job_id,), daemon=True).start()
        return job_id

    def _run_import(self, job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS[job_id]
        mode = job["mode"]
        requested = job["count"] if mode == "material" else len(job["items"])
        accepted: list = []
        rejected: list = []
        audit_rows: list = []
        rounds: list = []
        shortfall = 0
        error: str | None = None
        # Same single snapshot as _run_generation: the audit row names
        # the model that actually ran.
        snapshot = {"provider": "", "base_url": "", "model": "", "api_key": None}
        try:
            snapshot = settings.resolve(self.engine.db_path)
            # Rows that are already complete need no model, so no key either.
            if mode == "material" or any(gen.needs_completion(q) for q in job["items"]):
                no_key = settings.missing_key_error(snapshot)
                if no_key:
                    raise settings.MissingKeyError(no_key)
            llm = dict(model=snapshot["model"], base=snapshot["base_url"],
                       api_key=snapshot["api_key"])
            with _GEN_SEM:  # one at a time: shared with every other generation path
                if mode == "fill":
                    result = gen.complete_questions(job["items"], **llm)
                else:
                    result = gen.generate_from_material(job["text"], job["count"], **llm)
            accepted, rejected = result["accepted"], result["rejected"]
            audit_rows = result["audit"]
            shortfall = result.get("shortfall", 0)
            if accepted:
                rounds = self.save_import_rounds(accepted, job["category"])
            else:
                failure = next((r["reason"] for r in audit_rows
                                if r.get("status") == "error"), None)
                if failure:
                    error = "The AI model could not complete this: " + failure
        except Exception as e:  # surface, never crash the thread; never log pasted text
            error = (str(e) if isinstance(e, settings.MissingKeyError)
                     else f"{type(e).__name__}: {e}")
        finally:
            with _JOBS_LOCK:
                job["done"] = True
                job["error"] = error
                job["accepted"] = len(accepted)
                job["rejected"] = [
                    {"stem": (r.get("stem") or "")[:200], "answer": (r.get("answer") or "")[:200],
                     "reason": r.get("reason", "")} for r in rejected[:100]]
                job["rounds"] = rounds
                job["shortfall"] = shortfall
                job["text"] = job["items"] = None  # pasted content is not kept after the run
            try:
                self.engine.record_generation(
                    job_id=job_id, game_id=None, topic=f"import:{mode}",
                    model=snapshot["model"], count=requested,
                    accepted=len(accepted), rejected=len(rejected),
                    audit_rows=audit_rows, error=error,
                    base_host=_base_host(snapshot["base_url"]),
                    provider=snapshot["provider"])
            except Exception:
                pass

    def import_job_status(self, job_id: str) -> dict | None:
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if not j or j.get("kind") != "import":
                return None
            return {k: (list(v) if isinstance(v, list) else v) for k, v in j.items()
                    if k in ("done", "error", "accepted", "rejected", "rounds",
                             "shortfall", "truncated")}


def _json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler, max_body: int = MAX_BODY) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        length = -1
    if length < 0 or length > max_body:
        # A negative length would make rfile.read() read to EOF, unbounded.
        # The unread body is still on the socket, so drop the connection.
        handler.close_connection = True
        return None
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set by the server factory
    server_version = "QuestionableIntel/0.1"
    protocol_version = "HTTP/1.1"
    # Socket timeout (seconds). Without it a client that opens a connection and
    # never finishes its request holds a server thread forever. Clients poll
    # every ~1 s, so keep-alive connections never sit idle this long.
    timeout = 30

    def log_message(self, fmt, *args):  # quiet by default; keep access log minimal
        pass

    def end_headers(self):
        # Single choke-point for security headers: every response, including
        # error paths and 404s, gets them.
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def _token(self, qs: dict) -> str:
        """The caller's host or team token. Pages send it as ``X-Token`` so it
        stays out of proxy access logs; the ``token`` query parameter is still
        accepted for scripts."""
        return self.headers.get("X-Token") or (qs.get("token") or [""])[0]

    def _client_ip(self) -> str:
        # Rate-limit key: trust ``X-Forwarded-For`` only when the direct
        # peer is an explicitly configured proxy (``TRIVIA_TRUSTED_PROXIES``).
        # Otherwise every phone behind the proxy (e.g. NPM) would share one
        # bucket, so a room full of teams would trip the limiter at once.
        #
        # Walk ``X-Forwarded-For`` from the right, skipping trusted hops, and
        # key on the first untrusted one (what nginx's real_ip_recursive does).
        # Through the tunnel the chain is "<client>, <cloudflared as NPM sees
        # it>", so trusting only NPM would put every internet phone in one
        # bucket. Entries left of the first untrusted hop are client-supplied
        # and never read.
        direct = self.client_address[0]
        if not self.app.is_trusted_proxy(direct):
            return direct
        hops = [h.strip() for h in self.headers.get("X-Forwarded-For", "").split(",")
                if h.strip()]
        for hop in reversed(hops):
            if not self.app.is_trusted_proxy(hop):
                return hop
        return hops[0] if hops else direct

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        self._handle(self._route_get)

    def do_POST(self):
        self._handle(self._route_post)

    def _handle(self, route) -> None:
        try:
            route()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            try:
                self._json_error(500, "internal error")
            except Exception:
                pass

    def _route_get(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path in ("/", ""):
            self._redirect("/console")
        elif path in ("/console", "/stage", "/join", "/play"):
            self._serve_static(path.lstrip("/") + ".html")
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/qr":
            self._serve_qr(qs)
        elif path == "/healthz":
            self._healthz()
        elif path == "/api/state":
            self._api_state(qs)
        elif path == "/api/settings":
            self._api_settings_get(qs)
        elif path == "/api/games":
            self._api_list_games()
        elif path == "/api/library/rounds":
            self._api_library_rounds()
        elif path == "/api/generate/status":
            self._api_gen_status(qs)
        elif path == "/api/configure/status":
            self._api_configure_status(qs)
        elif path == "/api/import/status":
            self._api_import_status(qs)
        elif path == "/favicon.ico":
            self.send_response(404); self.end_headers()
        else:
            self._json_error(404, "not found")

    def _route_post(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/game/create":
            self._api_create_game()
        elif path == "/api/game/join":
            self._api_join()
        elif path == "/api/game/select":
            self._api_select_game()
        elif path == "/api/game/delete":
            self._api_delete_game()
        elif path == "/api/game/configure":
            self._api_configure()
        elif path == "/api/game/teams":
            self._api_teams()
        elif path == "/api/answer":
            self._api_answer()
        elif path == "/api/team/rename":
            self._api_rename()
        elif path == "/api/action":
            self._api_action()
        elif path == "/api/generate":
            self._api_generate()
        elif path == "/api/settings":
            self._api_settings_save()
        elif path == "/api/settings/test":
            self._api_settings_test()
        elif path == "/api/import/parse":
            self._api_import_parse()
        elif path == "/api/import/save":
            self._api_import_save()
        elif path == "/api/import/ai":
            self._api_import_ai()
        else:
            self._json_error(404, "not found")

    # ---- helpers -----------------------------------------------------------
    def _redirect(self, loc: str):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_error(self, status: int, msg: str, errors: list[str] | None = None):
        payload: dict = {"error": msg}
        if errors is not None:
            payload["errors"] = errors
        _json(self, payload, status)

    def _serve_static(self, rel: str):
        # Percent-decode first (so %2e%2e / %2f forms are caught too),
        # reject any traversal segment outright, then contain by resolving both
        # paths to real paths and requiring the result to stay under STATIC_DIR.
        rel = urllib.parse.unquote(rel)
        if "\\" in rel or "\x00" in rel or rel.startswith("/") or ".." in rel.split("/"):
            self._json_error(404, "not found")
            return
        root = os.path.realpath(STATIC_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            self._json_error(404, "not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_qr(self, qs: dict):
        if not self.app.join_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too many attempts — wait a moment")
            return
        code = (qs.get("code") or [""])[0].strip().upper()
        # Room codes are exactly 4 chars from the room alphabet. Bound the
        # input so an oversized query cannot drive a huge QR encode.
        if not re.fullmatch(r"[A-Z0-9]{4}", code):
            self._json_error(400, "invalid room code")
            return
        host = self.headers.get("Host", "localhost")
        url = _join_url(code, host)
        png = qr.qr_png(url, scale=10)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(png)

    def _healthz(self):
        # Liveness + DB reachability: a fresh connection must be able to
        # run ``SELECT 1``. Docker/compose poll this endpoint to mark the
        # container healthy; a dead DB reports 503 so the healthcheck fails.
        if self.app.engine.healthcheck():
            _json(self, {"status": "ok"})
        else:
            _json(self, {"status": "error", "error": "database unreachable"}, 503)

    # ---- API ---------------------------------------------------------------
    def _api_create_game(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        name = (body.get("name") or "Trivia Night")[:80]
        result = self.app.engine.create_game(name)
        _json(self, result, 201)

    def _api_list_games(self):
        if not self._check_admin():
            return
        _json(self, {"games": self.app.engine.list_games()})

    def _api_library_rounds(self):
        if not self._check_admin():
            return
        _json(self, {"rounds": self.app.engine.list_library_rounds()})

    def _api_select_game(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        meta = self.app.engine.select_game(game_id)
        if meta is None:
            self._json_error(404, "no such game")
            return
        _json(self, meta)

    def _api_delete_game(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        if body.get("confirm") is not True:
            self._json_error(400, "confirm must be true")
            return
        if not self.app.engine.delete_game(game_id):
            self._json_error(404, "no such game")
            return
        _json(self, {"ok": True, "deleted": game_id})

    def _api_configure(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        errors, steps = self.app.engine.configure_game(game_id, body.get("rounds"))
        if errors:
            if "no such game" in errors:
                self._json_error(404, "no such game")
            else:
                self._json_error(400, "invalid configuration", errors=errors)
            return
        job_id = self.app.start_configure(game_id, steps)
        _json(self, {"job_id": job_id, "game_id": game_id}, 202)

    def _api_join(self):
        if not self.app.join_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too many attempts — wait a moment")
            return
        body = _read_json(self) or {}
        code = (body.get("code") or "").strip().upper()
        game = self.app.engine.game_by_code(code)
        if not game:
            self._json_error(404, "no game with that code")
            return
        team_id = body.get("team_id")
        if team_id is not None:
            team_id = _parse_int(team_id)
            if team_id is None:
                self._json_error(400, "team_id must be an integer")
                return
        try:
            result = self.app.engine.join_team(game["id"], body.get("team_name"),
                                               team_id=team_id, pin=body.get("pin"))
        except ValueError as e:
            self._json_error(400, str(e))
            return
        result["game_id"] = game["id"]
        result["code"] = game["code"]
        _json(self, result)

    def _api_teams(self):
        if not self.app.join_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too many attempts — wait a moment")
            return
        body = _read_json(self) or {}
        code = (body.get("code") or "").strip().upper()
        game = self.app.engine.game_by_code(code)
        if not game:
            self._json_error(404, "no game with that code")
            return
        _json(self, {"teams": self.app.engine.list_teams(game["id"])})

    def _api_answer(self):
        if not self.app.answer_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too fast")
            return
        body = _read_json(self) or {}
        team = self.app.engine.team_by_token(body.get("join_token") or "")
        if not team:
            self._json_error(401, "unknown team session")
            return
        result = self.app.engine.submit_answer(team["id"], team["game_id"],
                                               body.get("answer_text") or "")
        _json(self, result)

    def _api_rename(self):
        if not self.app.answer_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too fast")
            return
        body = _read_json(self) or {}
        team = self.app.engine.team_by_token(body.get("join_token") or "")
        if not team:
            self._json_error(401, "unknown team session")
            return
        ok = self.app.engine.rename_team(team["game_id"], team["id"], body.get("name"))
        _json(self, {"ok": ok})

    def _api_state(self, qs: dict):
        game_id = _parse_int((qs.get("game_id") or [None])[0])
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        role = (qs.get("role") or ["team"])[0]
        snap = self.app.engine.snapshot(game_id, role, self._token(qs))
        if snap is None:
            self._json_error(401, "not authorized")
            return
        # The public join URL (TRIVIA_PUBLIC_BASE_URL origin when set, else the
        # request Host for direct-LAN play) so the stage can render QR/join
        # links against the correct origin.
        snap["join_url"] = _join_url(snap["code"], self.headers.get("Host"))
        _json(self, snap)

    def _api_gen_status(self, qs: dict):
        job_id = (qs.get("job_id") or [""])[0]
        is_admin, gid = self._auth_context(qs)
        if not is_admin and gid is None:
            self._json_error(401, "not authorized")
            return
        st = self.app.job_status(job_id)
        if st is None or (not is_admin and st["game_id"] != gid):
            self._json_error(404, "no such job")
            return
        _json(self, st)

    def _api_configure_status(self, qs: dict):
        job_id = (qs.get("job_id") or [""])[0]
        is_admin, gid = self._auth_context(qs)
        if not is_admin and gid is None:
            self._json_error(401, "not authorized")
            return
        st = self.app.configure_job_status(job_id)
        if st is None or (not is_admin and st["game_id"] != gid):
            self._json_error(404, "no such job")
            return
        _json(self, st)

    # ---- question import ---------------------------------------------------
    def _import_body(self) -> dict | None:
        """The JSON object body of an import request (up to ``IMPORT_MAX_BODY``),
        or None after writing a 400 that says what to do."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = -1
        if length > IMPORT_MAX_BODY:
            self.close_connection = True
            self._json_error(400, f"That is too much text to send at once (limit "
                                  f"{IMPORT_MAX_BODY // 1024} KB). Split it into smaller imports.")
            return None
        body = _read_json(self, IMPORT_MAX_BODY)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            self._json_error(400, 'Send a JSON object with a "text" string.')
            return None
        return body

    @staticmethod
    def _import_category(body: dict) -> str:
        cat = body.get("category")
        cat = " ".join(cat.split()) if isinstance(cat, str) else ""
        return cat[:importer.CATEGORY_MAX]

    def _api_import_parse(self):
        if not self._check_admin():
            return
        body = self._import_body()
        if body is None:
            return
        result = importer.parse(body["text"], swap=bool(body.get("swap")))
        result["counts"] = importer.counts(result["questions"])
        _json(self, result)

    def _api_import_save(self):
        if not self._check_admin():
            return
        body = self._import_body()
        if body is None:
            return
        parsed = importer.parse(body["text"], swap=bool(body.get("swap")))
        rows = []
        for q in parsed["questions"]:  # re-parsed here: client-side rows are never trusted
            if q["status"] == "complete":
                rows.append(q)
            elif q["status"] == "needs_answer" and body.get("include_incomplete") is True:
                # Host judges it live. Typed, never auto-scored: a blank answer
                # must not match a team that submits a blank.
                rows.append(dict(q, answer="", type="text", options=None))
        skipped = len(parsed["questions"]) - len(rows)
        if not rows:
            c = importer.counts(parsed["questions"])
            reason = (parsed["errors"][0]["message"] if not parsed["questions"] and parsed["errors"]
                      else f"{c['needs_answer'] + c['needs_question']} row(s) are missing a "
                           "question or an answer. Add them, or use Let AI fill the gaps.")
            self._json_error(400, "Nothing to save: " + reason, errors=parsed["errors"])
            return
        questions = [{"stem": q["stem"], "answer": q["answer"], "options": q["options"],
                      "type": q["type"], "points": q["points"], "category": q["category"],
                      "source": "imported"} for q in rows]
        rounds = self.app.save_import_rounds(questions, self._import_category(body))
        _json(self, {"rounds": rounds, "skipped": skipped, "errors": parsed["errors"]}, 201)

    def _api_import_ai(self):
        if not self._check_admin():
            return
        body = self._import_body()
        if body is None:
            return
        mode = body.get("mode")
        if mode not in ("fill", "material"):
            self._json_error(400, 'mode must be "fill" or "material"')
            return
        text = body["text"]
        count, items = 0, []
        if mode == "material":
            raw_count = body.get("count")
            count = _parse_int(raw_count) if raw_count is not None else 10
            if count is None or not (1 <= count <= 40):
                self._json_error(400, "count must be 1..40")
                return
            if not text.strip():
                self._json_error(400, "Paste the notes or article to write questions from.")
                return
        else:
            parsed = importer.parse(text, swap=bool(body.get("swap")))
            items = parsed["questions"]
            if not items:
                msg = parsed["errors"][0]["message"] if parsed["errors"] else importer.EMPTY_MESSAGE
                self._json_error(400, msg, errors=parsed["errors"])
                return
        job_id = self.app.start_import(mode, text=text, category=self._import_category(body),
                                       count=count, items=items)
        _json(self, {"job_id": job_id}, 202)

    def _api_import_status(self, qs: dict):
        if not self._check_admin():
            return
        st = self.app.import_job_status((qs.get("job_id") or [""])[0])
        if st is None:
            self._json_error(404, "no such job")
            return
        _json(self, st)

    def _auth_context(self, qs: dict) -> tuple[bool, int | None]:
        """Authorize a job-status request. Returns ``(is_admin, host_game_id)``:
        admin token (>=16 chars, constant-time) grants ``is_admin``; otherwise a
        valid host token for ``game_id`` grants ``host_game_id``. Returns
        ``(False, None)`` for an anonymous caller, who learns nothing (no job
        existence, no metadata)."""
        expected = os.environ.get("TRIVIA_ADMIN_TOKEN") or ""
        provided = self.headers.get("X-Admin-Token") or ""
        if expected and len(expected) >= 16 and hmac.compare_digest(provided, expected):
            return True, None
        gid = _parse_int((qs.get("game_id") or [None])[0])
        if gid is not None and self.app.engine.verify_host(gid, self._token(qs)):
            return False, gid
        return False, None

    def _api_action(self):
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        token = body.get("token") or ""
        if not self.app.engine.verify_host(game_id, token):
            self._json_error(401, "not authorized")
            return
        action = body.get("action")
        params = body.get("params") or {}
        try:
            err = self._dispatch_action(game_id, action, params)
        except (ValueError, TypeError) as e:
            self._json_error(400, f"invalid parameter: {e}")
            return
        if err:
            self._json_error(400, err)
        else:
            _json(self, {"ok": True})

    def _dispatch_action(self, game_id: int, action: str, params: dict) -> str | None:
        e = self.app.engine
        return {
            "start": lambda: e.start_game(game_id),
            "reveal": lambda: e.reveal(game_id),
            "next": lambda: e.next_question(game_id),
            "skip": lambda: e.skip(game_id),
            "void": lambda: e.void(game_id),
            "next_round": lambda: e.next_round(game_id),
            "finish": lambda: e.finish(game_id),
            "undo": lambda: e.undo(game_id),
            "award": lambda: e.award(game_id, int(params.get("team_id") or 0),
                                     int(params.get("delta") or 0),
                                     params.get("reason") or "manual"),
            "set_points": lambda: e.set_points(game_id, int(params.get("points") or 10)),
            "start_clock": lambda: e.start_clock(game_id, params.get("duration")),
            "pause_clock": lambda: e.pause_clock(game_id),
            "extend_clock": lambda: e.extend_clock(game_id, int(params.get("seconds") or 30)),
            "adlib": lambda: e.adlib(game_id, params.get("stem") or "",
                                     params.get("answer") or "",
                                     params.get("options"),
                                     int(params.get("points") or 10),
                                     params.get("type") or "mc"),
            "add_question": lambda: e.add_question(game_id, {
                "stem": params.get("stem") or "", "answer": params.get("answer") or "",
                "options": params.get("options"), "points": int(params.get("points") or 10),
                "type": params.get("type") or "mc", "source": "authored"}),
        }.get(action, lambda: "unknown action")()

    def _api_generate(self):
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        token = body.get("token") or ""
        if not self.app.engine.verify_host(game_id, token):
            self._json_error(401, "not authorized")
            return
        topic = (body.get("topic") or "").strip()
        raw_count = body.get("count")
        count = _parse_int(raw_count) if raw_count is not None else 5
        if count is None:
            self._json_error(400, "count must be an integer")
            return
        if not topic:
            self._json_error(400, "topic required")
            return
        if not (1 <= count <= 40):
            self._json_error(400, "count must be 1..40")
            return
        job_id = self.app.start_generation(game_id, topic, count,
                                           adlib=bool(body.get("adlib")))
        _json(self, {"job_id": job_id}, 202)

    # ---- LLM settings -----------------------------
    def _check_admin(self) -> bool:
        """Management auth: ``X-Admin-Token`` must equal
        ``TRIVIA_ADMIN_TOKEN`` (constant-time compare). Unset or shorter than 16
        chars locks every management endpoint. Checked BEFORE any game_id/body
        parsing. Writes the 403 response itself on failure."""
        expected = os.environ.get("TRIVIA_ADMIN_TOKEN") or ""
        if len(expected) < 16:
            self._json_error(403, "management locked: set TRIVIA_ADMIN_TOKEN "
                                  "(at least 16 characters)")
            return False
        provided = self.headers.get("X-Admin-Token") or ""
        if not hmac.compare_digest(provided, expected):
            self._json_error(403, "admin token rejected")
            return False
        return True

    def _settings_error(self, exc: Exception) -> None:
        # Log only the exception type — never user input or header
        # values, which could carry the admin token or API key.
        sys.stderr.write(f"[settings] {type(exc).__name__}\n")
        sys.stderr.flush()
        try:
            self._json_error(500, "settings error")
        except Exception:
            pass

    def _api_settings_get(self, qs):
        try:
            self._settings_get(qs)
        except Exception as e:
            self._settings_error(e)

    def _settings_get(self, qs):
        if not self._check_admin():
            return
        game_id = _parse_int((qs.get("game_id") or [None])[0])
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        if not self.app.engine.verify_host(game_id, self._token(qs)):
            self._json_error(401, "not authorized")
            return
        _json(self, settings.public_view(
            settings.resolve(self.app.engine.db_path)))

    def _api_settings_save(self):
        try:
            self._settings_save()
        except Exception as e:
            self._settings_error(e)

    def _settings_save(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        token = body.get("token") or ""
        if not self.app.engine.verify_host(game_id, token):
            self._json_error(401, "not authorized")
            return

        allowed = {"game_id", "token", "provider", "base_url", "model",
                   "api_key", "clear_api_key"}
        unknown = [k for k in body if k not in allowed]
        if unknown:
            self._json_error(400, "invalid settings",
                             errors=[f"unknown key: {k}" for k in unknown])
            return

        clear = bool(body.get("clear_api_key"))
        has_key_field = "api_key" in body and body["api_key"] is not None
        # api_key:"" = unchanged; clear only via clear_api_key:true;
        # providing both is contradictory -> 400, nothing persisted.
        if clear and has_key_field:
            self._json_error(400, "provide either api_key or clear_api_key, not both")
            return

        updates: dict = {}
        for field in ("provider", "base_url", "model"):
            value = body.get(field)
            if value is not None and value != "":
                updates[field] = value
        if has_key_field and body["api_key"] != "":
            updates["api_key"] = body["api_key"]
        if clear:
            updates["api_key"] = ""  # delete the override

        try:
            resolved = settings.save(self.app.engine.db_path, updates)
        except settings.SettingsError as e:
            self._json_error(400, "invalid settings", errors=e.errors)
            return
        _json(self, settings.public_view(resolved))

    def _api_settings_test(self):
        try:
            self._settings_test()
        except Exception as e:
            self._settings_error(e)

    def _settings_test(self):
        if not self._check_admin():
            return
        body = _read_json(self) or {}
        game_id = _parse_int(body.get("game_id"))
        if game_id is None:
            self._json_error(400, "game_id must be an integer")
            return
        token = body.get("token") or ""
        if not self.app.engine.verify_host(game_id, token):
            self._json_error(401, "not authorized")
            return
        # Rate-limit (10/min/IP) then the global concurrency cap.
        if not self.app.settings_ratelimit.allow(self._client_ip()):
            self._json_error(429, "too many attempts — wait a moment")
            return
        if not _TEST_SEM.acquire(blocking=False):
            self._json_error(503, "test in progress")
            return
        try:
            self._run_settings_test(body)
        finally:
            _TEST_SEM.release()

    def _run_settings_test(self, body):
        resolved = settings.resolve(self.app.engine.db_path)
        draft_base = body.get("base_url") or resolved["base_url"]

        # Validate the draft without persisting anything.
        updates: dict = {}
        for field in ("provider", "base_url", "model", "api_key"):
            value = body.get(field)
            if value is not None and value != "":
                updates[field] = value
        errors = settings.validate(updates, resolved=resolved)
        if errors:
            self._json_error(400, "invalid settings", errors=errors)
            return

        url = settings.normalize_base_url(draft_base) + "/models"

        # Key egress: send a key only when (a) the body supplies one, or
        # (b) the draft base_url is byte-identical (normalized) to the resolved
        # base_url. Otherwise send NO Authorization header and flag
        # key_required_for_new_host.
        headers: dict = {}
        key_required_for_new_host = False
        supplied_key = (body.get("api_key") or "").strip()
        same_host = (settings.normalize_base_url(draft_base)
                     == settings.normalize_base_url(resolved["base_url"]))
        if supplied_key:
            headers["Authorization"] = "Bearer " + supplied_key
        elif same_host:
            if resolved["api_key"]:
                headers["Authorization"] = "Bearer " + resolved["api_key"]
        else:
            key_required_for_new_host = True

        t0 = time.monotonic()
        try:
            status, raw = netutil.open_no_redirect(url, headers=headers,
                                                   timeout=5.0, max_bytes=1 << 20)
            latency_ms = int((time.monotonic() - t0) * 1000)
            result = {"ok": True, "status": status, "latency_ms": latency_ms,
                      "models": _parse_models(raw), "error": None}
        except netutil.NetError as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            result = {"ok": False, "status": e.status, "latency_ms": latency_ms,
                      "models": [], "error": e.reason}
        if key_required_for_new_host:
            result["key_required_for_new_host"] = True
        _json(self, result)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    Handler.app = app
    return ThreadingHTTPServer((host, port), Handler)
