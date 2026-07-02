#!/usr/bin/env python3
"""
db.py — Postgres (Supabase) storage for the Indexing Tool, schema `indexing_tool`.

Tables
  clients          one row per client (name + sitemap url + saved GSC property)
  urls             one row per (client, url): crawl + GSC + diagnosis fields
  links            the internal link graph: (client, from_url, to_url)
  auth             the single connected agency Google account (OAuth token)  [global]
  service_account  the single uploaded Indexing-API service-account key       [global]
  request_log      audit of Indexing-API / sitemap-submit requests

Credentials (DATABASE_URL — pgbouncer transaction-pool connection) are read from the
same Cortex-root `.env` (walk-up) used for the Google OAuth client id/secret. Holds
tokens + the SA key — treat as secret.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_here = os.path.dirname(os.path.abspath(__file__))

# Load the Cortex-root .env by walking up (same pattern as core_gsc.py).
_dir = _here
for _ in range(8):
    _env = os.path.join(_dir, ".env")
    if os.path.exists(_env):
        load_dotenv(_env)
        break
    _parent = os.path.dirname(_dir)
    if _parent == _dir:
        break
    _dir = _parent

SCHEMA = "indexing_tool"

# prepare_threshold=None disables server-side prepared statements — required for
# Supabase's pooled connection (pgbouncer, transaction mode) which doesn't support them.
_pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    min_size=1,
    max_size=5,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The URL columns that carry crawl/GSC/diagnosis state. Kept in one place so the
# upsert stays in sync with the schema.
URL_FIELDS = [
    "title", "lastmod", "created_date", "modified_date", "http_status",
    "first_seen", "last_checked", "sitemap_source", "in_sitemap", "meta_robots",
    "index_status", "coverage_state", "verdict", "google_canonical",
    "user_canonical", "robots_state", "last_crawled", "referring_urls_json",
    "impressions", "clicks",
    "inbound_count", "outbound_count", "link_quality", "click_depth",
    "issue_type", "severity", "recommendation", "warnings_json", "nudge_eligible",
    "action", "rec_json", "page_type",
    "requested_at", "indexed_detected_at", "prev_index_status",
]


def init_db() -> None:
    with _pool.connection() as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.clients (
                id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name         TEXT NOT NULL,
                sitemap_url  TEXT,
                site_url     TEXT,
                property_url TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.urls (
                id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                client_id           INTEGER NOT NULL REFERENCES {SCHEMA}.clients(id) ON DELETE CASCADE,
                url                 TEXT NOT NULL,
                title               TEXT,
                lastmod             TEXT,
                created_date        TEXT,
                modified_date       TEXT,
                http_status         INTEGER,
                first_seen          TEXT,
                last_checked        TEXT,
                sitemap_source      TEXT,
                in_sitemap          INTEGER DEFAULT 0,
                meta_robots         TEXT,
                index_status        TEXT DEFAULT 'unknown',
                coverage_state      TEXT,
                verdict             TEXT,
                google_canonical    TEXT,
                user_canonical      TEXT,
                robots_state        TEXT,
                last_crawled        TEXT,
                referring_urls_json TEXT,
                impressions         INTEGER DEFAULT 0,
                clicks              INTEGER DEFAULT 0,
                inbound_count       INTEGER DEFAULT 0,
                outbound_count      INTEGER DEFAULT 0,
                link_quality        TEXT,
                click_depth         INTEGER,
                issue_type          TEXT,
                severity            TEXT,
                recommendation      TEXT,
                warnings_json       TEXT,
                nudge_eligible      INTEGER DEFAULT 0,
                action              TEXT,
                rec_json            TEXT,
                page_type           TEXT,
                requested_at        TEXT,
                indexed_detected_at TEXT,
                prev_index_status   TEXT,
                UNIQUE (client_id, url)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.links (
                client_id INTEGER NOT NULL REFERENCES {SCHEMA}.clients(id) ON DELETE CASCADE,
                from_url  TEXT NOT NULL,
                to_url    TEXT NOT NULL,
                anchor    TEXT
            )
            """
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_links_to   ON {SCHEMA}.links(client_id, to_url)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_links_from ON {SCHEMA}.links(client_id, from_url)")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.auth (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                token_json TEXT NOT NULL,
                email      TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.service_account (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                key_json     TEXT NOT NULL,
                client_email TEXT,
                project_id   TEXT,
                updated_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.request_log (
                id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                client_id    INTEGER,
                url          TEXT,
                method       TEXT,
                requested_at TEXT NOT NULL,
                ok           INTEGER,
                response     TEXT
            )
            """
        )


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #
def create_client(name: str, sitemap_url: str | None, site_url: str | None = None) -> int:
    ts = _now()
    with _pool.connection() as conn:
        cur = conn.execute(
            f"INSERT INTO {SCHEMA}.clients (name, sitemap_url, site_url, created_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name.strip(), (sitemap_url or "").strip() or None, (site_url or "").strip() or None, ts, ts),
        )
        return int(cur.fetchone()["id"])


def list_clients() -> list[dict[str, Any]]:
    with _pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {SCHEMA}.clients ORDER BY lower(name)").fetchall()
    return [dict(r) for r in rows]


def get_client(client_id: int) -> dict[str, Any] | None:
    with _pool.connection() as conn:
        r = conn.execute(f"SELECT * FROM {SCHEMA}.clients WHERE id = %s", (client_id,)).fetchone()
    return dict(r) if r else None


def update_client(client_id: int, **fields: Any) -> None:
    allowed = {"name", "sitemap_url", "site_url", "property_url"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k} = %s" for k in sets)
    with _pool.connection() as conn:
        conn.execute(f"UPDATE {SCHEMA}.clients SET {cols} WHERE id = %s", (*sets.values(), client_id))


def delete_client(client_id: int) -> None:
    with _pool.connection() as conn:
        conn.execute(f"DELETE FROM {SCHEMA}.clients WHERE id = %s", (client_id,))


# --------------------------------------------------------------------------- #
# urls
# --------------------------------------------------------------------------- #
def upsert_urls(client_id: int, rows: list[dict[str, Any]]) -> None:
    """Insert/update url rows. Only the provided fields are written; existing values
    for omitted fields are preserved (via COALESCE on excluded)."""
    if not rows:
        return
    cols = ["client_id", "url"] + URL_FIELDS
    placeholders = ", ".join("%s" for _ in cols)
    # On conflict: new value wins when present, EXCEPT these which keep the first value
    # ever recorded (created date must be stable; first_seen is a one-time stamp).
    _keep_first = {"created_date", "first_seen"}
    updates = ", ".join(
        (f"{f} = COALESCE({SCHEMA}.urls.{f}, excluded.{f})" if f in _keep_first
         else f"{f} = COALESCE(excluded.{f}, {SCHEMA}.urls.{f})")
        for f in URL_FIELDS
    )
    sql = (
        f"INSERT INTO {SCHEMA}.urls ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(client_id, url) DO UPDATE SET {updates}"
    )
    payload = []
    for r in rows:
        payload.append((client_id, r["url"], *[r.get(f) for f in URL_FIELDS]))
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)


def get_urls(client_id: int) -> list[dict[str, Any]]:
    with _pool.connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM {SCHEMA}.urls WHERE client_id = %s ORDER BY url", (client_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["referring_urls"] = json.loads(d.pop("referring_urls_json") or "[]")
        d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        d["rec"] = json.loads(d.pop("rec_json") or "null")
        out.append(d)
    return out


def get_url(client_id: int, url: str) -> dict[str, Any] | None:
    with _pool.connection() as conn:
        r = conn.execute(
            f"SELECT * FROM {SCHEMA}.urls WHERE client_id = %s AND url = %s", (client_id, url)
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["referring_urls"] = json.loads(d.pop("referring_urls_json") or "[]")
    d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
    d["rec"] = json.loads(d.pop("rec_json") or "null")
    return d


def set_url_fields(client_id: int, url: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    with _pool.connection() as conn:
        conn.execute(
            f"UPDATE {SCHEMA}.urls SET {cols} WHERE client_id = %s AND url = %s",
            (*fields.values(), client_id, url),
        )


# Columns that aren't TEXT — needed to cast VALUES literals correctly in bulk updates.
_INT_COLS = {
    "http_status", "impressions", "clicks", "inbound_count", "outbound_count",
    "click_depth", "nudge_eligible", "in_sitemap",
}


def bulk_set_url_fields(client_id: int, field_names: list[str], rows: list[dict[str, Any]]) -> None:
    """Update many url rows in one round trip. Each row in `rows` is {'url': ..., <field>: value}
    for the given field_names; a missing/None value keeps the existing column value (COALESCE),
    same semantics as set_url_fields. Chunked to keep each statement's param count sane."""
    if not rows:
        return
    CHUNK = 500
    casts = [_INT_COLS.__contains__(f) and "integer" or "text" for f in field_names]
    value_cols = ", ".join(f"col_{i}" for i in range(len(field_names)))
    set_clause = ", ".join(
        f"{f} = COALESCE(v.col_{i}, {SCHEMA}.urls.{f})" for i, f in enumerate(field_names)
    )
    with _pool.connection() as conn:
        for start in range(0, len(rows), CHUNK):
            batch = rows[start:start + CHUNK]
            values_row = "(" + ", ".join(["%s::text"] + [f"%s::{c}" for c in casts]) + ")"
            values_clause = ", ".join([values_row] * len(batch))
            sql = (
                f"UPDATE {SCHEMA}.urls SET {set_clause} "
                f"FROM (VALUES {values_clause}) AS v(url, {value_cols}) "
                f"WHERE {SCHEMA}.urls.client_id = %s AND {SCHEMA}.urls.url = v.url"
            )
            params: list[Any] = []
            for r in batch:
                params.append(r["url"])
                params.extend(r.get(f) for f in field_names)
            params.append(client_id)
            conn.execute(sql, params)


def clear_urls(client_id: int) -> None:
    with _pool.connection() as conn:
        conn.execute(f"DELETE FROM {SCHEMA}.urls WHERE client_id = %s", (client_id,))


# --------------------------------------------------------------------------- #
# links (the internal graph)
# --------------------------------------------------------------------------- #
def replace_links(client_id: int, pairs: list[tuple[str, str, str]]) -> None:
    """Replace the whole link graph for a client. pairs = (from_url, to_url, anchor)."""
    with _pool.connection() as conn:
        conn.execute(f"DELETE FROM {SCHEMA}.links WHERE client_id = %s", (client_id,))
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {SCHEMA}.links (client_id, from_url, to_url, anchor) VALUES (%s, %s, %s, %s)",
                [(client_id, f, t, a) for (f, t, a) in pairs],
            )


