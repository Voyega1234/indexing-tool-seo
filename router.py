#!/usr/bin/env python3
"""
router.py — Indexing Tool API.

Endpoints under /api/*: clients, crawl (Step 1), GSC connect + check (Step 2),
service account + request-indexing (Step 3), plus refresh and detail lookups.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import core_crawl as crawl
import core_gsc as gsc
import core_indexing as indexing
import db
import diagnose as dx
import jobs

_here = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(_here, "static", "index.html")

api = APIRouter(prefix="/api", tags=["indexing"])
page = APIRouter()

_oauth_states: dict[str, str] = {}

# URL Inspection API daily ceiling per property (leave headroom under the 2k quota).
INSPECT_CAP = 1800
SA_LOOKBACK_DAYS = 90
# Concurrent inspections. Google allows ~600/min (~10/s); 8 stays comfortably under it.
INSPECT_CONCURRENCY = 8
# Page types we never bother inspecting (clutter that shouldn't be indexed anyway) —
# keeps the run small and fast, and off the daily quota.
SKIP_INSPECT_TYPES = {"System", "Parameter", "Feed", "Author", "Tag", "Pagination",
                      "Category", "Media", "Broken", "Utility", "External", "Other"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _home_of(client: dict) -> str | None:
    for key in ("site_url", "sitemap_url", "property_url"):
        base = crawl.normalize_base((client.get(key) or "").replace("sc-domain:", "https://"))
        if base:
            return crawl.normalize_url(base + "/")
    return None


def _crawl_seed(client: dict) -> str | None:
    return (client.get("sitemap_url") or client.get("site_url")
            or (client.get("property_url") or "").replace("sc-domain:", "https://") or None)


def run_diagnosis(client_id: int) -> dict:
    """Load a client's URLs + link graph, run the engine, persist results, return summary."""
    client = db.get_client(client_id)
    rows = db.get_urls(client_id)
    for r in rows:  # meta_robots isn't a stored column; diagnosis reads it from warnings-free input
        r.setdefault("meta_robots", "")
    inbound = db.inbound_map(client_id)
    outbound = db.outbound_map(client_id)
    result = dx.diagnose(rows, inbound, outbound, _home_of(client) if client else None)
    prev_status = {r["url"]: r.get("index_status") for r in rows}
    field_names = [
        "index_status", "page_type", "link_quality", "click_depth", "inbound_count",
        "outbound_count", "issue_type", "severity", "recommendation", "warnings_json",
        "action", "rec_json", "nudge_eligible", "indexed_detected_at",
    ]
    updates = []
    for url, d in result.items():
        prev = prev_status.get(url)
        row = {
            "url": url,
            "index_status": d["index_status"],
            "page_type": d["page_type"],
            "link_quality": d["link_quality"],
            "click_depth": d["click_depth"],
            "inbound_count": d["inbound_count"],
            "outbound_count": d["outbound_count"],
            "issue_type": d["issue_type"],
            "severity": d["severity"],
            "recommendation": d["recommendation"],
            "warnings_json": __import__("json").dumps(d["warnings"], ensure_ascii=False),
            "action": d["action"],
            "rec_json": __import__("json").dumps(d["rec"], ensure_ascii=False),
            "nudge_eligible": 1 if d["nudge_eligible"] else 0,
        }
        # newly-indexed flip detection
        if prev and prev != "indexed" and d["index_status"] == "indexed":
            row["indexed_detected_at"] = db._now()
        updates.append(row)
    db.bulk_set_url_fields(client_id, field_names, updates)
    return dx.summarize(result)


async def _valid_access_token() -> str:
    tok = db.get_token()
    if not tok or not tok.get("refresh_token"):
        raise HTTPException(status_code=401, detail="Not connected to Google Search Console.")
    if tok.get("_expiry", 0) - time.time() < 60:
        new = await gsc.refresh_access_token(tok["refresh_token"])
        tok["access_token"] = new.get("access_token")
        tok["_expiry"] = time.time() + int(new.get("expires_in", 3600))
        if new.get("refresh_token"):
            tok["refresh_token"] = new["refresh_token"]
        db.save_token({k: v for k, v in tok.items() if k != "_email"}, tok.get("_email"))
    return tok["access_token"]


