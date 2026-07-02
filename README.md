# Indexing Tool — README (as-built)

A standalone **Cortex module** (see `../../README.md`). Self-contained FastAPI + vanilla-JS
app backed by a **Supabase Postgres DB** (schema `indexing_tool`). Reads `DATABASE_URL`
(pooled, pgbouncer) / `DIRECT_URL` and the Google OAuth client id/secret from the shared
**Cortex-root `.env`** (found by walking up the directory tree, same pattern as the Keyword
Research Tools module).

> **Design rationale + decisions** live in [PLAN.md](PLAN.md). This README is the
> as-built reference. If you're continuing development in a new session, read both.

## What it does

An SEO **page-indexing checkup + fix-tracking** tool for programmatic-SEO sites where
pages fail to index. It:
1. **Crawls** a client's site (sitemap + full internal-link-graph crawl).
2. **Checks** each URL's Google index status (URL Inspection API) + impressions (Search Analytics).
3. **Diagnoses** *why* pages aren't indexed, buckets them by issue with a recommended fix.
4. Optionally **nudges** ready pages via the Indexing API (service account) + sitemap submit.
5. **Refreshes** over time; newly-indexed pages get a 🆕 tag (30-day flip detection).

Core philosophy: **diagnosis-first.** "Request Indexing" is a nudge, not a fix — it's the
last step, gated to pages where it helps.

## Status

Built and working end-to-end (crawl → check → diagnose → nudge → refresh). Verified against
solarth.co (Yoast/Elementor WordPress). Single agency Google login (v1); one optional global
service account.

## Run it

```powershell
cd "Cortex/Modules/Indexing Tool"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload --port 8010
```
Open **http://127.0.0.1:8010**. Add the redirect URI **`http://127.0.0.1:8010/api/oauth/callback`**
to the existing Google OAuth client (the same one KRT uses) so "Connect Google" works.

Requires `DATABASE_URL` (Supabase pooled connection, port 6543) and `DIRECT_URL` (direct,
port 5432) in the Cortex-root `.env` — `db.py` creates the `indexing_tool` schema/tables on
first run if they don't exist.

## Architecture / files

```
Indexing Tool/
├── app.py             # FastAPI app; runs on :8010 locally, Vercel entrypoint in prod
├── router.py          # all /api/* endpoints + the job "work" functions
├── db.py              # Supabase Postgres (schema `indexing_tool`): schema, CRUD, bulk updates
├── jobs.py            # background-job runner, state in Postgres (start_job/get/active_for)
├── core_crawl.py      # sitemap discovery + full-site BFS crawl + internal link graph
├── core_gsc.py        # Google OAuth + URL Inspection + Search Analytics + sitemap submit
├── core_indexing.py   # service-account Indexing API client (google-auth; optional)
├── diagnose.py        # THE ENGINE: signals → page_type, issue buckets, link quality, nudge eligibility
├── static/index.html  # entire UI (vanilla JS, single file), served via a FastAPI route
├── vercel.json · requirements.txt · .gitignore · PLAN.md · README.md
```

The heavy work (HTML parsing, diagnosis, DB writes, URL inspection) is offloaded via
`asyncio.to_thread` so long jobs don't block the event loop / freeze the UI.

## Data model (Supabase Postgres, schema `indexing_tool`)

- **clients**: `id, name, sitemap_url, site_url, property_url, created_at, updated_at`
- **urls**: one row per (client, url). Key columns: `url, title, lastmod, created_date,
  modified_date, http_status, first_seen, last_checked, sitemap_source, in_sitemap,
  meta_robots, index_status, coverage_state, verdict, google_canonical, user_canonical,
  robots_state, last_crawled, referring_urls_json, impressions, clicks, inbound_count,
  outbound_count, link_quality, click_depth, page_type, issue_type, severity,
  recommendation, warnings_json, nudge_eligible, requested_at, indexed_detected_at,
  prev_index_status`
  - **`created_date` and `first_seen` are keep-first** (never overwritten on re-crawl);
    everything else new-value-wins. `db.py` has a column migration for existing DBs.
- **links**: `client_id, from_url, to_url, anchor` — the internal link graph (self-links skipped).
- **auth**: single global Google OAuth token (id=1).
- **service_account**: single global uploaded Indexing-API key (id=1, optional).
- **request_log**: audit of indexing/sitemap requests.

## API endpoints (all under `/api`)

Clients: `GET/POST /clients`, `GET/PATCH/DELETE /clients/{id}`.
Data: `GET /clients/{id}/urls` (rows + summary), `GET /clients/{id}/url?url=` (detail + inbound/outbound lists),
`POST /clients/{id}/diagnose` (re-run engine, no crawl/network — the "Re-run checkup" button).
Jobs (return `{job_id}`, poll `GET /job/{id}`; `GET /clients/{id}/active?kind=`):
`POST /clients/{id}/crawl`, `/check`, `/refresh`, `/request-indexing`.
GSC: `GET /auth-status`, `/oauth/start`, `/oauth/callback`, `POST /disconnect`, `GET /properties`, `POST /clients/{id}/submit-sitemap`.
Service account: `GET/POST/DELETE /service-account`, `GET /clients/{id}/nudge-availability`.

