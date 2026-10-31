# Questionable Intel — Python 3.14 slim (pinned by digest), stdlib-only runtime.
FROM python:3.14-slim@sha256:82abc9f53600280b0757126f83c5a7d47088592d2acb6b4c0b9e97f76865508c

WORKDIR /app

# Application code (pure stdlib — no pip install step, no build step).
# Glob, not a file list: a hand-maintained list silently drops any new module,
# and the app then fails at import time.
COPY *.py ./
COPY static/ ./static/
COPY LICENSE README.md THIRD_PARTY_LICENSES.md ./

# Persistent, self-contained data directory (the single SQLite file lives here).
# Run as a non-root user and give it ownership of /data so the SQLite file is
# writable — a container that can't write its DB is worse than one running root.
RUN useradd --uid 10001 --create-home trivia \
    && mkdir -p /data \
    && chown trivia:trivia /data
ENV TRIVIA_DATA_DIR=/data \
    TRIVIA_HOST=0.0.0.0 \
    TRIVIA_PORT=8130

EXPOSE 8130
VOLUME ["/data"]

# Non-root runtime user: the app must never run as root.
USER trivia

# Liveness + DB reachability probe: poll GET /healthz (200 + SELECT 1).
# No curl in slim, so use stdlib urllib against the app's own loopback port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('TRIVIA_PORT','8130')+'/healthz',timeout=3)" || exit 1

CMD ["python3", "app.py"]
