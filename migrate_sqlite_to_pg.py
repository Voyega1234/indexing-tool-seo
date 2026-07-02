#!/usr/bin/env python3
"""One-time migration: copy indexing_tool.db (SQLite) into Supabase Postgres (schema
indexing_tool). Run once after db.py has been switched to Postgres. Safe to re-run —
uses the same upsert/ON CONFLICT paths as the app, and clients are inserted with
their original id preserved via OVERRIDING SYSTEM VALUE so url/link client_id
references stay valid.
"""
from __future__ import annotations

import json
import sqlite3

import db as pg

SQLITE_PATH = "indexing_tool.db"


def main() -> None:
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row

    with pg._pool.connection() as pconn:
        # clients — preserve original ids (urls/links reference client_id by id)
        clients = conn.execute("SELECT * FROM clients").fetchall()
        for c in clients:
            pconn.execute(
                f"INSERT INTO {pg.SCHEMA}.clients "
                f"(id, name, sitemap_url, site_url, property_url, created_at, updated_at) "
                f"OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"ON CONFLICT (id) DO NOTHING",
                (c["id"], c["name"], c["sitemap_url"], c["site_url"], c["property_url"],
                 c["created_at"], c["updated_at"]),
            )
        if clients:
            max_id = max(c["id"] for c in clients)
            pconn.execute(
                f"SELECT setval(pg_get_serial_sequence('{pg.SCHEMA}.clients', 'id'), %s)",
                (max_id,),
            )
        print(f"clients: {len(clients)}")

        # urls
        urls = conn.execute("SELECT * FROM urls").fetchall()
        cols = ["client_id", "url"] + pg.URL_FIELDS
        rows = []
        for u in urls:
            d = dict(u)
            row = [d.get("client_id"), d.get("url")]
            for f in pg.URL_FIELDS:
                row.append(d.get(f))
            rows.append(tuple(row))
        placeholders = ", ".join("%s" for _ in cols)
        with pconn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {pg.SCHEMA}.urls ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (client_id, url) DO NOTHING",
                rows,
            )
        print(f"urls: {len(rows)}")

        # links
        links = conn.execute("SELECT client_id, from_url, to_url, anchor FROM links").fetchall()
        with pconn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {pg.SCHEMA}.links (client_id, from_url, to_url, anchor) VALUES (%s, %s, %s, %s)",
                [(r["client_id"], r["from_url"], r["to_url"], r["anchor"]) for r in links],
            )
        print(f"links: {len(links)}")

        # auth (single global token)
        auth = conn.execute("SELECT * FROM auth WHERE id = 1").fetchone()
        if auth:
            pg.save_token(json.loads(auth["token_json"]), auth["email"])
            print("auth: migrated")

        # service_account (single global key)
        sa = conn.execute("SELECT * FROM service_account WHERE id = 1").fetchone()
        if sa:
            pg.save_service_account(json.loads(sa["key_json"]))
            print("service_account: migrated")

        # request_log
        reqs = conn.execute("SELECT client_id, url, method, requested_at, ok, response FROM request_log").fetchall()
        with pconn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {pg.SCHEMA}.request_log (client_id, url, method, requested_at, ok, response) "
                f"VALUES (%s, %s, %s, %s, %s, %s)",
                [(r["client_id"], r["url"], r["method"], r["requested_at"], r["ok"], r["response"]) for r in reqs],
            )
        print(f"request_log: {len(reqs)}")

    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