## The diagnosis engine (`diagnose.py`)

Runs on stored data (no network). `diagnose(rows, inbound_map, outbound_map, home)` →
per-url `{index_status, page_type, link_quality, click_depth, inbound/outbound_count,
issue_type (headline), severity, recommendation, warnings[], nudge_eligible}`.

- **page_type** — `page_type(url, sitemap_source)`. Prefers Yoast **split-sitemap** type
  (`post-sitemap.xml`→Blog, `page-`→Page, `property-`→Product, `elementor_library-`→System,
  etc.) since flat slugs no longer encode it; falls back to URL heuristics. Values:
  Homepage, Page, Blog, Product, Company, Service, Category, Tag, Author, Pagination, Feed,
  Parameter, System, Utility, Media, Broken, External.
- **issue/warning types** (a page can have several; headline = highest severity):
  server_error, not_found, forbidden, redirect, **lost_page** (was ranking/indexed, now
  404/redirect), template_var, feed, param_url, archive, utility, **not_in_sitemap** (real
  content page missing from sitemap), duplicate_pattern, wrong_canonical, noindex,
  discovered, crawled_not_indexed, orphan, weak_links, deep. Healthy/unchecked when none.
- **link_quality**: orphan (0 inbound) / weak (only archive/index/author or non-indexed
  sources) / deep (≥4 clicks from home) / ok.
- **index_status**: indexed (impressions>0 or coverage says indexed) / not_indexed /
  requested / unknown (not yet checked).
- **nudge_eligible**: `index_status != indexed` AND not clutter/dup/error/noindex. Only
  eligible rows get a checkbox + count toward Request Indexing.

## Google integration

- **Reads (OAuth user login)** — reuse KRT's `GOOGLE_OAUTH_CLIENT_ID/SECRET`. Scope full
  `webmasters`. URL Inspection (`searchconsole.googleapis.com/v1/urlInspection/index:inspect`),
  Search Analytics (page dim), sitemap submit. **No batch API exists** → concurrency is the
  speed lever: `INSPECT_CONCURRENCY=8` (under Google's ~600/min), shared httpx client, one
  token per run, skip clutter page-types + already-indexed, cap `INSPECT_CAP=1800`/day.
- **Nudge (optional, Indexing API)** — one global **service account** JSON key (upload in
  Settings, or via file picker). Must be added as **Owner** of each property in Search
  Console. `~200 URLs/day` per Cloud project. `nudge-availability` probes per-property.
  Grey-area (officially job/event pages) — best-effort, surfaced in UI.

## Deploying (Vercel)

`app.py` exports `app = FastAPI()` at the repo root, which is a supported Vercel entrypoint —
no restructuring needed. `vercel.json` sets `maxDuration: 300` (the Hobby-plan ceiling; raise it
on Pro/Enterprise for large-site crawls — see [Vercel's duration docs](https://vercel.com/docs/functions/configuring-functions/duration)).

Required **Environment Variables** in the Vercel project (Settings → Environment Variables —
these are *not* read from a committed `.env`, which stays local/gitignored):
- `DATABASE_URL`, `DIRECT_URL` — the Supabase Postgres connection strings.
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.

After the first deploy, add the production callback URL —
`https://<your-domain>/api/oauth/callback` — to the Google OAuth client's authorized
redirect URIs (Google Cloud Console), same as the local `127.0.0.1:8010` one.

**Background jobs (crawl/check/refresh/nudge) run via `asyncio.create_task` in the process
that started them**, same as local dev — but a serverless instance isn't guaranteed to stay
warm for a job's full duration the way a persistent `uvicorn` process is. Job/progress *state*
lives in Postgres (`jobs` table) so polling works correctly no matter which instance answers a
given request, but if the instance running the job itself gets recycled mid-crawl, the job can
still stall. This is more likely to bite on very large sites; if a job seems stuck, the fallback
is the same as the pre-existing "interrupted crawl" gotcha below — rerun the step.

## Known gotchas / TODO

- **Stale link graph / dates need a full crawl.** In/Out (and Created/Modified) come from the
  crawl. If a crawl was interrupted (server auto-reload mid-run, or — on Vercel — an instance
  recycling mid-job — links save only on completion), the graph is partial → run
  **1. Fetch / Crawl site** to rebuild. "Re-run checkup" recomputes diagnosis from existing data
  but does NOT re-crawl.
- **Orphan detection is best-effort** — the crawler reads server-side HTML only, so
  JS-rendered nav isn't seen; cross-checked against GSC `referringUrls`. Self-links skipped.
- **Not-yet-implemented (proposed):** add GSC Search-Analytics pages that aren't in our set
  (live/indexed pages outside the sitemap+crawl) as a discovery source + flag. Only truly
  orphaned + non-ranking + non-sitemap URLs are undiscoverable.
- **Group-by-Issue** intentionally shows a multi-issue page under **each** of its issue groups
  (complete worklists); Type/Severity/Status group one-row-each.
- **Weekly GSC pull** for client-verifiable numbers — deferred.
- Everything is single-agency-login + one global service account (v1). Per-client Google
  accounts and moving GSC-auth into shared Cortex `systems/` are future work.
