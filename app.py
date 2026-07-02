#!/usr/bin/env python3
r"""
app.py — Indexing Tool (standalone Cortex module).

Run it:
    cd "Cortex/Modules/Indexing Tool"
    python -m venv .venv && .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8010

Then open http://127.0.0.1:8010

A self-contained SEO indexing checkup: crawl a client's site + link graph, check
each URL's Google index status, diagnose *why* pages aren't indexed with fixes, and
(optionally) nudge the ready ones via the Indexing API. Supabase Postgres DB
(schema `indexing_tool`); shares the Cortex-root `.env` for DATABASE_URL/DIRECT_URL
and the Google OAuth client id/secret.
"""
from __future__ import annotations

from fastapi import FastAPI

import db
from router import api, page

app = FastAPI(title="Indexing Tool")

db.init_db()

app.include_router(api)
app.include_router(page)