def _apply_impressions(cid: int, sa_norm: dict) -> None:
    updates = []
    for r in db.get_urls(cid):
        m = sa_norm.get(r["url"])
        if m:
            updates.append({"url": r["url"], "impressions": m["impressions"], "clicks": m["clicks"]})
    db.bulk_set_url_fields(cid, ["impressions", "clicks"], updates)


async def _inspect_gaps(cid: int, prop: str, progress) -> int:
    """Concurrently URL-Inspect the pages that still need it — not already indexed
    (via impressions), not clutter page-types. Reuses ONE HTTP connection + ONE token
    across the whole batch (fast), writes results, returns the count inspected."""
    import httpx

    token = await _valid_access_token()
    rows = await asyncio.to_thread(db.get_urls, cid)
    gaps = [r for r in rows
            if not (r.get("impressions") or 0)
            and r.get("page_type") not in SKIP_INSPECT_TYPES
            and r.get("index_status") != "indexed"]
    gaps = gaps[:INSPECT_CAP]
    if not gaps:
        return 0
    total = len(gaps)
    sem = asyncio.Semaphore(INSPECT_CONCURRENCY)
    counter = {"n": 0}

    async with httpx.AsyncClient(timeout=45) as client:
        async def one(r):
            async with sem:
                for _ in range(3):
                    try:
                        ins = await gsc.inspect_url(token, prop, r["url"], client=client)
                        counter["n"] += 1
                        if counter["n"] % 10 == 0:
                            progress(phase="inspect", done=counter["n"], total=total,
                                     message=f"Inspected {counter['n']}/{total} URLs with Google…")
                        return (r["url"], ins)
                    except RuntimeError as e:
                        if "QUOTA" in str(e):   # rate-limited — brief back-off, then retry
                            await asyncio.sleep(3)
                            continue
                        return None
                return None
        results = await asyncio.gather(*[one(r) for r in gaps])

    def _write():                  # one bulk round trip, off the event loop
        field_names = [
            "coverage_state", "verdict", "google_canonical", "user_canonical",
            "robots_state", "last_crawled", "referring_urls_json", "last_checked",
        ]
        now = db._now()
        updates = []
        for res in results:
            if not res:
                continue
            url, ins = res
            updates.append({
                "url": url,
                "coverage_state": ins.get("coverage_state"), "verdict": ins.get("verdict"),
                "google_canonical": ins.get("google_canonical"), "user_canonical": ins.get("user_canonical"),
                "robots_state": ins.get("robots_state"), "last_crawled": ins.get("last_crawled"),
                "referring_urls_json": json.dumps(ins.get("referring_urls", []), ensure_ascii=False),
                "last_checked": now,
            })
        db.bulk_set_url_fields(cid, field_names, updates)
        return len(updates)
    return await asyncio.to_thread(_write)


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #
@page.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


# --------------------------------------------------------------------------- #
# clients
# --------------------------------------------------------------------------- #
class ClientIn(BaseModel):
    name: str
    sitemap_url: str | None = None
    site_url: str | None = None


@api.get("/clients")
async def clients_list() -> list[dict[str, Any]]:
    return db.list_clients()


@api.post("/clients")
async def clients_create(body: ClientIn) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(400, "Client name is required.")
    cid = db.create_client(body.name, body.sitemap_url, body.site_url)
    return db.get_client(cid)


@api.get("/clients/{cid}")
async def clients_get(cid: int) -> dict[str, Any]:
    c = db.get_client(cid)
    if not c:
        raise HTTPException(404, "Client not found.")
    return c


class ClientPatch(BaseModel):
    name: str | None = None
    sitemap_url: str | None = None
    site_url: str | None = None
    property_url: str | None = None


@api.patch("/clients/{cid}")
async def clients_update(cid: int, body: ClientPatch) -> dict[str, Any]:
    db.update_client(cid, **{k: v for k, v in body.model_dump().items() if v is not None})
    return db.get_client(cid)


@api.delete("/clients/{cid}")
async def clients_delete(cid: int) -> dict[str, Any]:
    db.delete_client(cid)
    return {"ok": True}


