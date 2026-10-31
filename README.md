# Questionable Intel

A self-hosted trivia-night app. You run the game from a laptop and put the
stage on a TV or projector; players join from their own phones in a browser. No
app to install, no accounts, no sign-up. It is pure Python standard library plus
vanilla HTML/CSS/JS, with nothing to `pip install` and no build step. Questions
come from an AI model, from your own lists, or both. It also works with no AI at
all.

*Not affiliated with or endorsed by the U.S. Army or any government body; the
military theme is decorative.*

- **Host console** (`/console`) — runs the game: next / reveal / award / skip /
  undo, a shared clock, hold-to-peek answers, and free-text judging.
- **Stage** (`/stage`) — the full-screen projector/TV surface: QR join, big-type
  questions, reveal, scoreboard, podium.
- **Phone** (`/join` → `/play`) — 4-character room code (or QR), team pick,
  answer in portrait on a phone-sized screen.

Two ways to run it: **LAN only** (the default — laptop and phones on the same
network, nothing reachable from the internet) or **secured public web** behind a
Cloudflare Tunnel and a reverse proxy. Game management is protected by an admin
token in both modes. Deployment, HTTPS and day-of workflows:
**[docs/WEB_DEPLOYMENT.md](docs/WEB_DEPLOYMENT.md)**.

---

## Quick start (Docker)

You need Docker with Compose. The image is published at
`ghcr.io/mknerr/questionable-intel`.

**Management needs an admin token** (16+ characters; it unlocks creating,
configuring and deleting games and the AI settings). Generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # 32 chars
# or: openssl rand -base64 24
```

Leave it unset and management stays **locked** (secure by default): players can
still join, but nobody can create a game.

**With Docker Compose:**

```bash
mkdir questionable-intel && cd questionable-intel
curl -O https://raw.githubusercontent.com/mknerr/questionable-intel/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/mknerr/questionable-intel/main/.env.example
# Edit .env: set TRIVIA_ADMIN_TOKEN, and TRIVIA_LLM_API_KEY if you want AI questions
docker compose up -d
```

**Or a single `docker run`:**

```bash
docker run -d --name questionable-intel -p 8130:8130 \
  -v trivia-data:/data \
  -e TRIVIA_ADMIN_TOKEN=<your generated token> \
  -e TRIVIA_LLM_API_KEY=<your DeepSeek key, optional> \
  ghcr.io/mknerr/questionable-intel:latest
```

Then open **http://localhost:8130/console**, enter the admin token to unlock
management, and start a game. Phones on the same network use the laptop's LAN
address instead of `localhost` (for example `http://192.168.1.20:8130/join`);
the stage's QR code does this for them.

## Quick start (no Docker)

Requires Python 3.11 or newer. There are no third-party packages to install.

```bash
git clone https://github.com/mknerr/questionable-intel.git
cd questionable-intel
export TRIVIA_ADMIN_TOKEN=<your generated token>
export TRIVIA_LLM_API_KEY=<your DeepSeek key, optional>
python3 app.py
```

Configuration is read from environment variables only. A `.env` file is read by
Docker Compose, not by `app.py`, so export the variables in your shell (or load
the file yourself, for example `set -a; . ./.env; set +a`). Then open:

| Surface | URL |
|---|---|
| Host console | http://localhost:8130/console |
| Stage (projector) | http://localhost:8130/stage#g=`<game id>`&t=`<host token>` — use the console's **Open stage** link (a bare `/stage` shows *"No game"*) |
| Phone join | http://localhost:8130/join |

---

## Getting questions in

You can mix all of these in one game, round by round.

### AI generation