def inbound_links(client_id: int, url: str) -> list[dict[str, Any]]:
    with _pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT from_url AS url, anchor FROM {SCHEMA}.links "
            f"WHERE client_id = %s AND to_url = %s ORDER BY from_url",
            (client_id, url),
        ).fetchall()
    return [dict(r) for r in rows]


def outbound_links(client_id: int, url: str) -> list[dict[str, Any]]:
    with _pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT to_url AS url, anchor FROM {SCHEMA}.links "
            f"WHERE client_id = %s AND from_url = %s ORDER BY to_url",
            (client_id, url),
        ).fetchall()
    return [dict(r) for r in rows]


def inbound_map(client_id: int) -> dict[str, list[str]]:
    """{to_url: [from_url, ...]} for the whole client — used by the diagnosis engine."""
    with _pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT from_url, to_url FROM {SCHEMA}.links WHERE client_id = %s", (client_id,)
        ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["to_url"], []).append(r["from_url"])
    return out


def outbound_map(client_id: int) -> dict[str, list[str]]:
    with _pool.connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT from_url, to_url FROM {SCHEMA}.links WHERE client_id = %s", (client_id,)
        ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["from_url"], []).append(r["to_url"])
    return out


# --------------------------------------------------------------------------- #
# auth (single global Google account)
# --------------------------------------------------------------------------- #
def save_token(token: dict[str, Any], email: str | None) -> None:
    with _pool.connection() as conn:
        conn.execute(
            f"INSERT INTO {SCHEMA}.auth (id, token_json, email, updated_at) VALUES (1, %s, %s, %s) "
            f"ON CONFLICT(id) DO UPDATE SET token_json=excluded.token_json, "
            f"email=excluded.email, updated_at=excluded.updated_at",
            (json.dumps(token, ensure_ascii=False), email, _now()),
        )