@api.post("/clients/{cid}/diagnose")
async def clients_diagnose(cid: int) -> dict[str, Any]:
    """Re-run the diagnosis engine on the stored data (no crawl/network). Fast."""
    if not db.get_client(cid):
        raise HTTPException(404, "Client not found.")
    return {"summary": await asyncio.to_thread(run_diagnosis, cid)}


@api.get("/clients/{cid}/urls")
async def clients_urls(cid: int) -> dict[str, Any]:
    rows = db.get_urls(cid)
    return {"urls": rows, "summary": _summary_from_rows(rows)}


def _summary_from_rows(rows: list[dict]) -> dict:
    diagnosed = {r["url"]: {
        "issue_type": r.get("issue_type") or "unchecked",
        "severity": r.get("severity") or "info",
        "index_status": r.get("index_status") or "unknown",
    } for r in rows}
    return dx.summarize(diagnosed)


@api.get("/clients/{cid}/url")
async def client_url_detail(cid: int, url: str) -> dict[str, Any]:
    row = db.get_url(cid, url)
    if not row:
        raise HTTPException(404, "URL not found.")
    row["inbound"] = db.inbound_links(cid, url)
    row["outbound"] = db.outbound_links(cid, url)
    return row


# --------------------------------------------------------------------------- #
# Step 1 — crawl
# --------------------------------------------------------------------------- #
class CrawlIn(BaseModel):
    url: str | None = None
    max_pages: int | None = None


