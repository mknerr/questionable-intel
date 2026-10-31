# Secure web deployment and host/contestant workflows

This is the operator's guide to running Questionable Intel two ways:

- **LAN only (default, safe):** laptop on the same network as the phones; nothing
  is reachable from the internet. The Quick start in the [README](../README.md)
  is enough.
- **Public web (optional):** a Cloudflare Tunnel (named `my-tunnel` in this guide)
  → a reverse proxy such as Nginx Proxy Manager (NPM) at `192.168.1.10:443` → the
  Questionable Intel container. Phones anywhere can join over HTTPS at
  `https://trivia.example.com`.

Public exposure is a deliberate choice, not a requirement. Both modes run the
same image and the same application-level authorization checks.

Throughout this guide, substitute your own values for the examples:
`trivia.example.com` (your public hostname), `192.168.1.10` (your reverse proxy's
LAN address), `my-tunnel` (your tunnel's name). Never reuse a placeholder as a
real secret.

---

## 1. Three credentials — don't mix them up

| Credential | What it unlocks | Where it lives | Lifetime |
|---|---|---|---|
| **Admin token** — `TRIVIA_ADMIN_TOKEN` | All game management: create, list, select, delete, configure; browsing the round library; AI settings | Server environment; typed into the console, sent as the `X-Admin-Token` header | Until you change the env value and restart |
| **Per-game host token** | One game: host console actions (`/api/action`), host/stage snapshots, legacy `/api/generate` | Minted per game, stored hashed in SQLite, held by the console/stage pages | Rotated whenever that game is selected from the previous-games list |
| **Room code** (4 characters) | Joining a game as a new team — nothing else | Printed on the stage / encoded in the QR | Life of the game |
| **Team rejoin PIN** (6 digits) | Rejoining an existing team from a different phone | Shown on the team's phone and in the host console | Life of the team |

**Admin token rules**

- **At least 16 characters.** Shorter than 16 (or unset) counts as *not
  configured*, and management stays **locked**. Generate something long:
  `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` — that is 24
  random bytes, 32 URL-safe characters.
- Sent **only** as the `X-Admin-Token` request header. It is never a contestant
  credential, never accepted in a query string or JSON body, and never placed in
  a URL by the app.
- The console keeps it in a **page-session-only** JavaScript variable. It is not
  written to `localStorage` or `sessionStorage`, so a reload means typing it
  again. That is intentional.
- Changing it means editing the environment and restarting the container. Old
  admin tokens stop working immediately after the restart.

**"Locked" is secure-by-default, not a bug.** With no admin token (or one that
is too short), management endpoints refuse every call and the console's
management surfaces stay locked. There is no fallback to "anyone on the LAN".

**Why both?** Host actions and stage snapshots need that game's host token. The
AI settings panel needs both the admin token *and* a valid host token. Possessing
the admin token does not let you drive `/api/action` for an arbitrary game, and
possessing a host token does not let you create, configure or delete games.

---

## 2. Topology

```
phones / host laptop
        │  HTTPS
        ▼
Cloudflare edge  ──►  Cloudflare Tunnel "my-tunnel"
        │
        ▼
Reverse proxy (NPM) 192.168.1.10:443   (TLS terminates here)
        │  HTTP over the LAN
        ▼
questionable-intel container on the Docker host (published on :8130)
        │
        ├──► data volume: trivia.db   (single SQLite file)
        └──► LLM endpoint: DeepSeek / OpenAI over HTTPS, or a local Ollama on the
             LAN (never public)
```

Non-negotiables:

- **No router port-forward.** The only ingress is the Cloudflare Tunnel. Nothing
  publishes `8130` to the internet.
- **Never expose a local LLM server.** If you run Ollama, its port (`11434`) stays
  on the LAN; the tunnel route points at the Questionable Intel container only,
  and the app reaches Ollama as a client.
- **The proxy is not the security boundary.** NPM gives you TLS and a hostname;
  it does not authenticate anyone. Every management call is still checked
  against `TRIVIA_ADMIN_TOKEN` **inside the application**, and every host action
  against the per-game host token. Do not remove those checks because "the
  proxy is in front" — anyone who learns the hostname reaches the app directly
  through it.
- The app needs **no WebSocket support**. It is plain HTTP request/response with
  periodic polling from the console and stage. Do not enable extra upgrade
  plumbing in NPM or Cloudflare for it.

---

## 3. Deploy, step by step

Assumptions: the Docker host runs the stack from a directory you choose (this
guide uses `~/questionable-intel`), and your reverse proxy can reach the Docker
host's LAN address on port 8130. The commands use the published image
(`ghcr.io/mknerr/questionable-intel:latest`); to build from source instead, clone
the repository and add `--build` to `docker compose up`.

### Step 1 — get the compose file and decide where data lives

```bash
mkdir -p ~/questionable-intel && cd ~/questionable-intel
curl -O https://raw.githubusercontent.com/mknerr/questionable-intel/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/mknerr/questionable-intel/main/.env.example
```

All state is the single file `trivia.db` (WAL mode, so `trivia.db-wal` and `-shm`
may exist too), mounted at `/data` in the container. By default it lives in a
Docker-managed **named volume** (`trivia-data`), which needs no setup. The app
creates the data directory `0700` and the database files `0600` itself.

If you would rather keep the database in a host directory, easy to back up and
inspect, set `TRIVIA_DATA_MOUNT=./data` in `.env` and create the directory for the
container's non-root user (uid 10001) first:

```bash
mkdir data
sudo chown 10001:10001 data
sudo chmod 700 data
```

> **The database is a credential.** `trivia.db` stores the LLM provider API key
> in plaintext and the game host tokens as hashes. A backup of the data is as
> sensitive as `.env`: keep it owner-only, never attach it to a ticket, issue,
> screenshot or chat, and don't sync it to a shared folder. Restoring an old
> copy restores old games and the old LLM key with it.

### Step 2 — generate and set a strong admin token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Copy the output once into `.env` (next step). Don't paste it into shell history
on a shared box if you can avoid it; if you do, clear the history afterwards.

### Step 3 — configure the environment

Edit `.env` and set, at minimum:

```dotenv
# Public origin: scheme + host (+ port only if non-standard), no trailing slash.
# Used for QR codes and join links. Must be the HTTPS origin users actually see.
TRIVIA_PUBLIC_BASE_URL=https://trivia.example.com

# The IP the app sees as the direct peer, i.e. your reverse proxy.
# See the table below for the extra entry when the tunnel connector runs in Docker.
TRIVIA_TRUSTED_PROXIES=192.168.1.10

# 32 URL-safe characters from Step 2.
TRIVIA_ADMIN_TOKEN=<ADMIN_TOKEN>

# AI question generation: the default is DeepSeek, which needs a key
# (platform.deepseek.com). Leave this empty if you will only import questions
# or configure the provider later in the console's AI settings panel.
TRIVIA_LLM_API_KEY=<LLM_API_KEY>

# Or use a local Ollama on the LAN instead (no key needed). Reachable from inside
# the container, on the LAN, never a public address:
# TRIVIA_LLM_BASE=http://192.168.1.50:11434/v1
# TRIVIA_LLM_MODEL=qwen3.5:9b
```

`TRIVIA_PUBLIC_BASE_URL` exists so the QR code and the join link printed on the
stage are correct **before** anyone scans them. Without it the app can only
guess from the request's `Host` header — fine on the LAN, wrong behind a tunnel
where the browser-facing origin and the internal host differ. Give it the
origin only (scheme + host + port if non-standard); the app uses it as an
origin, so a path or trailing slash adds nothing.

`TRIVIA_TRUSTED_PROXIES` is a comma-separated list of IPs or CIDRs: the proxy
hops between the internet and the app. It only decides which client address
keys the rate limiter. Leave it empty for direct LAN use. Never add
`0.0.0.0/0`, a hostname, or a range that holds your phones.

The app walks `X-Forwarded-For` from the right, skips every trusted hop, and
keys on the first untrusted one (the same rule as nginx's
`real_ip_recursive`). Entries to the left of that are client-supplied and never
read. NPM appends the address it saw to the chain, so:

| Path | `X-Forwarded-For` reaching the app | Keyed on |
|---|---|---|
| LAN phone → NPM | `192.168.1.55` | the phone |
| Internet → Cloudflare → cloudflared → NPM | `<client>, 172.16.x.1` | the client |

If `cloudflared` runs in a Docker network on the same host as NPM, NPM sees it as
a `172.16.0.0/12` gateway address (Docker's private ranges). The working value is
then:

```dotenv
TRIVIA_TRUSTED_PROXIES=192.168.1.10,172.16.0.0/12
```

If your connector runs elsewhere, list the address your proxy actually sees it
as. With only NPM's IP listed, every internet phone shares the cloudflared
bucket, and one client exhausting it blocks joins for everyone. No NPM header
rewriting is needed. Keep `X-Forwarded-Proto: https`.

### Step 4 — start

```bash
docker compose up -d
```

This pulls the published image; the container restarts `unless-stopped`. `.env`
is loaded by `env_file:` and also feeds the `${…}` interpolation in
`docker-compose.yml`, so the port mapping and LLM defaults come from the same
file. To run your own build instead, use a source checkout and
`docker compose up -d --build`.

### Step 5 — verify the container is actually healthy

Health is a real check, not an open port: Docker runs
`GET /healthz`, which returns `200 {"status":"ok"}` only if the database answers
`SELECT 1` (dead DB → `503`).

```bash
docker compose ps
# STATUS should read "Up ... (healthy)"

curl -fsS http://127.0.0.1:8130/healthz
# {"status": "ok"}
```

If `ps` says `starting` for more than ~45 seconds, or `unhealthy`, check
`docker compose logs --tail=100`. A `503` from `/healthz` means the data
directory isn't writable by the container user or the DB is corrupt — fix that
before wiring the tunnel.

### Step 6 — DNS, tunnel, NPM, HTTPS

1. **DNS:** point `trivia.example.com` at the tunnel (a Cloudflare-managed CNAME
   record for the `my-tunnel` tunnel). No A record to a home IP, no port-forward.
2. **Tunnel:** add a public hostname route for `trivia.example.com` to
   `https://192.168.1.10:443` (NPM) — in the Zero Trust dashboard or the
   tunnel's `config.yml` ingress, whichever manages your `my-tunnel` tunnel — with
   **no path restrictions**; the app serves `/console`, `/stage`, `/join`,
   `/play`, `/qr` and `/api/*` from one origin.
3. **NPM:** create a Proxy Host for `trivia.example.com` →
   `http://<DOCKER_HOST_LAN_IP>:8130`, enable **Block Common Exploits**,
   request/attach a certificate (Cloudflare Origin Cert or Let's Encrypt), force
   SSL, and set HTTP/2. Leave WebSocket support **off** — it isn't needed. NPM's
   default `X-Forwarded-For` handling is what Step 3's `TRIVIA_TRUSTED_PROXIES`
   expects; no custom headers.
4. **Cloudflare:** SSL/TLS mode Full (strict) so the edge verifies NPM's cert.
   Don't add WAF rules that block `/api/*`; the app's own checks are the
   authorization layer.

Then confirm the public origin end to end:

```bash
curl -fsS https://trivia.example.com/healthz
# {"status": "ok"}
```

### Step 7 — acceptance tests before the first real night

Run these against the **public** origin; they are the checks that catch a
mis-wired proxy, a missing token or a leaked QR link.

**A. Management is locked without the admin token**

```bash
# No header -> refused (403). This must NOT create a game.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://trivia.example.com/api/game/create \
  -H 'Content-Type: application/json' -d '{"name":"probe"}'

# Same call with the header -> 201 and a host token.
curl -s -X POST https://trivia.example.com/api/game/create \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $TRIVIA_ADMIN_TOKEN" \
  -d '{"name":"deploy probe"}'
```

Also check that a short token (e.g. 15 characters) behaves like "unset": set it,
restart, and confirm management is still locked.

**B. Host management works in the console**

Open `https://trivia.example.com/console`, unlock management with the admin
token, create a game, configure two rounds (one category round, one library
round) and watch the per-round progress. Round 1 should become startable first;
`Next round` must refuse to advance into a round that is still generating. Open
the stage from the console and confirm the QR/join URL shows
`https://trivia.example.com/join?code=…` — not an internal IP, not `http://`.

**C. A contestant cannot manage anything**

On a phone, open `https://trivia.example.com/join`, enter only the 4-character
room code, pick or create a team, and answer a question. Then, from the same
phone session (room code + team session, no admin token), try each management
call:

```bash
# All of these must be refused (403) from a browser/phone that only knows the
# room code and its own team session. CONTESTANT_TOKEN stands for the team's
# `join_token` (returned by /api/game/join) — it is not an admin credential.
curl -s -o /dev/null -w 'list=%{http_code}\n'  https://trivia.example.com/api/games
curl -s -o /dev/null -w 'lib=%{http_code}\n'   https://trivia.example.com/api/library/rounds
curl -s -o /dev/null -w 'create=%{http_code}\n' -X POST https://trivia.example.com/api/game/create \
  -H 'Content-Type: application/json' -H "X-Admin-Token: $CONTESTANT_TOKEN" \
  -d '{"name":"contestant probe"}'
curl -s -o /dev/null -w 'config=%{http_code}\n' -X POST https://trivia.example.com/api/game/configure \
  -H 'Content-Type: application/json' -H "X-Admin-Token: $CONTESTANT_TOKEN" \
  -d '{"game_id":1,"rounds":[{"category":"x"}]}'
curl -s -o /dev/null -w 'delete=%{http_code}\n' -X POST https://trivia.example.com/api/game/delete \
  -H 'Content-Type: application/json' -H "X-Admin-Token: $CONTESTANT_TOKEN" \
  -d '{"game_id":1,"confirm":true}'
```

The point of the last three is that even a *real* team session token cannot
stand in for the admin token. The first two show the endpoints are closed to a
plain anonymous browser.

**D. Anonymous generation status is closed**

`GET /api/generate/status?job_id=…` without admin or matching host authorization
must be refused; job IDs and progress are not public metadata.

**E. Proxy headers**

Confirm `X-Forwarded-For` handling by watching the join rate limiter with two
devices on different networks (see Step 3) — the app's own limiter is the
observable signal. If both devices throttle together, the trusted-proxy value or
NPM's forwarding is wrong.

---

## 4. Host workflow on the night

1. **Before the night:** set `TRIVIA_ADMIN_TOKEN` and restart once. Pre-generate
   or pre-configure rounds while you still have time — generation can take tens
   of seconds per round and must never gate the room.
2. **Open the console:** `https://trivia.example.com/console`. Unlock management
   with the admin token (page session only — a refresh means re-entering it).
3. **Pick or create a game.** The previous-games list shows each game's name,
   room code, state, and round/question counts. Creating a game requires the
   admin token.
4. **Configure rounds:** choose 1–12 rounds. Each round is either:
   - a **category**, with a question count (default **10**, allowed **1–40**), or
   - a **library round** — reusable questions saved from an earlier generation
     or import. The game gets its own deep copy; later edits to the game (or
     deleting it) never change the library.
5. **Wait only for round 1.** Round 1 can start as soon as it is ready. Later
   rounds keep generating in the background and each shows its own progress and
   any error. `Next round` refuses to advance into a round that isn't ready
   yet, and the current question is left untouched — nothing is skipped.
6. **Open the stage** from the console (that link carries the current host
   token; keep it off the projector's browser history if the TV is public).
7. **Play:** teams scan the QR or type the room code, then use Start / Reveal /
   Next / Skip / Void / Next round / Finish, the shared clock, hold-to-peek and
   free-text judging as before.

**Selecting a previous game rotates its host token.** That is the security
behaviour, not a glitch: selecting hands the authorized host a *new* host token,
and every older console or stage link for that game stops working
(`Not authorized — open stage from the host console.` on the stage). Open a
fresh stage from the console after selecting. Any other device still holding the
old token is locked out — which is exactly what you want if a link leaked.

**Deleting a game** asks for confirmation, then removes that game's data —
questions, teams, answers, scores and action log — while **reusable library
rounds survive**. Deleting cannot be undone; the library is the way to keep the
good questions.

## 5. Contestant workflow

1. Scan the stage QR or open `https://trivia.example.com/join`.
2. Type the **4-character room code** (or arrive with it already in the link).
3. Create a team with a team name, or pick an existing team and enter its
   6-digit PIN (shown on the team's first phone and in the host console).
4. Answer on the phone. Refresh/reconnect returns to the same team and question.

Contestants have **no accounts and no passwords**. The room code lets a phone
create a new team; rejoining an existing team from another phone also needs
that team's PIN, because everyone holding the code can see the team list.
Joining, the team list and the QR endpoint share a per-IP rate limit, and a
game holds at most 40 teams. Contestants cannot create, configure, select or
delete games, browse the round library, or reach AI settings — those calls need
the admin token, which never leaves the host's console.

---

## 6. Restart, rollback, and caveats

**Interrupted generation.** Generation runs in a background job. If the
container restarts mid-generation, pending/generating rounds are marked
interrupted/failed rather than being served as if ready — you will never get a
phantom round with missing questions. Rounds already ready stay ready. To
recover, re-run the configuration for the affected rounds — but configuration
only applies to a game still in its lobby, so do this before the night starts.
Once a game has started, create a new game rather than trying to reconfigure the
running one. Never advance into a failed round expecting content.

**Rollback** (data preserved):

```bash
cd ~/questionable-intel
docker compose down                 # stop and remove the container; the data stays
# Pin a previous published image (tags: latest, a version, or sha-<short commit>):
TRIVIA_IMAGE=ghcr.io/mknerr/questionable-intel:<previous-tag> docker compose up -d
```

If you build from source instead, `git checkout <previous-good-sha>` and run
`docker compose up -d --build`.

`docker compose down` does **not** delete the data: with the default named volume
it survives unless you add `-v`, and a bind-mounted `./data` is never touched.
The database, games, teams and saved LLM key survive a stop, a rebuild and an
image rollback. Only deleting the volume or directory yourself (or pointing the
mount somewhere fresh) destroys it — don't do either casually. The database
schema migrates forward automatically on start and is idempotent; an older image
may not read a newer schema, so when you downgrade across a schema change, roll
back the image and restore a matching data snapshot together.

**Host-token rotation caveat.** Selecting a game invalidates the previous host
token. If you keep a stage window open across a select/re-select, reload it from
the console. Rejoining by game ID + host token only works until the next select.

**No WebSocket requirement.** Everything is HTTP: polling for state, a POST for
each action. Cloudflare and NPM need no special upgrade handling.

**Long generation and local-model contention.** Rounds generate one at a time, in
order. With a local model (Ollama), configuring many large rounds (e.g. 12 × 40)
can occupy the GPU for a long while — round 1 is unaffected once ready, but keep
the total sane on a shared box.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Console says management is locked | `TRIVIA_ADMIN_TOKEN` unset, shorter than 16 characters, or the container didn't pick up `.env` | Set a 32-char token, `docker compose up -d` (recreates with the new env), then confirm the length without printing the secret: `docker compose exec trivia-knight sh -c 'printf %s "$TRIVIA_ADMIN_TOKEN" \| wc -c'` (expect `32`) |
| `403` on every `/api/game/*` call with the right token | Token typed with stray whitespace/quote, or a different `.env` than the running stack | Re-enter it in the console; check the running env |
| QR/join link shows an internal IP or `http://` | `TRIVIA_PUBLIC_BASE_URL` missing or not the public origin | Set `https://trivia.example.com`, recreate the container |
| Every phone gets `429 too many attempts` at once | `TRIVIA_TRUSTED_PROXIES` empty while behind NPM, or NPM not forwarding a clean `X-Forwarded-For` | Set it to NPM's IP plus cloudflared's Docker network (see Step 3) |
| One phone can forge its address / bypass the limiter | NPM appends client-supplied `X-Forwarded-For` | Configure NPM to replace, not append |
| `/healthz` is `503` | DB not writable / corrupt | Check the data directory's ownership and `docker compose logs` |
| Stage says *Not authorized* after using the previous-games list | Host token was rotated by the select | Reopen the stage from the console |
| `Next round` refuses | The next round is still generating or failed | Wait for ready; on failure, reconfigure in the lobby |
| Rounds stuck after a restart | Interrupted job | Reconfigure the affected rounds (lobby only) |

---

## 8. Security checklist before you expose the app

- [ ] `TRIVIA_ADMIN_TOKEN` set to ≥16 chars of generated randomness (32
      recommended), stored only in `.env`/compose env, never in the repo.
- [ ] Management is confirmed locked with no token and with a 15-char token.
- [ ] `TRIVIA_PUBLIC_BASE_URL` is the exact public HTTPS origin.
- [ ] `TRIVIA_TRUSTED_PROXIES` lists NPM's IP and cloudflared's network as
      NPM sees it (for example `192.168.1.10,172.16.0.0/12`), and nothing that
      holds clients.
- [ ] No router port-forward; the only route is the `my-tunnel` tunnel →
      NPM:443 → container.
- [ ] A local LLM server such as Ollama (`:11434`) is not routed, tunneled, or
      published publicly.
- [ ] The data directory is `0700`, `trivia.db*` is `0600`, and backups are
      treated as credentials.
- [ ] `/healthz` returns `{"status":"ok"}` both locally and via the public
      origin.
- [ ] A contestant session (room code + team) is refused by `/api/games`,
      `/api/library/rounds`, `/api/game/configure` and `/api/game/delete`.
- [ ] The stage QR decodes to the public HTTPS join URL.
- [ ] `docker compose down` + `up` is a known-good rollback path and the data
      survives it.
