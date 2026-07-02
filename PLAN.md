# Indexing Tool — Plan (v2 draft)

> **STATUS: BUILT.** This is the original design/rationale doc. For the as-built reference
> (files, data model, endpoints, engine, gotchas) see **[README.md](README.md)**. Kept for
> the "why" behind decisions. A few things evolved during the build (added: Dates columns,
> Page-Type column + multi-select filter, sortable columns, Group-by, `not_in_sitemap` +
> `lost_page` issues, concurrency/threading for the URL Inspection + no-hang UI, Help modal).

> Standalone Cortex module. Own folder, own SQLite DB, own FastAPI app — no shared
> code with other modules for now. Reuses *patterns* (not imports) from the Keyword
> Research Tools module: the GSC OAuth flow (`tools/gsc_stats/`) and the sitemap
> crawler (`sitemap_crawler.py`).

## 1. What it does (reframed)

A **page-indexing checkup + fix-tracking tool** for a client's site — built for
programmatic-SEO sites where lots of pages don't index.

It doesn't just check "indexed / not indexed and hit a button." It **diagnoses why
each page isn't indexed, buckets the problems, tells the specialist what actually
fixes each one**, and only *then* offers the Google "Request Indexing" nudge — for
the pages where a nudge will actually help (technically-fine or just-fixed pages),
not the ones that need content/structural work first.

Core loop: **Fetch → Check (GSC) → Diagnose → Recommend → (nudge the ready ones) →
Refresh over time to confirm fixes worked.**

## 2. The core: the Checkup (issue buckets)

Every URL gets classified into one **issue type** with a **severity** and a
**recommended action**. This is what turns a confusing list of 135 "crawled – not
indexed" rows into a handful of action lists. Buckets (derived from real solarth.co
data):

| # | Issue type | How we detect it | Severity | Recommendation shown | Nudge helps? |
|---|---|---|---|---|---|
| 1 | **Sitemap noise — feeds** | URL matches `/feed/` (or ends `/feed`) | 🟡 | "RSS feed shouldn't be in the sitemap or indexed. Remove from sitemap." | ❌ |
| 2 | **Sitemap noise — param/junk URLs** | has a query string (`?ref=`, `?s=`, `?utm=`, etc.) | 🟡 | "Parameter/duplicate URL. Canonicalize to the clean URL; exclude from sitemap." | ❌ |
| 3 | **Broken URL template** | URL contains `{`, `}`, `%7B`/`%7D` (unresolved var, e.g. `?s={search_term_string}`) | 🔴 | "A template variable leaked into a live URL. Fix the template/schema emitting it." | ❌ |
| 4 | **Low-value archive** | `/author/`, `/tag/`, `/category/.../page/N/`, bare category/date archives | 🟡 | "Thin archive. `noindex` or leave it — don't fight to index. Remove from sitemap if noindexed." | ❌ |
| 5 | **Duplicate URL architecture** | same final slug appears under 2+ path prefixes (e.g. `/x` **and** `/blog/x`, `/property/x` **and** `/blog/property/x`); or GSC `googleCanonical ≠ userCanonical` / "Duplicate…"/"Alternate page…" | 🔴 | "Same content at multiple URLs. Pick one structure, 301 the rest, drop dupes from the sitemap." | ❌ |
| 6 | **Orphan page** | in sitemap but **0 internal inbound links** in our crawl graph (and/or GSC `referringUrls` empty / "None detected") | 🔴 | "Nothing links to this page. Add internal links from relevant hub/category/related pages." | after fix |
| 7 | **Thin / crawled-not-indexed** | GSC `Crawled - currently not indexed`, page is 200 + indexable, not in buckets 1–6 (optionally our low-word-count heuristic) | 🟡 | "Google crawled it and judged it low-value. Add unique content, cut boilerplate, add internal links — then request indexing." | after fix |
| 8 | **Discovered - not indexed** | GSC `Discovered - currently not indexed` | 🟡 | "Google knows it but hasn't crawled — usually crawl-budget/quality. Improve internal linking; a nudge can help here." | ✅ (helps) |
| 9 | **Technical error** | our HTTP fetch or GSC = 404 / 5xx / 403 / soft-404 / "Page with redirect" | 🔴 | Per type: 404 → remove from sitemap or restore; redirect → point sitemap at the final URL; 403/5xx → server/WAF blocking Googlebot. | ❌ |
| 10 | **Blocked by noindex / robots** | GSC "Excluded by 'noindex'" or `robotsTxtState=DISALLOWED` | 🟡 | "If you want it indexed, remove the noindex/robots block. If intentional, remove from sitemap." | ❌ |
| 11 | **Indexed — healthy** | GSC indexed (or has impressions in Search Analytics) | 🟢 | none (optionally flag "indexed but 0 impressions" = relevance/content issue, not indexing) | n/a |