@api.post("/clients/{cid}/crawl")
async def client_crawl(cid: int, body: CrawlIn) -> dict[str, Any]:
    client = db.get_client(cid)
    if not client:
        raise HTTPException(404, "Client not found.")
    seed = (body.url or "").strip() or _crawl_seed(client)
    if not seed:
        raise HTTPException(400, "No website/sitemap URL for this client.")
    if body.url and not client.get("sitemap_url"):
        db.update_client(cid, sitemap_url=body.url)

    async def work(progress):
        res = await crawl.crawl_site(seed, body.max_pages or crawl.DEFAULT_MAX_PAGES, progress)
        progress(phase="save", message="Saving pages + link graph…")
        now = db._now()
        rows = [{
            "url": p["url"], "title": p.get("title"), "lastmod": p.get("lastmod"),
            "created_date": p.get("published_time") or p.get("lastmod") or None,
            "modified_date": p.get("modified_time") or p.get("lastmod") or None,
            "http_status": p.get("http_status"), "in_sitemap": 1 if p.get("in_sitemap") else 0,
            "sitemap_source": p.get("sitemap_source"), "meta_robots": p.get("meta_robots"),
            "first_seen": now, "last_checked": now,
        } for p in res["pages"]]
        # fill omitted URL_FIELDS with None so upsert has all keys
        await asyncio.to_thread(db.upsert_urls, cid, rows)
        await asyncio.to_thread(db.replace_links, cid, [(f, t, a) for (f, t, a) in res["edges"]])
        summary = await asyncio.to_thread(run_diagnosis, cid)
        return {"count": res["count"], "edges": res["edge_count"],
                "sitemap_count": res["sitemap_count"], "note": res["note"], "summary": summary}

    job = jobs.start_job("crawl", cid, work)
    return {"job_id": job.id}


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def _redirect_uri(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/api/oauth/callback"


def _close_html(msg: str) -> str:
    safe = msg.replace("&", "&amp;").replace("<", "&lt;")
    return ("<!doctype html><meta charset='utf-8'><body style='font:15px sans-serif;padding:40px'>"
            f"<p>{safe}</p><script>try{{if(window.opener)window.opener.postMessage('gsc-auth-changed','*')}}catch(e){{}}"
            "setTimeout(()=>window.close(),1200)</script></body>")


@api.get("/auth-status")
async def auth_status(request: Request) -> dict[str, Any]:
    tok = db.get_token()
    return {"configured": gsc.oauth_configured(),
            "connected": bool(tok and tok.get("refresh_token")),
            "email": tok.get("_email") if tok else None,
            "redirect_uri": _redirect_uri(request)}


@api.get("/oauth/start")
async def oauth_start(request: Request) -> dict[str, Any]:
    if not gsc.oauth_configured():
        raise HTTPException(400, "Google OAuth not configured (GOOGLE_OAUTH_CLIENT_ID/SECRET in the Cortex .env).")
    ru = _redirect_uri(request)
    state = uuid.uuid4().hex
    _oauth_states[state] = ru
    return {"auth_url": gsc.build_auth_url(ru, state)}


@api.get("/oauth/callback")
async def oauth_callback(request: Request, code: str | None = None,
                         state: str | None = None, error: str | None = None) -> HTMLResponse:
    if error or not code or not state or state not in _oauth_states:
        return HTMLResponse(_close_html(f"Authorization failed: {error or 'invalid request'}."))
    ru = _oauth_states.pop(state)
    try:
        token = await gsc.exchange_code(code, ru)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(_close_html(f"Token exchange failed: {exc}"))
    token["_expiry"] = time.time() + int(token.get("expires_in", 3600))
    info = await gsc.fetch_userinfo(token.get("access_token", ""))
    db.save_token(token, info.get("email"))
    return HTMLResponse(_close_html(f"Connected{(' as ' + info['email']) if info.get('email') else ''}."))


@api.post("/disconnect")
async def disconnect() -> dict[str, Any]:
    db.clear_token()
    return {"ok": True}


@api.get("/properties")
async def properties() -> dict[str, Any]:
    token = await _valid_access_token()
    sites = await gsc.list_sites(token)
    props = sorted(({"site_url": s["siteUrl"], "permission": s.get("permissionLevel")}
                    for s in sites if s.get("siteUrl")), key=lambda p: p["site_url"])
    return {"properties": props}


# --------------------------------------------------------------------------- #
# Step 2 — GSC check
# --------------------------------------------------------------------------- #
@api.post("/clients/{cid}/check")
async def client_check(cid: int) -> dict[str, Any]:
    client = db.get_client(cid)
    if not client:
        raise HTTPException(404, "Client not found.")
    prop = client.get("property_url")
    if not prop:
        raise HTTPException(400, "Pick and save a GSC property for this client first.")
    await _valid_access_token()

    async def work(progress):
        token = await _valid_access_token()
        end = date.today()
        start = end - timedelta(days=SA_LOOKBACK_DAYS)
        progress(phase="analytics", message="Pulling Search Analytics (indexed + impressions)…")
        sa = await gsc.search_analytics_pages(token, prop, start.isoformat(), end.isoformat())
        sa_norm = {crawl.normalize_url(k) or k: v for k, v in sa.items()}
        await asyncio.to_thread(_apply_impressions, cid, sa_norm)
        inspected = await _inspect_gaps(cid, prop, progress)
        summary = await asyncio.to_thread(run_diagnosis, cid)
        return {"inspected": inspected, "analytics_pages": len(sa), "summary": summary}

    job = jobs.start_job("check", cid, work)
    return {"job_id": job.id}


# --------------------------------------------------------------------------- #
# Refresh — crawl + check + diagnose
# --------------------------------------------------------------------------- #
@api.post("/clients/{cid}/refresh")
async def client_refresh(cid: int) -> dict[str, Any]:
    client = db.get_client(cid)
    if not client:
        raise HTTPException(404, "Client not found.")
    seed = _crawl_seed(client)
    prop = client.get("property_url")

    async def work(progress):
        if seed:
            res = await crawl.crawl_site(seed, crawl.DEFAULT_MAX_PAGES, progress)
            now = db._now()
            await asyncio.to_thread(db.upsert_urls, cid, [{
                "url": p["url"], "title": p.get("title"), "lastmod": p.get("lastmod"),
                "created_date": p.get("published_time") or p.get("lastmod") or None,
                "modified_date": p.get("modified_time") or p.get("lastmod") or None,
                "http_status": p.get("http_status"), "in_sitemap": 1 if p.get("in_sitemap") else 0,
                "sitemap_source": p.get("sitemap_source"), "meta_robots": p.get("meta_robots"),
                "last_checked": now,
            } for p in res["pages"]])
            await asyncio.to_thread(db.replace_links, cid, [(f, t, a) for (f, t, a) in res["edges"]])
        if prop:
            token = await _valid_access_token()
            end = date.today(); start = end - timedelta(days=SA_LOOKBACK_DAYS)
            progress(phase="analytics", message="Refreshing Search Analytics…")
            sa = await gsc.search_analytics_pages(token, prop, start.isoformat(), end.isoformat())
            sa_norm = {crawl.normalize_url(k) or k: v for k, v in sa.items()}
            await asyncio.to_thread(_apply_impressions, cid, sa_norm)
            await _inspect_gaps(cid, prop, progress)
        summary = await asyncio.to_thread(run_diagnosis, cid)
        return {"summary": summary}

    job = jobs.start_job("refresh", cid, work)
    return {"job_id": job.id}


# --------------------------------------------------------------------------- #
# Service account (global, optional) + nudge availability
# --------------------------------------------------------------------------- #
@api.get("/service-account")
async def sa_status() -> dict[str, Any]:
    sa = db.get_service_account()
    return {"configured": bool(sa), "client_email": sa.get("client_email") if sa else None,
            "project_id": sa.get("project_id") if sa else None}


class SAIn(BaseModel):
    key_json: dict[str, Any]


@api.post("/service-account")
async def sa_upload(body: SAIn) -> dict[str, Any]:
    k = body.key_json
    if k.get("type") != "service_account" or "private_key" not in k:
        raise HTTPException(400, "That doesn't look like a service-account JSON key.")
    db.save_service_account(k)
    return {"ok": True, "client_email": k.get("client_email")}


@api.delete("/service-account")
async def sa_delete() -> dict[str, Any]:
    db.clear_service_account()
    return {"ok": True}


@api.get("/clients/{cid}/nudge-availability")
async def nudge_availability(cid: int) -> dict[str, Any]:
    client = db.get_client(cid)
    sa = db.get_service_account()
    if not client:
        raise HTTPException(404, "Client not found.")
    if not sa:
        return {"available": False, "reason": "no_service_account"}
    home = _home_of(client)
    if not home:
        return {"available": False, "reason": "no_url"}
    try:
        token = await indexing.get_access_token(sa["key"])
        state = await indexing.probe_permission(token, home)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"error: {exc}"}
    if state == "ok":
        return {"available": True}
    if state == "forbidden":
        return {"available": False, "reason": "not_owner", "client_email": sa["client_email"]}
    return {"available": False, "reason": "unknown"}


