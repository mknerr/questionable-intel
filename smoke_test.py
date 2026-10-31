"""End-to-end smoke test: boots the real server and plays a full two-team
round over HTTP — create game -> 2 teams join -> questions served -> answers
submitted -> reveal -> scores -> final scoreboard. Prints a transcript and
asserts every step. No network beyond loopback; questions are seeded via the
host 'add_question' action (authored), so this does not depend on Ollama.

Run:  python3 smoke_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = None  # set after finding a free port
ADMIN_TOKEN = "smoke-test-admin-token-123456789"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, path, body=None, expect=200, headers=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=15) as r:
        code = r.status
        payload = r.read().decode()
    if expect and code != expect:
        raise AssertionError(f"{method} {path} -> {code} (expected {expect}): {payload[:300]}")
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        parsed = payload
    return code, parsed


def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    global BASE
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="trivia-smoke-")
    env = dict(os.environ)
    env.update({
        "TRIVIA_HOST": "127.0.0.1",
        "TRIVIA_PORT": str(port),
        "TRIVIA_DATA_DIR": os.path.join(tmp, "data"),
        "TRIVIA_LLM_BASE": "http://127.0.0.1:1/v1",  # never called in this test
        "TRIVIA_ADMIN_TOKEN": ADMIN_TOKEN,
    })
    BASE = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen([sys.executable, "app.py"], env=env,
                            cwd=os.path.dirname(os.path.abspath(__file__)),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        # wait for readiness
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/console", timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        else:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError("server did not boot:\n" + out)

        # Host view + phone join view both HTTP 200
        c, _ = http("GET", "/console")
        step(f"GET /console -> {c} (host view)")
        c, _ = http("GET", "/join")
        step(f"GET /join -> {c} (phone join view)")

        # create game (management API now requires the admin token)
        _, g = http("POST", "/api/game/create", {"name": "Smoke Night"}, expect=201,
                    headers={"X-Admin-Token": ADMIN_TOKEN})
        host_token, code = g["host_token"], g["code"]
        step(f"created game id={g['game_id']} code={code}")

        # 2 teams join
        _, ta = http("POST", "/api/game/join", {"code": code, "team_name": "Wet Bandits"})
        _, tb = http("POST", "/api/game/join", {"code": code, "team_name": "Sticky Bandits"})
        step(f"team A '{ta['name']}' joined (id={ta['team_id']})")
        step(f"team B '{tb['name']}' joined (id={tb['team_id']})")

        def host_action(action, params=None):
            return http("POST", "/api/action",
                        {"game_id": g["game_id"], "token": host_token,
                         "action": action, "params": params or {}})

        # seed 2 MC questions
        host_action("add_question", {"stem": "What is the capital of France?",
                                     "answer": "Paris",
                                     "options": ["London", "Berlin", "Madrid", "Paris"],
                                     "points": 10, "type": "mc"})
        host_action("add_question", {"stem": "Which planet is known as the Red Planet?",
                                     "answer": "Mars",
                                     "options": ["Venus", "Jupiter", "Saturn", "Mars"],
                                     "points": 10, "type": "mc"})
        step("seeded 2 multiple-choice questions")

        # start
        host_action("start")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        assert s["state"] == "question", s["state"]
        assert s["question"]["stem"].startswith("What is the capital"), s["question"]
        step("game started -> question 1 open")

        # Team role must NOT see the answer before reveal
        _, ts = http("GET", f"/api/state?game_id={g['game_id']}&role=team&token={ta['join_token']}")
        assert "answer" not in ts["question"], "answer leaked to team before reveal!"
        step("verified: correct answer withheld from team before reveal")

        # both teams answer Q1 (A correct, B wrong)
        http("POST", "/api/answer", {"join_token": ta["join_token"], "answer_text": "Paris"})
        http("POST", "/api/answer", {"join_token": tb["join_token"], "answer_text": "London"})
        step("Q1 answered: A=Paris (correct), B=London (wrong)")

        host_action("reveal")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        totals = {t["name"]: t["total"] for t in s["teams"]}
        assert totals["Wet Bandits"] == 10 and totals["Sticky Bandits"] == 0, totals
        step(f"Q1 reveal -> totals {totals}")

        # next question
        host_action("next")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        assert s["state"] == "question" and "Red Planet" in s["question"]["stem"], s
        step("advanced -> question 2 open")

        http("POST", "/api/answer", {"join_token": ta["join_token"], "answer_text": "Mars"})
        http("POST", "/api/answer", {"join_token": tb["join_token"], "answer_text": "Mars"})
        step("Q2 answered: both correct")

        host_action("reveal")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        totals = {t["name"]: t["total"] for t in s["teams"]}
        assert totals["Wet Bandits"] == 20 and totals["Sticky Bandits"] == 10, totals
        step(f"Q2 reveal -> totals {totals}")

        # round end -> final
        host_action("next")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        assert s["state"] == "round_end", s["state"]
        step("round_end scoreboard shown")

        host_action("next_round")
        _, s = http("GET", f"/api/state?game_id={g['game_id']}&role=host&token={host_token}")
        assert s["state"] == "final", s["state"]
        ranked = sorted(s["teams"], key=lambda t: -t["total"])
        assert ranked[0]["name"] == "Wet Bandits" and ranked[0]["total"] == 20, ranked
        assert ranked[1]["name"] == "Sticky Bandits" and ranked[1]["total"] == 10, ranked
        step(f"FINAL: 1st {ranked[0]['name']} {ranked[0]['total']} pts, "
             f"2nd {ranked[1]['name']} {ranked[1]['total']} pts")

        print("\nSMOKE TEST PASSED — full two-team round completed over HTTP.\n")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        # surface any server stderr that accumulated
        if proc.stdout:
            try:
                out = proc.stdout.read()
            except Exception:
                out = ""
        if out.strip():
            print("--- server output ---\n" + out)


if __name__ == "__main__":
    sys.exit(main())