> The buckets are ordered as a rough fix-priority: hygiene (1–4) and dupes (5) first
> (cheap, sitewide lift), then orphans/thin (6–8), with technical errors (9) treated
> as urgent one-offs. **Request Indexing is only offered for buckets where it does
> something** (8, and 6–7 *after* the underlying fix), never for hygiene/dupes.

### 2a. Link-quality sub-signals (from the full link graph)

Beyond the pure orphan check (bucket 6), each page gets a **link-quality** grade
using *who* links to it, surfaced as an extra warning + recommendation:

| Grade | Detection | Recommendation |
|---|---|---|
| **Orphan** | 0 inbound internal links (and GSC `referringUrls` empty) | add internal links from relevant indexed pages |
| **Weakly linked** | inbound links exist but *only* from low-value sources (blog index, `/author/`, `/tag/`, pagination, category archives) **or** only from pages that are themselves *not indexed* | add a **contextual** link from a relevant, already-indexed content/service page |
| **Deep** | click-depth from the homepage ≥ 4 (BFS over the graph) | shorten the path — link it closer to a top-level hub |
| **OK** | ≥1 inbound link from a relevant indexed content page | — |

Also: **Too new to judge** — `lastmod`/first-seen < ~14 days → don't flag as a failure
yet (new pages legitimately take time), shown as an info note, not a red/yellow warning.

## 3. Data sources & credentials

**Reads (OAuth user login — reuse KRT `gsc_stats` flow):**
- **Sitemap crawl** (our own, no API) → the URL universe: url, title, lastmod, HTTP status.
- **Internal link graph** (our own crawl) → parse each page's internal `<a href>`s →
  inbound-link count per URL → drives **orphan** detection. (Extends the KRT crawler,
  which already fetches each page for its title.)
- **URL Inspection API** (`urlInspection.index.inspect`) → per-URL `verdict`,
  `coverageState` (the reason), `lastCrawlTime`, `googleCanonical`/`userCanonical`,
  `robotsTxtState`, `referringUrls`, `sitemap`. Quota **~2,000/day per property** →
  batch/cache; only (re)inspect gaps & stale rows.
