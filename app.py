"""Questionable Intel — self-hosted, AI-connected trivia night.

Entry point. Boots the DB and the HTTP server. Run::

    python3 app.py

Configuration via environment (all optional, see README.md / .env.example):

  TRIVIA_PORT       bind port (default 8130)
  TRIVIA_HOST       bind address (default 0.0.0.0 — LAN)
  TRIVIA_DATA_DIR   where data/trivia.db lives (default ./data)
  TRIVIA_LLM_BASE   OpenAI-compatible base URL (default https://api.deepseek.com)
  TRIVIA_LLM_MODEL  model id (default deepseek-flash)
  TRIVIA_LLM_API_KEY API key; required for DeepSeek/OpenAI, optional for local servers
"""
from __future__ import annotations

import os
import sys

import db
import server
import settings


def main() -> int:
    host = os.environ.get("TRIVIA_HOST", "0.0.0.0")
    port = int(os.environ.get("TRIVIA_PORT", "8130"))
    data_dir = os.environ.get("TRIVIA_DATA_DIR", os.path.join(os.getcwd(), "data"))
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "trivia.db")

    db.init_db(db_path)
    app = server.App(db_path)
    # Startup reconciliation: any round left pending/generating by a
    # previous process is orphaned; mark it failed, never phantom-ready.
    interrupted = app.engine.interrupt_inflight_rounds()
    httpd = server.make_server(app, host, port)
    resolved = settings.resolve(db_path)

    print(f"Questionable Intel listening on http://{host}:{port}", flush=True)
    print(f"  SQLite: {db_path}", flush=True)
    print(f"  LLM:    {resolved['model']} @ {resolved['base_url']}", flush=True)
    if interrupted:
        print(f"  marked {interrupted} orphaned round(s) failed from a prior run",
              flush=True)
    print("  Host console:  /console   Stage: /stage   Phone join: /join", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