The host console generates questions from a category you type, using any
OpenAI-compatible chat endpoint. The default is **DeepSeek**: create an API key
at [platform.deepseek.com](https://platform.deepseek.com) and set it as
`TRIVIA_LLM_API_KEY` (or paste it into the console's **AI settings** panel). In
that panel you can switch, with no restart, to **OpenAI**, a **local Ollama**
(`http://host.docker.internal:11434/v1` from inside Docker) or any other
**OpenAI-compatible URL**. See [Change the model](#change-the-model-from-the-console).

Every generated question passes a nine-check validation gate before it can reach
a player: well-formed, four distinct choices, the answer present exactly once,
no answer leaking into the question, no near-duplicate, sane lengths, a spread of
answer positions, a model confidence that is not "low", and a screen for facts
that go stale (rejected unless anchored to a year). Rejected questions are logged
and shown to the host, never to players.

**AI-written questions can still be wrong.** The gate catches malformed,
duplicated and leaking questions, not confidently stated false facts. Skim the
questions before game night.

### Import without AI

Paste text or upload a `.txt`, `.csv`, `.tsv` or `.json` file in the console's
**Import questions** panel. The format is detected for you: TSV and CSV (with
or without a header row), Quizlet exports, JSON (including Open Trivia DB
downloads), `Q:` / `A:` blocks, lettered multiple choice (including Moodle's
Aiken format), or just one question per line. Preview shows what was understood
before anything is saved; **Save as round** puts it in your round library. See
**[docs/IMPORT.md](docs/IMPORT.md)** for every format.

### Paste a draft and let AI finish it

If an AI model is set up, import has two extra buttons. **Let AI fill the gaps**
completes half-done rows (a question without an answer, an answer without a
question, or no wrong choices), keeping your wording. **Write questions from this
text** turns pasted notes or an article into questions drawn only from that text.
Neither is required.

**The whole app works with no LLM at all:** import your own lists, save them as
library rounds, and play. Only AI generation needs a model.

---

## Typical flow

Open `/console` → enter the admin token to unlock management → *Start a new game*
(or pick a previous game) → configure 1–12 rounds (a category, default 10
questions, 1–40 adjustable; or a saved library round) → round 1 can start as soon
as it is ready, later rounds keep generating in the background → click *Open
stage* (put that window on the TV, full-screen) → teams scan the QR or type the
room code → *Start* → play.

The console reopens the last game after a reload. **Manage games** (under *Open
stage*) goes back to the start screen and its game list, which shows when each
game was created and last played; *Back to game* returns without rotating the
host token. The admin token field is a username/password pair, so a password
manager can save and fill it — the page itself never stores it.

## Configuration

All via environment variables (see [`.env.example`](.env.example) for the full
list). The important ones:

| Var | Default | Purpose |
|---|---|---|
| `TRIVIA_PORT` | `8130` | bind port |
| `TRIVIA_HOST` | `0.0.0.0` | bind address (LAN) |
| `TRIVIA_DATA_DIR` | `./data` | where `trivia.db` lives (running `app.py` directly; in Docker it is always `/data` in the container) |
| `TRIVIA_DATA_MOUNT` | `trivia-data` | Docker Compose only: what is mounted at `/data`. The default is a Docker-managed named volume. Set `./data` to keep the database in a host directory (it must be writable by uid 10001: `mkdir data && sudo chown 10001:10001 data`) |
| `TRIVIA_IMAGE` | `ghcr.io/mknerr/questionable-intel:latest` | Docker Compose only: image to run (pin a version, or point at your own build) |
| `TRIVIA_LLM_BASE` | `https://api.deepseek.com` | OpenAI-compatible endpoint — **default only; the console overrides it** |
| `TRIVIA_LLM_MODEL` | `deepseek-flash` | model id — **default only; the console overrides it** |
| `TRIVIA_LLM_API_KEY` | *(empty)* | bearer token. **Required for DeepSeek and OpenAI**; optional for local servers — **default only; the console overrides it** |
| `TRIVIA_ADMIN_TOKEN` | *(empty)* | **16+ characters.** Authorizes all game management — create/list/select/delete/configure, round-library browsing — and the console **AI settings** panel. Sent as `X-Admin-Token`, page-session-only in the console. **Unset or shorter than 16 chars ⇒ management and the panel are locked (`403`)** |
| `TRIVIA_PUBLIC_BASE_URL` | *(empty)* | public HTTPS origin (e.g. `https://trivia.example.com`) used for QR codes and join links. Unset ⇒ derived from the request `Host` — correct for direct LAN use |
| `TRIVIA_TRUSTED_PROXIES` | *(empty)* | comma-separated proxy IPs/CIDRs to skip when reading `X-Forwarded-For` (rate-limit keying). Behind a reverse proxy plus a tunnel: the proxy's IP and the tunnel connector's Docker network |

**Env is the default, not the last word.** Provider, base URL, model and API key
resolve in this order: **console (saved in the AI settings panel) → environment
variable → built-in default.** A saved override wins at runtime with no restart.

## Change the model from the console

Provider, base URL, model and API key can all change from the host console — no
`.env` edit, no rebuild, no restart.

One-time setup: give the app an admin token of at least 16 characters (in `.env`
or the compose environment), then restart so it can read it. The same token
unlocks game management; it is separate from each game's host token.

Then, with the app running:

1. Open `/console` and enter the game (the game's **host token** is still
   required — it is a separate credential from the admin token).
2. Open the **AI settings** panel.
3. Type the admin token into **Admin token**. It is held for this page session
   only; the browser never saves it, so after a reload you re-enter it.
4. Pick a preset (DeepSeek, **Local Ollama**, OpenAI) or type a **custom base
   URL**.
5. Press **Detect models**, then pick the model from the list.
6. Press **Save**.

The panel header then shows the effective config and where each field came from
(`from console`, `from env`, or the built-in default). The next generation uses
those values immediately.

Two things the panel will refuse, on purpose:

- **Plain `http` to a public host while an API key is set** —
  `base_url: http not allowed to a public host when an API key is set`. Use
  `https://` for anything outside your LAN; private (RFC1918) addresses over
  `http` are fine.
- **Loopback, link-local or cloud-metadata addresses** — `base_url: blocked
  target`. Point the app at your LAN box (for example `192.168.1.50`), or at
  `host.docker.internal` from a container, not `127.0.0.1` or `169.254.169.254`.

**Rollback:** the console wins only while its rows exist. Deleting the
`app_settings` rows hands control back to env and the built-in default
immediately, with no restart:

```bash
sqlite3 data/trivia.db "DELETE FROM app_settings;"
# Docker (the image has no sqlite3 CLI, but it has Python):
docker compose exec trivia-knight python3 -c \
  "import sqlite3; c = sqlite3.connect('/data/trivia.db'); c.execute('DELETE FROM app_settings'); c.commit()"
```

**Clear stored key** only removes the saved API key — the saved provider, base URL
and model stay in place. To undo just the key, clear it; to undo everything,
delete the rows.

**If the panel says `settings locked: set TRIVIA_ADMIN_TOKEN`**, that is
secure-by-default, not a bug: without an admin token the settings endpoints
refuse every call. Set the variable and restart once.

---

## Running the tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'     # scoring, gate, settings, import, netutil (offline)
python3 smoke_test.py                                      # full 2-team round over HTTP
python3 smoke_gen.py "1980s music" 10                      # live generation against your configured LLM
```

The unit tests and the smoke test need no network and no API key. A
`RuntimeError: boom` traceback in the unit-test output is a test's intentional
failure. `smoke_gen.py` calls the configured model and needs a key (or a local
server).

---

## Docker details

`docker-compose.yml` pulls the published image and runs it as a non-root user;
the single SQLite file lives in a Docker-managed named volume by default (see
`TRIVIA_DATA_MOUNT` above for a host directory instead).

```bash
docker compose up -d              # start (pulls the published image)
docker compose ps                 # shows health (GET /healthz -> healthy)
docker compose down               # stop; the data volume stays
docker compose pull               # refresh the published image
```

**Building from source.** With a checkout of this repository,
`docker compose up -d --build` builds the image from the `Dockerfile` and tags it
with the image name, so later plain `up` runs reuse it. A plain `up` never
rebuilds from changed source, so after editing code use `--build`.

The compose file templates `TRIVIA_LLM_BASE`, `TRIVIA_LLM_MODEL`, the
`TRIVIA_PORT` mapping and the admin/public-origin/proxy variables from `.env`, so
edits there take effect on the next `up` (no need to edit `docker-compose.yml`).
A `/healthz` endpoint (200 + `SELECT 1`) is wired to a Dockerfile `HEALTHCHECK`
and a compose `healthcheck:` stanza — `docker compose ps` reporting `(healthy)`
is the real check, not an open port. To reach an Ollama server on the Docker host
from the container, use `http://host.docker.internal:11434/v1` (Compose maps it
for you; with plain `docker run` add `--add-host=host.docker.internal:host-gateway`
on Linux).

**Two supported shapes:**

- **LAN only (default, safe).** The app binds `0.0.0.0:8130` on the local
  network. Nothing is reachable from the internet; no tunnel route and no
  router port-forward are needed.
- **Secured public web (optional).** Cloudflare Tunnel → a reverse proxy such as
  Nginx Proxy Manager → this container, with HTTPS and a public origin for QR
  codes. Set `TRIVIA_ADMIN_TOKEN`, `TRIVIA_PUBLIC_BASE_URL` and
  `TRIVIA_TRUSTED_PROXIES` as described in
  **[docs/WEB_DEPLOYMENT.md](docs/WEB_DEPLOYMENT.md)**. There is still no router
  port-forward, and a local LLM server is never exposed.

A Cloudflare tunnel route is only safe *with* the deployment steps in that
guide — a tunnel in front of an unconfigured app just publishes it.

> **Compose service name.** The Compose service and container are still called
> `trivia-knight`, so commands like `docker compose exec trivia-knight …` use that
> name.

---

## Architecture

Pure Python standard library (`http.server`/`ThreadingHTTPServer`, `sqlite3`,
`json`, `urllib`) + vanilla HTML/CSS/JS. No build step, no Node pipeline. Fonts
are served locally from `static/fonts/`, so game night needs no internet.

| File | Role |
|---|---|
| `app.py` | entry point — env, DB init, server boot |
| `server.py` | HTTP routing, API, static serving, generation jobs |
| `engine.py` | game state machine, derived scoring, undo |
| `gen.py` | OpenAI-compatible LLM client + the 9-check validation gate + retry |
| `importer.py` | question import: parses pasted/uploaded text (TSV, CSV, Quizlet, JSON, `Q:`/`A:`, lettered choices) with no LLM |
| `settings.py` | LLM settings resolution (console → env → default), validation, persistence |
| `netutil.py` | outbound HTTP for LLM calls: no redirects, blocked-target guard, deadline + size cap |
| `db.py` | SQLite schema (WAL) |
| `qr.py` / `qrcodegen.py` | server-side QR (MIT, vendored — see `THIRD_PARTY_LICENSES.md`) |
| `static/` | host console, stage, join, play, plus shared `style.css`, `theme.js`, and fonts |
| `smoke_test.py`, `smoke_gen.py`, `tests/` | verification |

State is held server-side in a single SQLite file (`trivia.db`, WAL mode); the
browser is a dumb view, so a console refresh resumes the exact question, clock
and scores (no lost state).

---

## Security posture

- **Safe by default on the LAN; public exposure is opt-in.** The app binds
  `0.0.0.0:8130` for the local network. If you want phones anywhere, front it
  with the Cloudflare Tunnel → reverse proxy HTTPS setup in
  [docs/WEB_DEPLOYMENT.md](docs/WEB_DEPLOYMENT.md) — never with a router
  port-forward, and never by exposing a local LLM server.
- **Admin token gates all management.** Game create/list/select/delete/configure,
  round-library browsing and AI settings require `TRIVIA_ADMIN_TOKEN` (≥16 chars,
  sent as the `X-Admin-Token` header, constant-time compared). Unset or shorter
  ⇒ management is locked (`403`) — secure-by-default. The token lives in the
  server environment and in the console's page session only: not in URLs, not in
  `localStorage`/`sessionStorage`, not in the database. It is a different
  credential from a per-game host token and from a contestant room code.
- **Per-game host token** (≥128-bit, `secrets.token_urlsafe(32)`, stored hashed,
  compared with `hmac.compare_digest`) authorizes that game's host actions and
  stage/host snapshots. Room codes are for *joining* only — they never authorise
  reveal/score/reset, and they cannot reach any management endpoint.
- **Selecting a previous game rotates its host token** and returns the new one
  only to the admin-authorized console; old console/stage host links stop
  working. Deleting a game requires confirmation, removes that game's data, and
  leaves reusable library rounds intact.
- **The proxy is not an authorization layer.** Behind Cloudflare and a reverse
  proxy, every management and host check still runs in the application. TLS
  terminates at the proxy; the app reads `X-Forwarded-For` only when the direct
  peer is in `TRIVIA_TRUSTED_PROXIES`, and keys on the rightmost hop that is not.
- **Outbound requests are constrained** (SSRF): the only server-side fetch is to
  the configured LLM endpoint, through a transport that refuses loopback,
  link-local and cloud-metadata targets, never follows redirects, and caps
  response size and time. Media URLs are rendered client-side on the stage and
  never fetched server-side.
- **The correct answer is withheld** from phones and the stage until reveal.
- **No contestant accounts** (by design): joining is by the 4-character room
  code plus a team session. Each team gets a 6-digit **rejoin PIN**, shown on
  its phone and in the host console; rejoining an existing team from another
  phone needs it, so seeing the code on the TV is not enough to take over a
  team. Joining and the team list are rate-limited per IP, and a game holds at
  most 40 teams. Contestants cannot create, configure, select or delete games
  or browse the library. Keep host tokens off the projector/TV.
- **Tokens travel in the `X-Token` header**, not the URL, so they stay out of
  proxy access logs. The `token` query parameter is still accepted for scripts.
- **Parameterised SQL only**; no `eval`/`exec`; no secrets in the repo.
- **The database holds secrets.** `trivia.db` stores the LLM provider API key in
  **plaintext** and the game host tokens as hashes — the same exposure as
  `.env`, and encryption with a key sitting next to the file would be theatre.
  Treat the DB file as a credential: **do not paste it into tickets, issue
  reports, backups, screenshots, or chat.** The data directory is created `0700`
  and `trivia.db*` is `0600`; keep those modes on any copy or backup you make.
- **AI settings have two locks.** Changing the LLM endpoint requires
  `TRIVIA_ADMIN_TOKEN` *and* a valid per-game host token. With no admin token,
  the panel is locked (`403 settings locked: set TRIVIA_ADMIN_TOKEN`) — that is
  secure-by-default, not a bug.

---

## Content policy

**AI-written questions** are machine output and are not original works. They are
screened by the validation gate but not fact-checked, so the host should skim them
before the night. The gate catches malformed, duplicated, leaking and
stale-pattern questions, not confidently wrong facts.

**Imported questions are the host's responsibility.** The importer parses
whatever you give it; it does not check where the text came from. Don't import
material you don't have the rights to use, such as published quiz books, board
game card sets or scraped trivia sites, and do not feed such material into the
AI helpers either. No third-party question dataset is bundled with this project;
see `THIRD_PARTY_LICENSES.md` for the components that are.

## License

MIT — see [`LICENSE`](LICENSE).