- **Search Analytics API** (dimension `page`) → free, unlimited "**definitely
  indexed + getting impressions**" signal + impressions/clicks for **prioritization**
  (fix the orphan that's *close* to traffic first). Any URL with impressions = indexed,
  so we skip inspecting those and save quota.

**Writes (nudge — Indexing API, separate credential):**
- **Indexing API** (`urlNotifications:publish`, `URL_UPDATED`) via an uploaded
  **service-account JSON key** (added as Owner of the property; ~200 URLs/day). Used
  only by the Request Indexing button, only on nudge-eligible rows.
- **Sitemap submit** (`sitemaps.submit`, optional) — resubmit the sitemap as the
  passive baseline after cleanup.

> Scopes: OAuth `https://www.googleapis.com/auth/webmasters` (full, so sitemap submit
> works; readonly is enough for inspection). Service account `.../auth/indexing`.
> **Agency-account assumption for v1:** one agency Google login covers all client
> properties; per client we just save which property. (Per-client logins = later.)

## 4. The table

Columns (default view):

| Column | Source |
|---|---|
| ☐ select | — (per row + "select all nudge-eligible") |
| URL | crawl (path shown, full on hover) |
| Title | crawl (`<title>`→`<h1>`) |
| **Index status** | GSC / Search Analytics — `Indexed` / `Not indexed` / `Requested — {date}` |
| **Issue type** | diagnosis engine — the bucket from §2 |
| **Severity** | 🔴/🟡/🟢 |
| **Recommendation** | diagnosis engine — short action, expandable to detail |
| Reason (raw) | GSC `coverageState` (the exact Google wording) |
| Impressions | Search Analytics (prioritization; blank = none) |
| **Inbound internal links** | our crawl graph — count; expandable to the list of pages linking *to* this page (0 = orphan) |
| **Outbound internal links** | our crawl graph — count; expandable to the list of pages this page links *to* |
| Link quality | derived — Orphan / Weakly-linked / Deep / OK (see §2a) |
| Canonical ✓/✗ | URL Inspection (Google-chosen ≠ this URL → ✗) |
| Last crawled | URL Inspection |
| Last checked | tool |
| 🆕 New | flip detection (see §6) |

Filters: by issue type, severity, index status, nudge-eligible, has-impressions,
orphan-only. "Select all" selects **nudge-eligible unindexed** only (with a manual
override).

## 5. The Checkup dashboard (top of the tool)

A summary that mirrors GSC's "Why pages aren't indexed" but **enriched with our extra
buckets (orphans, feeds, dupes) and a recommendation per bucket** — each row clickable
to filter the table:

```
Indexed 412 / 690   ·   Not indexed 278   ·   Requested 40

🔴 Duplicate URL architecture     58   → consolidate to one URL structure (301)
🔴 Orphan pages                   34   → add internal links
🔴 Technical errors (404/redirect)33   → fix/remove from sitemap
🟡 Thin / crawled-not-indexed    129   → improve content, then request indexing
🟡 Discovered - not indexed       73   → internal links (+ nudge helps here)
🟡 Sitemap noise (feeds/params)   19   → remove from sitemap / noindex
🟢 Indexed & healthy             412
```

## 6. Workflow

**First run (per client)**
1. **Fetch** — enter website URL / sitemap URL / upload sitemap → crawl sitemaps
   (index + `.gz`), collect url + title + lastmod + HTTP status, **and build the
   internal-link graph**. Table populates; diagnosis columns blank.
2. **Connect GSC & check** — OAuth login → pick property → Save. Pull Search
   Analytics (free indexed signal) then URL Inspection on the gaps (quota-aware).
3. **Diagnose** — run the checkup: assign issue type + severity + recommendation to
   every row; render the dashboard.
4. **Act** — filter to a bucket, follow the recommendation (hand hygiene/dupe/orphan
   fixes to the content/dev team; export lists). For **nudge-eligible** rows, select
   → **Request Indexing**. Optionally resubmit the sitemap.

**Saved after first run** — client record persists (sitemap URL, property, tokens,
service-account, and the full table/diagnosis). Two top buttons:
- **Refresh data** — re-run Fetch + Check + Diagnose → update statuses, add new URLs,
  re-bucket. Detects fixes that worked.
- **Request Indexing** — as above.

## 7. "New / newly indexed" (flip detection)

Google gives no clean "date indexed," so we detect it ourselves: each row stores its
previous status; on a Refresh, when it flips **Not indexed/Requested → Indexed**, we
stamp `indexed_detected_at = today`. Show a **🆕 New** pill when that's within the last
**30 days** — which also gives a real "indexed this month" number for client reporting,
and lets us show *which fixes are working*.

## 8. Client model

Light, like KRT but simpler: **name + sitemap URL** (+ saved property, OAuth token,
service-account, and the persisted table). Client selector/creator at top, same UX
pattern as KRT.

## 9. Module architecture (mirrors the KRT tool pattern)

```
Indexing Tool/
├── app.py               # small FastAPI app (own port)
├── requirements.txt · .gitignore
├── db.py                # SQLite: clients, urls (+diagnosis), links, auth, service_account, request_log
├── core_crawl.py        # sitemap discovery + page fetch + internal-link extraction (adapts KRT sitemap_crawler.py)
├── core_gsc.py          # OAuth + URL Inspection + Search Analytics + sitemap submit (adapts gsc_stats/core.py)
├── core_indexing.py     # service-account Indexing API client (new)
├── diagnose.py          # THE ENGINE: signals → issue type + severity + recommendation + nudge-eligibility
├── jobs.py              # background-job runner (adapts KRT jobs.py) — fetch/check are long
├── router.py            # API endpoints
└── static/index.html    # UI: client selector, checkup dashboard, table, filters (vanilla JS, KRT style)
```
Reads Google OAuth client id/secret from the shared **Cortex-root `.env`** (walk-up),
same as KRT. Otherwise fully self-contained.

## 10. Data model (first cut)

- **clients**: `id, name, sitemap_url, property_url, created_at`
- **auth**: OAuth token (single agency account, v1) — like gsc_stats `auth`
- **service_account**: uploaded Indexing-API JSON key
- **urls**: `id, client_id, url, title, lastmod, http_status, index_status, coverage_state,
  issue_type, severity, recommendation, google_canonical, inbound_links, impressions,
  clicks, last_crawled, last_checked, requested_at, indexed_detected_at, sitemap_source`
- **links**: `client_id, from_url, to_url` (internal-link graph → inbound counts / orphans)
- **request_log**: `client_id, url, requested_at, api_response` (audit + quota tracking)

## 11. Build phases

1. **Scaffold** — app boots; client create/select (name + sitemap URL); empty table + dashboard shell.
2. **Fetch (Step 1)** — sitemap crawl + internal-link graph → populate `urls` + `links`.
3. **Diagnosis engine v1 (offline signals)** — buckets that need no GSC yet: feeds,
   params, template leaks, archives, **duplicate-URL detection**, **orphans**, HTTP
   errors. Dashboard + recommendations live already.
4. **GSC check (Step 2)** — OAuth + property save + Search Analytics + URL Inspection
   (quota-aware) → fill index status, coverage reason, canonical → complete buckets 5–11.
5. **Nudge (Step 3)** — service-account upload + Indexing API + eligibility gating +
   `Requested — {date}` stamping + request log.
6. **Persistence & Refresh** — save per client; Refresh re-runs + flip detection → 🆕 pill + "fixes working" view.
7. **Polish** — filters, quota meter, bucket tooltips, export per bucket, sitemap resubmit.

## 12. Decisions (resolved) & caveats

Resolved 2026-07-01:
- **Nudge mechanism — BOTH.** Indexing API (per-URL nudge, via service account) **and**
  sitemap submit (passive baseline). Build the service-account flow.
- **Google account model — single agency login** for all clients (v1). Per-client
  logins deferred.
- **Service account — ONE, global, and OPTIONAL** (not per client). Added as **Owner**
  on a client property to enable the one-click Indexing API nudge there. Stored as a
  single global setting; ~200/day quota shared across clients (per Cloud project).
  **The tool is fully functional with just the GSC login** — crawl, checkup,
  diagnosis, recommendations, and **sitemap submit** all work without it. The per-URL
  Indexing API nudge is a bonus that lights up **only for properties where the robot
  is an Owner** (some clients won't grant it). Per client, the tool detects whether the
  nudge is available; where it isn't, it falls back to sitemap submit + a note (and the
  manual "Request Indexing in GSC" option).
- **Link-graph crawl — FULL SITE crawl** (BFS from homepage + sitemap, following
  internal links, bounded by a page cap), not just sitemap URLs — so links from menus,
  related-posts, and non-sitemap pages are captured. Cross-checked against GSC
  `referringUrls`.
- **Inbound + outbound link columns** — the table shows both an **inbound internal
  links** count (which pages link *to* this page) and an **outbound internal links**
  count (which pages this page links to), each expandable to the actual URL list.

Caveats:
- **JS-rendered nav isn't seen** (server-side HTML only) → orphan flag is "best-effort,"
  backstopped by GSC `referringUrls`.
- **Quota** — URL Inspection 2,000/day/property: fine up to ~2k URLs in one pass;
  larger sites batch over days (prioritized by impressions/importance).
- **Indexing API is grey-area** — officially for job/event pages; a crawl nudge, not a
  guarantee, ~200 URLs/day. UI must say so.

## 13. One-time Google setup (what you register for)

**Reading (OAuth) — nothing new.** Reuse KRT's existing `GOOGLE_OAUTH_CLIENT_ID` /
`GOOGLE_OAUTH_CLIENT_SECRET` from the Cortex-root `.env`. Only add this tool's OAuth
**redirect URI** to that existing client (it runs on its own port).

**Nudge (Indexing API) — one-time, ~10 min in Google Cloud Console:**
1. In a Cloud project (can reuse the KRT OAuth project) → enable the **"Web Search
   Indexing API."**
2. Create a **Service Account** → create a **JSON key** → download it.
3. In **Search Console**, per client property: Settings → Users and permissions → **Add
   user** → paste the service account's `…gserviceaccount.com` email → role **Owner**.
4. Upload the JSON key into the Indexing Tool once.

**Sitemap submit — nothing extra** (uses the OAuth login, full `webmasters` scope).

> Not blocking the build: the nudge is phase 5, so phases 1–4 (fetch → diagnose → GSC
> checkup) can be built before this setup is done.