# --------------------------------------------------------------------------- #
# Step 3 — request indexing / submit sitemap
# --------------------------------------------------------------------------- #
class NudgeIn(BaseModel):
    urls: list[str]


@api.post("/clients/{cid}/request-indexing")
async def request_indexing(cid: int, body: NudgeIn) -> dict[str, Any]:
    client = db.get_client(cid)
    sa = db.get_service_account()
    if not client:
        raise HTTPException(404, "Client not found.")
    if not sa:
        raise HTTPException(400, "No service account configured — upload one, or use Submit Sitemap instead.")
    urls = [u for u in body.urls if u][:200]
    if not urls:
        raise HTTPException(400, "No URLs selected.")

    async def work(progress):
        token = await indexing.get_access_token(sa["key"])
        ok = 0
        for i, u in enumerate(urls):
            res = await indexing.publish(token, u)
            db.log_request(cid, u, "index", res["ok"], res["message"])
            if res["ok"]:
                ok += 1
                db.set_url_fields(cid, u, requested_at=db._now(), index_status="requested")
            progress(phase="nudge", done=i + 1, total=len(urls),
                     message=f"Requested {ok}/{len(urls)}…")
        return {"requested": ok, "total": len(urls)}

    job = jobs.start_job("nudge", cid, work)
    return {"job_id": job.id}


@api.post("/clients/{cid}/submit-sitemap")
async def submit_sitemap(cid: int) -> dict[str, Any]:
    client = db.get_client(cid)
    if not client:
        raise HTTPException(404, "Client not found.")
    prop = client.get("property_url")
    sm = client.get("sitemap_url")
    if not prop or not sm:
        raise HTTPException(400, "Need a saved GSC property and sitemap URL.")
    token = await _valid_access_token()
    ok = await gsc.submit_sitemap(token, prop, sm)
    db.log_request(cid, sm, "sitemap", ok, "submitted" if ok else "failed")
    return {"ok": ok}


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@api.get("/job/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job.to_dict()


@api.get("/clients/{cid}/active")
async def job_active(cid: int, kind: str) -> dict[str, Any]:
    job = jobs.active_for(kind, cid)
    return {"job": job.to_dict() if job else None}
