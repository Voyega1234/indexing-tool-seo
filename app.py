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
(optionally) nudge the ready ones via the Indexing API. Own SQLite DB; shares only
the Cortex-root `.env` for the Google OAuth client id/secret.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
from router import api, page

app = FastAPI(title="Indexing Tool")

db.init_db()

app.include_router(api)
app.include_router(page)

_here = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(_here, "static")), name="static")
