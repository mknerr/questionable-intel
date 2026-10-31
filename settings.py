"""LLM settings: resolution, validation, persistence (stdlib only).

Precedence per field is **DB row > env var > default**. Rows store only
overrides; writing an empty string deletes the row (clears the override).
Every call opens its own short-lived ``db.connect`` — no import-time env read,
no caching, so a console save is visible to the next request without a restart.

The ``base_url`` target check delegates to :mod:`netutil`: the host is
rejected when it *is*, or *resolves to*, a loopback / link-local / metadata /
unspecified / multicast address (IPv4 and IPv6). RFC1918 stays allowed.
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse

import db
import gen
import netutil

PRESETS = [
    {"id": "deepseek", "label": "DeepSeek",
     "base_url": "https://api.deepseek.com", "model_hint": "deepseek-flash"},
    {"id": "ollama", "label": "Local Ollama",
     "base_url": "http://host.docker.internal:11434/v1", "model_hint": "qwen3.5:9b"},
    {"id": "openai", "label": "OpenAI",
     "base_url": "https://api.openai.com/v1", "model_hint": "gpt-4o-mini"},
    {"id": "custom", "label": "Custom OpenAI-compatible",
     "base_url": "", "model_hint": ""},
]

_PROVIDER_IDS = {p["id"] for p in PRESETS}
_DEFAULT_PROVIDER = "deepseek"

# Public field name -> DB row key.
_FIELD_TO_KEY = {
    "provider": "llm.provider",
    "base_url": "llm.base_url",
    "model": "llm.model",
    "api_key": "llm.api_key",
}

_KNOWN_FIELDS = {"provider", "base_url", "model", "api_key"}


class MissingKeyError(RuntimeError):
    """A hosted provider (DeepSeek, OpenAI) is selected but no API key is set.
    ``str(e)`` is the complete user-facing message."""


class SettingsError(Exception):
    """Validation failure carrying a list of human-readable errors."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(errors))


def normalize_base_url(url: str) -> str:
    """Normalize a base URL for byte-comparison (a stored key is only ever
    sent to a draft URL identical to the resolved one): strip whitespace and
    trailing slashes."""
    return (url or "").strip().rstrip("/")


def _preset_for_base(base_url: str) -> dict | None:
    """The preset whose (normalized) base URL equals *base_url*, else None.
    The ``custom`` preset has no URL and never matches."""
    want = normalize_base_url(base_url).lower()
    if not want:
        return None
    for p in PRESETS:
        if p["base_url"] and normalize_base_url(p["base_url"]).lower() == want:
            return p
    return None


def _read_overrides(db_path: str) -> dict[str, str]:
    conn = db.connect(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT key, value FROM app_settings").fetchall()
        except sqlite3.OperationalError:
            # Old DB created before the app_settings table existed: treat as
            # no overrides rather than crashing.
            rows = []
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def resolve(db_path: str) -> dict:
    """Resolve effective settings: DB override > env var > default, per field."""
    rows = _read_overrides(db_path)
    source: dict[str, str] = {}

    base_url = rows.get("llm.base_url")
    if base_url:
        source["base_url"] = "db"
    else:
        env_base = os.environ.get("TRIVIA_LLM_BASE")
        if env_base:
            base_url = env_base
            source["base_url"] = "env"
        else:
            base_url = gen.DEFAULT_BASE
            source["base_url"] = "default"

    # The preset (if any) that the effective base URL belongs to.
    matched = _preset_for_base(base_url)

    provider = rows.get("llm.provider")
    if provider:
        source["provider"] = "db"
    else:
        # Not chosen explicitly: infer it from where requests really go, so an
        # env-only TRIVIA_LLM_BASE for a self-hosted server reports "custom"
        # rather than the default provider.
        provider = matched["id"] if matched else "custom"
        source["provider"] = "default"

    model = rows.get("llm.model")
    if model:
        source["model"] = "db"
    else:
        env_model = os.environ.get("TRIVIA_LLM_MODEL")
        if env_model:
            model = env_model
            source["model"] = "env"
        elif source["base_url"] != "default" and matched and matched["model_hint"]:
            # A non-default endpoint must not be sent the default provider's
            # model id; use that endpoint's own preset model instead.
            model = matched["model_hint"]
            source["model"] = "default"
        else:
            model = gen.DEFAULT_MODEL
            source["model"] = "default"

    api_key = rows.get("llm.api_key")
    if api_key:
        source["api_key"] = "db"
    else:
        env_key = os.environ.get("TRIVIA_LLM_API_KEY")
        if env_key:
            api_key = env_key
            source["api_key"] = "env"
        else:
            api_key = None
            source["api_key"] = "none"

    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "source": source,
    }


