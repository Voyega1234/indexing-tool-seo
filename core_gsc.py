#!/usr/bin/env python3
"""
core_gsc.py — Google Search Console integration (reads).

OAuth 2.0 (auth-code) login done directly over httpx — no Google client libraries.
Provides:
  * OAuth: build auth url / exchange code / refresh token / userinfo / list sites
  * URL Inspection API  — per-URL index status + reason + canonical + referring urls
  * Search Analytics API (dimension=page) — the free "already indexed + impressions" signal
  * Sitemap submit — the sanctioned passive nudge

Credentials (Google OAuth client id/secret) are read from the shared Cortex-root
`.env` (walk-up), same as the Keyword Research Tools module.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlencode

from dotenv import load_dotenv

# Load the Cortex-root .env by walking up (same pattern as the KRT modules).
_dir = os.path.dirname(os.path.abspath(__file__))
for _ in range(8):
    _env = os.path.join(_dir, ".env")
    if os.path.exists(_env):
        load_dotenv(_env)
        break
    _parent = os.path.dirname(_dir)
    if _parent == _dir:
        break
    _dir = _parent

# Full webmasters scope (needed for sitemap submit; inspection/analytics also fine) + profile.
SCOPES = "https://www.googleapis.com/auth/webmasters openid email"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_URI = "https://www.googleapis.com/oauth2/v2/userinfo"
WM_BASE = "https://www.googleapis.com/webmasters/v3"
INSPECT_URI = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

SA_PAGE_SIZE = 25000


# --------------------------------------------------------------------------- #
# OAuth client config
# --------------------------------------------------------------------------- #
def client_creds() -> tuple[str | None, str | None]:
    return os.getenv("GOOGLE_OAUTH_CLIENT_ID"), os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")


def oauth_configured() -> bool:
    cid, cs = client_creds()
    return bool(cid and cs)


def build_auth_url(redirect_uri: str, state: str) -> str:
    cid, _ = client_creds()
    params = {
        "client_id": cid or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URI}?{urlencode(params)}"


# --------------------------------------------------------------------------- #
# OAuth token endpoints
# --------------------------------------------------------------------------- #
async def _post_token(data: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URI, data=data)
        if r.status_code >= 400:
            raise RuntimeError(f"Google token error ({r.status_code}): {r.text[:300]}")
        return r.json()


async def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    cid, cs = client_creds()
    return await _post_token({
        "code": code, "client_id": cid, "client_secret": cs,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    })


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    cid, cs = client_creds()
    return await _post_token({
        "refresh_token": refresh_token, "client_id": cid, "client_secret": cs,
        "grant_type": "refresh_token",
    })


async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"})
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


# --------------------------------------------------------------------------- #
# Search Console reads
# --------------------------------------------------------------------------- #
async def list_sites(access_token: str) -> list[dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{WM_BASE}/sites", headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 400:
            raise RuntimeError(f"GSC sites error ({r.status_code}): {r.text[:300]}")
        return r.json().get("siteEntry", [])


async def inspect_url(access_token: str, site_url: str, page_url: str, client: Any = None) -> dict[str, Any]:
    """URL Inspection API for one page. Returns the flattened indexStatusResult.

    Pass a shared ``httpx.AsyncClient`` to reuse the connection (keep-alive) across a
    concurrent batch — much faster than opening a new TLS connection per URL.
    """
    import httpx

    body = {"inspectionUrl": page_url, "siteUrl": site_url, "languageCode": "en-US"}
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=45)
    try:
        r = await client.post(INSPECT_URI, headers={"Authorization": f"Bearer {access_token}"}, json=body)
        if r.status_code == 429:
            raise RuntimeError("QUOTA")   # rate-limited or daily cap; caller backs off / skips
        if r.status_code >= 400:
            raise RuntimeError(f"inspect {r.status_code}: {r.text[:200]}")
        idx = (r.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
        return {
            "verdict": idx.get("verdict"),
            "coverage_state": idx.get("coverageState"),
            "robots_state": idx.get("robotsTxtState"),
            "indexing_state": idx.get("indexingState"),
            "last_crawled": idx.get("lastCrawlTime"),
            "page_fetch_state": idx.get("pageFetchState"),
            "google_canonical": idx.get("googleCanonical"),
            "user_canonical": idx.get("userCanonical"),
            "referring_urls": idx.get("referringUrls") or [],
            "sitemap": idx.get("sitemap") or [],
        }
    finally:
        if own:
            await client.aclose()


async def search_analytics_pages(
    access_token: str, site_url: str, start_date: str, end_date: str,
) -> dict[str, dict[str, Any]]:
    """{page_url: {impressions, clicks, position}} across the window — the free
    'this page is indexed and getting impressions' signal."""
    import httpx

    url = f"{WM_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
    out: dict[str, dict[str, Any]] = {}
    start_row = 0
    async with httpx.AsyncClient(timeout=90) as c:
        while True:
            body = {
                "startDate": start_date, "endDate": end_date,
                "dimensions": ["page"], "rowLimit": SA_PAGE_SIZE,
                "startRow": start_row, "dataState": "all",
            }
            r = await c.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"searchAnalytics {r.status_code}: {r.text[:200]}")
            rows = r.json().get("rows", [])
            if not rows:
                break
            for row in rows:
                page = (row.get("keys") or [""])[0]
                out[page] = {
                    "impressions": int(row.get("impressions", 0) or 0),
                    "clicks": int(row.get("clicks", 0) or 0),
                    "position": round(float(row.get("position", 0) or 0), 1),
                }
            if len(rows) < SA_PAGE_SIZE:
                break
            start_row += SA_PAGE_SIZE
    return out


async def submit_sitemap(access_token: str, site_url: str, sitemap_url: str) -> bool:
    import httpx

    url = f"{WM_BASE}/sites/{quote(site_url, safe='')}/sitemaps/{quote(sitemap_url, safe='')}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(url, headers={"Authorization": f"Bearer {access_token}"})
        return r.status_code < 400
