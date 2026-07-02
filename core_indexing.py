#!/usr/bin/env python3
"""
core_indexing.py — Google Indexing API client (the optional per-URL nudge).

Uses a service-account JSON key to mint an access token (JWT signed with the key,
scope `.../auth/indexing`) and publish URL_UPDATED notifications. This is the only
Google mechanism that nudges an individual URL for (re)crawl.

Optional: the whole tool works without it. It lights up only when a service-account
key is uploaded AND that service account has been added as an Owner of the property.
`google-auth` is imported lazily so the app boots even if it isn't installed.

Caveats (surfaced in the UI): officially for JobPosting/BroadcastEvent pages;
best-effort as a general crawl nudge, not a guarantee; ~200 URLs/day per project.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

PUBLISH_URI = "https://indexing.googleapis.com/v3/urlNotifications:publish"
METADATA_URI = "https://indexing.googleapis.com/v3/urlNotifications/metadata"
SCOPES = ["https://www.googleapis.com/auth/indexing"]


def _access_token_sync(key_json: dict[str, Any]) -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds = service_account.Credentials.from_service_account_info(key_json, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


async def get_access_token(key_json: dict[str, Any]) -> str:
    """Mint an Indexing-API access token from the service-account key (off-thread)."""
    return await asyncio.to_thread(_access_token_sync, key_json)


async def probe_permission(token: str, sample_url: str) -> str:
    """Cheaply test whether the service account may notify for this URL's property,
    without spending publish quota. Returns 'ok' | 'forbidden' | 'unknown'."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{METADATA_URI}?url={quote(sample_url, safe='')}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code in (200, 404):   # 404 = never notified but permitted
            return "ok"
        if r.status_code == 403:
            return "forbidden"
        return "unknown"
    except Exception:
        return "unknown"


async def publish(token: str, url: str, type_: str = "URL_UPDATED") -> dict[str, Any]:
    """Publish one URL notification. Returns {ok, status, message}."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                PUBLISH_URI,
                headers={"Authorization": f"Bearer {token}"},
                json={"url": url, "type": type_},
            )
        if r.status_code < 400:
            return {"ok": True, "status": r.status_code, "message": "Requested"}
        msg = r.text[:200]
        if r.status_code == 403:
            msg = "Not authorized — add the service account as an Owner of this property in Search Console."
        elif r.status_code == 429:
            msg = "Daily indexing quota reached (~200/day). Try again tomorrow."
        return {"ok": False, "status": r.status_code, "message": msg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "message": str(exc)}