def get_token() -> dict[str, Any] | None:
    with _pool.connection() as conn:
        r = conn.execute(f"SELECT token_json, email FROM {SCHEMA}.auth WHERE id = 1").fetchone()
    if not r:
        return None
    d = json.loads(r["token_json"])
    d["_email"] = r["email"]
    return d


def clear_token() -> None:
    with _pool.connection() as conn:
        conn.execute(f"DELETE FROM {SCHEMA}.auth WHERE id = 1")


# --------------------------------------------------------------------------- #
# service account (single global, optional)
# --------------------------------------------------------------------------- #
def save_service_account(key_json: dict[str, Any]) -> None:
    with _pool.connection() as conn:
        conn.execute(
            f"INSERT INTO {SCHEMA}.service_account (id, key_json, client_email, project_id, updated_at) "
            f"VALUES (1, %s, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET key_json=excluded.key_json, "
            f"client_email=excluded.client_email, project_id=excluded.project_id, updated_at=excluded.updated_at",
            (json.dumps(key_json, ensure_ascii=False), key_json.get("client_email"),
             key_json.get("project_id"), _now()),
        )


def get_service_account() -> dict[str, Any] | None:
    with _pool.connection() as conn:
        r = conn.execute(
            f"SELECT key_json, client_email, project_id FROM {SCHEMA}.service_account WHERE id = 1"
        ).fetchone()
    if not r:
        return None
    return {
        "key": json.loads(r["key_json"]),
        "client_email": r["client_email"],
        "project_id": r["project_id"],
    }


def clear_service_account() -> None:
    with _pool.connection() as conn:
        conn.execute(f"DELETE FROM {SCHEMA}.service_account WHERE id = 1")


# --------------------------------------------------------------------------- #
# request log
# --------------------------------------------------------------------------- #
def log_request(client_id: int | None, url: str, method: str, ok: bool, response: str) -> None:
    with _pool.connection() as conn:
        conn.execute(
            f"INSERT INTO {SCHEMA}.request_log (client_id, url, method, requested_at, ok, response) "
            f"VALUES (%s, %s, %s, %s, %s, %s)",
            (client_id, url, method, _now(), 1 if ok else 0, response[:2000]),
        )


init_db()