def missing_key_error(resolved: dict) -> str | None:
    """A human-readable message when the effective endpoint is a hosted API
    that needs a key (DeepSeek, OpenAI) and none is set; otherwise None.
    Matched by parsed hostname. Never includes or touches any key value."""
    host = gen._host_of(resolved.get("base_url") or "")
    names = {"api.deepseek.com": "DeepSeek", "api.openai.com": "OpenAI"}
    name = names.get(host)
    if not name or (resolved.get("api_key") or "").strip():
        return None
    where = " Get a key at platform.deepseek.com." if name == "DeepSeek" else ""
    return (f"AI question generation needs a {name} API key and none is set.{where} "
            "Add it under AI settings in the console, or set TRIVIA_LLM_API_KEY. "
            "You can still paste in or import questions without AI.")


def public_view(resolved: dict) -> dict:
    """The API-safe view of a resolution: no key value, prefix, length or hash —
    only ``api_key_set``."""
    return {
        "provider": resolved["provider"],
        "base_url": resolved["base_url"],
        "model": resolved["model"],
        "api_key_set": bool(resolved.get("api_key")),
        "source": resolved["source"],
        "defaults": {
            "provider": _DEFAULT_PROVIDER,
            "base_url": gen.DEFAULT_BASE,
            "model": gen.DEFAULT_MODEL,
        },
        "presets": PRESETS,
    }


def _effective_key(updates: dict, resolved: dict | None) -> str | None:
    """The API key that would be in effect after applying *updates* — used by the
    cleartext rule. An empty update value clears the override."""
    if "api_key" in updates:
        return (updates.get("api_key") or "").strip() or None
    return (resolved or {}).get("api_key") or None


def validate(updates: dict, resolved: dict | None = None) -> list[str]:
    """Validate a set of updates; return a list of human-readable errors ([] = ok).

    Errors carry field + constraint only — never the submitted value.
    """
    errors: list[str] = []
    resolved = resolved or {}

    for key in updates:
        if key not in _KNOWN_FIELDS:
            errors.append(f"unknown key: {key}")

    if "provider" in updates and updates["provider"] not in _PROVIDER_IDS:
        errors.append("provider: must be one of "
                      + ", ".join(p["id"] for p in PRESETS))

    if "model" in updates:
        m = updates["model"]
        if not isinstance(m, str) or not (1 <= len(m.strip()) <= 200):
            errors.append("model: must be 1..200 characters")
        elif "\r" in m or "\n" in m or any(ord(c) < 32 for c in m):
            errors.append("model: must not contain control characters")

    if "api_key" in updates:
        k = updates["api_key"]
        if k is not None:
            if not isinstance(k, str) or len(k) > 500:
                errors.append("api_key: longer than 500 characters")
            elif "\r" in k or "\n" in k or any(ord(c) < 32 for c in k):
                errors.append("api_key: must not contain control characters")

    if "base_url" in updates:
        bu = updates["base_url"]
        bu_errors: list[str] = []
        if not isinstance(bu, str) or not bu.strip():
            bu_errors.append("required")
        else:
            bu = bu.strip()
            if len(bu) > 500:
                bu_errors.append("longer than 500 characters")
            if any(ord(c) < 32 for c in bu) or any(c.isspace() for c in bu):
                bu_errors.append("must not contain whitespace or control characters")
            parts = urllib.parse.urlsplit(bu)
            if parts.scheme not in ("http", "https"):
                bu_errors.append("scheme must be http or https")
            if not parts.netloc:
                bu_errors.append("missing host")
            if "@" in parts.netloc:
                bu_errors.append("must not contain userinfo")
            if parts.query:
                bu_errors.append("must not contain a query string")
            if parts.fragment:
                bu_errors.append("must not contain a fragment")
            # Target check + cleartext rule, only when syntactically clean
            # (avoids a DNS probe on an obviously-malformed URL).
            if not bu_errors and parts.hostname:
                try:
                    addrs = netutil.resolve_host(parts.hostname)
                except netutil.NetError as e:
                    bu_errors.append(e.reason)
                else:
                    key = _effective_key(updates, resolved)
                    if (key and parts.scheme == "http"
                            and any(not netutil.rfc1918(a) for a in addrs)):
                        bu_errors.append(
                            "http not allowed to a public host when an API key is set")
            elif not bu_errors and not parts.hostname:
                bu_errors.append("missing host")
        for e in bu_errors:
            errors.append(f"base_url: {e}")

    return errors


def save(db_path: str, updates: dict) -> dict:
    """Validate then persist *updates* (empty value deletes the override); return
    the fresh :func:`resolve`. Raises :class:`SettingsError` on validation failure."""
    resolved_before = resolve(db_path)
    errors = validate(updates, resolved=resolved_before)
    if errors:
        raise SettingsError(errors)

    normalized = dict(updates)
    if normalized.get("base_url"):
        normalized["base_url"] = normalize_base_url(normalized["base_url"])

    conn = db.connect(db_path)
    try:
        for field, value in normalized.items():
            dbkey = _FIELD_TO_KEY[field]
            if value == "" or value is None:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (dbkey,))
            else:
                conn.execute(
                    "INSERT INTO app_settings(key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (dbkey, value, time.time()),
                )
        conn.commit()
    finally:
        conn.close()
    return resolve(db_path)
