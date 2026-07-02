#!/usr/bin/env python3
"""
diagnose.py — the checkup engine.

Takes the crawl + link graph + GSC signals for a client and, for every URL, derives:
  * index_status   Indexed / Not indexed / Requested / Unknown
  * link_quality   Orphan / Weak / Deep / OK  (from the full inbound link graph)
  * click_depth    clicks from the homepage (BFS over outbound links)
  * warnings[]     every issue found (type, severity, message, recommendation)
  * issue_type + severity + recommendation   the single headline issue (worst first)
  * nudge_eligible whether Request Indexing would actually do something

No network, no AI — pure signal combination. The offline buckets (feeds, params,
template leaks, archives, duplicates, orphans, HTTP errors) work before GSC is even
connected; the coverage-based buckets fill in once URL Inspection has run.
"""
from __future__ import annotations

from collections import deque
from urllib.parse import unquote, urlparse

RED, YELLOW, GREEN, INFO = "red", "yellow", "green", "info"
_RANK = {RED: 3, YELLOW: 2, INFO: 1, GREEN: 0}

# Low-value link sources: a link from one of these carries little indexing weight.
_ARCHIVE_HINTS = ("/author/", "/tag/", "/category/", "/page/", "/feed", "/archive")


# --------------------------------------------------------------------------- #
# URL pattern classifiers
# --------------------------------------------------------------------------- #
def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _query(url: str) -> str:
    return urlparse(url).query or ""


def _slug(url: str) -> str:
    segs = [s for s in _path(url).split("/") if s]
    return unquote(segs[-1]).lower() if segs else ""


def is_feed(url: str) -> bool:
    p = _path(url).lower()
    return p.endswith("/feed") or "/feed/" in p or p.endswith("/rss")


def has_template_var(url: str) -> bool:
    low = url.lower()
    return any(t in low for t in ("{", "}", "%7b", "%7d"))


def has_query(url: str) -> bool:
    return bool(_query(url))


def is_archive(url: str) -> bool:
    p = _path(url).lower()
    return ("/author/" in p or "/tag/" in p or "/page/" in p
            or p.startswith("/category/") or "/category/" in p or "/date/" in p)


def is_utility(url: str) -> bool:
    p = _path(url).lower()
    return any(k in p for k in ("/thank-you", "/thank_you", "/cart", "/checkout",
                                "/wp-json", "/wp-login", "/sitemap.htm", "/search"))


_MEDIA_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".mp4", ".mp3", ".zip")

# Yoast/RankMath split sitemaps by post type: `<type>-sitemap.xml`. The type token is
# the most reliable blog-vs-page signal (URL slugs no longer encode it).
_SITEMAP_TYPE = {
    "post": "Blog", "page": "Page", "property": "Product", "property-type": "Product",
    "product": "Product", "product-cat": "Category", "company": "Company",
    "service": "Service", "category": "Category", "authors": "Author", "author": "Author",
    "post-tag": "Tag", "elementor_library": "System", "installation-type": "Category",
    "externals": "External", "attachment": "Media",
}


def _type_from_sitemap(sitemap_source: str | None) -> str | None:
    if not sitemap_source:
        return None
    fn = sitemap_source.rsplit("/", 1)[-1].lower()
    token = fn.replace("-sitemap.xml", "").replace(".xml", "").replace("sitemap-", "")
    return _SITEMAP_TYPE.get(token)


def page_type(url: str, sitemap_source: str | None = None) -> str:
    """A filterable page-type label — separates real content (Page/Blog/Product) from
    clutter (System/Parameter/Author/…). WordPress-aware.

    Order: definite-clutter URL signals first, then the split-sitemap type (most reliable
    for blog-vs-page since slugs are now flat), then URL heuristics as a fallback."""
    low = url.lower()
    p = _path(url).lower()
    if has_template_var(url):
        return "Broken"
    if is_feed(url):
        return "Feed"
    if any(k in low for k in ("/wp-json", "/wp-admin", "/wp-content", "elementor",
                              "/cgi-bin", "/xmlrpc", "/wp-includes", "?p=", "?attachment_id=")):
        return "System"
    if _query(url):
        return "Parameter"
    if p in ("/", ""):
        return "Homepage"
    # split-sitemap type (authoritative on WP/Yoast sites)
    from_sm = _type_from_sitemap(sitemap_source)
    if from_sm:
        return from_sm
    # URL heuristics fallback (sites without split sitemaps / URLs found only by crawl)
    if "/author/" in p:
        return "Author"
    if "/tag/" in p:
        return "Tag"
    if "/page/" in p:
        return "Pagination"
    if p.startswith("/category/") or "/category/" in p or "/date/" in p:
        return "Category"
    if p.endswith(_MEDIA_EXT):
        return "Media"
    if any(k in p for k in ("/product/", "/property/", "/shop/", "/item/")):
        return "Product"
    if any(k in p for k in ("/thank-you", "/cart", "/checkout", "/search", "/contact")):
        return "Utility"
    if p.startswith("/blog") or "/blog/" in p:
        return "Blog"
    return "Page"


def source_type(url: str) -> str:
    if is_feed(url):
        return "feed"
    p = _path(url).lower()
    if "/author/" in p:
        return "author"
    if "/tag/" in p:
        return "tag"
    if "/page/" in p:
        return "pagination"
    if p.startswith("/category/") or "/category/" in p:
        return "archive"
    if p in ("/", "/blog", "/blog/"):
        return "index"
    return "content"


def _is_home(url: str) -> bool:
    return _path(url) in ("/", "")


# --------------------------------------------------------------------------- #
# index status
# --------------------------------------------------------------------------- #
def derive_index_status(coverage_state: str | None, impressions: int, requested_at: str | None) -> str:
    if impressions and impressions > 0:
        return "indexed"
    low = (coverage_state or "").lower()
    if low:
        if "not indexed" in low or "excluded" in low or "duplicate" in low or "alternate" in low \
                or "not found" in low or "redirect" in low or "error" in low or "blocked" in low \
                or "crawled" in low or "discovered" in low:
            return "requested" if requested_at else "not_indexed"
        if "indexed" in low:
            return "indexed"
        return "not_indexed"
    return "requested" if requested_at else "unknown"


# --------------------------------------------------------------------------- #
# graph-derived signals
# --------------------------------------------------------------------------- #
def compute_depths(home: str | None, outbound: dict[str, list[str]], all_urls: set[str]) -> dict[str, int]:
    """Clicks from the homepage via internal links (BFS)."""
    start = home if home in all_urls else next((u for u in all_urls if _is_home(u)), None)
    depths: dict[str, int] = {}
    if not start:
        return depths
    q = deque([(start, 0)])
    depths[start] = 0
    while q:
        u, d = q.popleft()
        for v in outbound.get(u, []):
            if v in all_urls and v not in depths:
                depths[v] = d + 1
                q.append((v, d + 1))
    return depths


def detect_duplicate_clusters(all_urls: list[str]) -> set[str]:
    """URLs whose final slug is shared by another URL at a different path
    (e.g. /x and /blog/x, /property/y and /blog/property/y)."""
    by_slug: dict[str, set[str]] = {}
    for u in all_urls:
        s = _slug(u)
        if len(s) <= 2 or s.isdigit() or is_archive(u) or is_feed(u) or has_query(u):
            continue
        by_slug.setdefault(s, set()).add(u)
    flagged: set[str] = set()
    for s, urls in by_slug.items():
        if len({_path(u) for u in urls}) >= 2:
            flagged |= urls
    return flagged


# --------------------------------------------------------------------------- #
# verdict model — turns the raw warnings into ONE page-level action label
# --------------------------------------------------------------------------- #
# Page types that are non-content by nature. When one of these isn't indexed it's
# usually *correct* — the verdict is "Ignore / no action needed", not a problem to fix.
CLUTTER_TYPES = {"System", "Feed", "Parameter", "Media", "Utility",
                 "Author", "Tag", "Category", "Pagination", "External"}

# What each issue means for the user's to-do list.
_FIX = {"server_error", "not_found", "forbidden", "redirect", "lost_page", "template_var",
        "duplicate_pattern", "wrong_canonical", "orphan", "discovered", "crawled_not_indexed"}
_REVIEW = {"not_in_sitemap", "weak_links", "deep", "noindex"}
_IGNORE = {"feed", "param_url", "archive", "utility"}
_ACTION_OF_TYPE = {**{t: "fix" for t in _FIX},
                   **{t: "review" for t in _REVIEW},
                   **{t: "ignore" for t in _IGNORE}}
_ACTION_RANK = {"fix": 3, "review": 2, "ignore": 1}


def _warn_key(w: dict) -> tuple[int, int]:
    """Sort key so the most-actionable, highest-severity warning becomes the headline."""
    return (_ACTION_RANK.get(_ACTION_OF_TYPE.get(w["type"], "review"), 2),
            _RANK.get(w["severity"], 0))


def _verdict(pt: str, idx: str, warnings: list[dict], primary_w: dict | None) -> str:
    """Fix / Review / Ignore / OK / Unchecked — the page-level 'what do I do here' label."""
    if idx == "indexed":
        # it's already working; only escalate for a genuine red error (e.g. now redirects)
        hard = any(_ACTION_OF_TYPE.get(w["type"]) == "fix" and w["severity"] == RED
                   for w in warnings)
        return "fix" if hard else "ok"
    if pt in CLUTTER_TYPES:
        return "ignore"
    if primary_w:
        return _ACTION_OF_TYPE.get(primary_w["type"], "review")
    if idx == "unknown":
        return "unchecked"
    return "review"


def _clutter_rec(pt: str, url: str) -> dict:
    """The 'this is fine, and here's why' explanation for non-content page types."""
    if pt == "System":
        return {"problem": "Internal system / builder URL — no action needed.",
                "why": "This is a CMS or page-builder artifact (e.g. an Elementor library entry, "
                       "wp-json, or admin URL), not a real page.",
                "fix": ["It's correct that Google isn't indexing this.",
                        "Optional: block the pattern (e.g. ?elementor_library=) in robots.txt to save crawl budget."]}
    if pt == "Parameter":
        return {"problem": "Parameter URL — no action needed.",
                "why": "This is a query-string variant of a clean URL, not a distinct page.",
                "fix": ["It's correct that Google keeps the clean URL instead.",
                        "Keep parameter URLs out of the sitemap; optionally block the parameter in robots.txt."]}
    if pt == "Feed":
        return {"problem": "Feed URL — no action needed.",
                "why": "RSS/Atom feeds aren't content pages and shouldn't be indexed.",
                "fix": ["Just keep feeds out of the sitemap."]}
    if pt == "Media":
        return {"problem": "Media file — no action needed.",
                "why": "This is an image/document/media file, not an HTML page.",
                "fix": ["It's normal that it isn't indexed as a page."]}
    if pt == "Utility":
        return {"problem": "Utility page — no action needed.",
                "why": "This is a functional page (cart, search, thank-you, …) with nothing for searchers to land on.",
                "fix": ["noindex it and keep it out of the sitemap."]}
    if pt == "External":
        return {"problem": "External URL — no action needed.",
                "why": "This points to another domain, outside your site.",
                "fix": ["Nothing to do here."]}
    # Author / Tag / Category / Pagination
    return {"problem": f"{pt} archive — low value, no action needed.",
            "why": "Archive/listing pages are thin and rarely deserve to rank on their own.",
            "fix": ["Leave it, or noindex it — don't fight to index it.",
                    "If noindexed, drop it from the sitemap."]}


def _primary_rec(action: str, pt: str, url: str, primary_w: dict | None) -> dict:
    """The single structured recommendation shown in the table's Recommendation cell."""
    if action == "ignore":
        return _clutter_rec(pt, url)
    if action == "ok":
        return {"problem": "Indexed and healthy — no action needed.", "why": "", "fix": []}
    if action == "unchecked":
        return {"problem": "Not checked yet.", "why": "",
                "fix": ["Connect Search Console and run “Check index status”."]}
    if primary_w:
        return {"problem": primary_w["problem"], "why": primary_w.get("why", ""),
                "fix": list(primary_w.get("fix", []))}
    return {"problem": "Not indexed — reason unclear.", "why": "",
            "fix": ["Run “Check index status” to get Google's reason."]}


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def diagnose(rows: list[dict], inbound: dict[str, list[str]], outbound: dict[str, list[str]],
             home: str | None) -> dict[str, dict]:
    """Return {url: {index_status, link_quality, click_depth, issue_type, severity,
    recommendation, warnings, nudge_eligible}}."""
    all_urls = {r["url"] for r in rows}
    by_url = {r["url"]: r for r in rows}

    # pass 1 — index status for everyone (needed by link-quality)
    status: dict[str, str] = {}
    for r in rows:
        status[r["url"]] = derive_index_status(
            r.get("coverage_state"), int(r.get("impressions") or 0), r.get("requested_at"))

    depths = compute_depths(home, outbound, all_urls)
    dupes = detect_duplicate_clusters(list(all_urls))

    out: dict[str, dict] = {}
    for r in rows:
        url = r["url"]
        warnings: list[dict] = []

        def warn(t, sev, problem, why, fix):
            warnings.append({"type": t, "severity": sev, "problem": problem,
                             "why": why, "fix": fix if isinstance(fix, list) else [fix]})

        coverage = (r.get("coverage_state") or "")
        low = coverage.lower()
        http = r.get("http_status")
        gcanon = r.get("google_canonical") or ""
        robots = (r.get("robots_state") or "").lower()
        meta_robots = (r.get("meta_robots") or "").lower()
        idx = status[url]
        pt = page_type(url, r.get("sitemap_source"))
        was_live = int(r.get("impressions") or 0) > 0 or bool(r.get("indexed_detected_at"))

        # --- technical errors (HTTP) ---
        if http and http >= 500:
            warn("server_error", RED, f"Server error ({http}) — the page won't load for Google.",
                 "Google can't index a page it can't fetch; repeated errors get it dropped.",
                 ["Fix the server/hosting error (check the logs).", "Re-check once it returns 200."])
        elif http == 404:
            warn("not_found", RED, "Returns 404 (not found).",
                 "A 404 can't be indexed, and if it's in the sitemap it also wastes crawl budget.",
                 ["If the page should exist, restore it.",
                  "If not, drop it from the sitemap and 301-redirect it to the closest live page."])
        elif http == 403:
            warn("forbidden", RED, "Returns 403 (blocked).",
                 "The server or WAF is refusing the crawler, so Google can't read the page.",
                 ["Allow Googlebot through the server/WAF/firewall.", "Re-check once it returns 200."])
        elif http and 300 <= http < 400:
            warn("redirect", RED, "This URL redirects instead of serving content.",
                 "Google indexes the destination, not the redirecting URL — so this URL won't be the indexed one.",
                 ["Put the final destination URL in the sitemap, not this one.",
                  "Update internal links to point straight at the destination."])

        # --- lost page: was live / ranking, now deleted or redirected ---
        if (http == 404 or (http and 300 <= http < 400)) and was_live:
            warn("lost_page", RED, "Was getting Google traffic but now 404s or redirects.",
                 "You're losing rankings and traffic this page used to earn.",
                 ["Restore the page, or 301-redirect it to the closest equivalent.",
                  "Fix internal links and the sitemap to match."])

        # --- sitemap hygiene / should-not-be-indexed ---
        if has_template_var(url):
            warn("template_var", RED, "A template variable leaked into a live URL (e.g. {{…}}).",
                 "These are broken, auto-generated URLs — they can't rank and dilute crawl budget.",
                 ["Fix the template/schema emitting this URL (often a search box or dynamic field).",
                  "Block or 410 the broken pattern and keep it out of the sitemap."])
        if is_feed(url):
            warn("feed", YELLOW, "RSS/feed URL.",
                 "Feeds aren't content pages and shouldn't be indexed.",
                 ["Keep feeds out of the sitemap — otherwise no action needed."])
        elif has_query(url):
            warn("param_url", YELLOW, "Parameter/tracking URL — a duplicate of a clean URL.",
                 "Query-string variants split signals and waste crawl budget; Google keeps the clean version.",
                 ["Canonicalize to the clean URL.",
                  "Keep parameter URLs out of the sitemap.",
                  "Optional: block the parameter in robots.txt."])
        elif is_archive(url):
            warn("archive", YELLOW, "Low-value archive page (author/tag/category/pagination).",
                 "These thin listing pages rarely deserve to rank and can bloat the index.",
                 ["Leave it, or noindex it — don't fight to index it.",
                  "If noindexed, drop it from the sitemap."])
        elif is_utility(url):
            warn("utility", YELLOW, "Utility / non-content page.",
                 "There's nothing here for searchers to land on.",
                 ["noindex it and keep it out of the sitemap."])

        # --- live but not in the XML sitemap (real content pages only) ---
        if not r.get("in_sitemap") and pt in ("Homepage", "Page", "Blog", "Product", "Service", "Company"):
            warn("not_in_sitemap", YELLOW, "Live page that isn't in the XML sitemap.",
                 "Pages missing from the sitemap get discovered slower and can look lower-priority.",
                 ["If it should rank, add it to the sitemap.",
                  "If it's a leftover/orphan, 301-redirect or remove it."])

        # --- duplicate / canonical ---
        if url in dupes:
            warn("duplicate_pattern", RED, "Same content served at multiple URL paths (e.g. /x and /blog/x).",
                 "Duplicates split ranking signals and Google indexes only one — often not the one you want.",
                 ["Pick one canonical URL structure.",
                  "301-redirect the duplicates to it.",
                  "Drop the duplicates from the sitemap."])
        elif gcanon and gcanon.rstrip("/") != url.rstrip("/") and "duplicate" in low or "alternate" in low:
            warn("wrong_canonical", RED, "Google chose a different canonical than this URL.",
                 "Google treats another URL as the real one, so this URL won't be indexed on its own.",
                 ["Decide which URL should be canonical.",
                  "Make the intended page self-canonical and 301 the rest."])

        # --- robots / noindex ---
        if "noindex" in meta_robots or "excluded by 'noindex'" in low or "disallow" in robots:
            warn("noindex", YELLOW, "Blocked by a noindex tag or robots rule.",
                 "This explicitly tells Google not to index the page.",
                 ["If it should rank, remove the noindex/robots block.",
                  "If it shouldn't, this is correct — just drop it from the sitemap."])

        # --- coverage-based (needs GSC) ---
        if "discovered - currently not indexed" in low:
            warn("discovered", YELLOW, "Discovered – currently not indexed (Google knows the URL but hasn't crawled it).",
                 "Usually a crawl-budget / low-priority signal — Google hasn't prioritized fetching it.",
                 ["Add internal links from strong, already-indexed pages.",
                  "Make sure it's in the sitemap.",
                  "A Request Indexing nudge can help once linking is fixed."])
        elif "crawled - currently not indexed" in low:
            warn("crawled_not_indexed", YELLOW, "Crawled – currently not indexed (Google fetched it but judged it low value).",
                 "Usually thin, duplicate, or boilerplate-heavy content.",
                 ["Add unique, substantial content and cut boilerplate.",
                  "Add internal links from related pages.",
                  "Then request indexing."])

        # --- link quality (from the full graph) ---
        inbound_list = inbound.get(url, [])
        inbound_count = len(inbound_list)
        depth = depths.get(url)
        good_source = any(
            source_type(s) == "content" and status.get(s) == "indexed" for s in inbound_list
        )
        if _is_home(url):
            link_quality = "ok"
        elif inbound_count == 0:
            link_quality = "orphan"
            warn("orphan", RED, "Orphan — no internal links point here.",
                 "Google finds pages by following links; with none, it may never crawl it or may drop it.",
                 ["Add contextual links from 2–3 related, already-indexed pages.",
                  "Add it to a relevant hub/category page.",
                  "Make sure it's in the sitemap."])
        elif not good_source:
            link_quality = "weak"
            warn("weak_links", YELLOW, "Weakly linked — only from archives/index/author or non-indexed pages.",
                 "Links from low-value or unindexed pages pass little crawl priority.",
                 ["Add a contextual link from a relevant, already-indexed content or service page."])
        elif depth is not None and depth >= 4:
            link_quality = "deep"
            warn("deep", YELLOW, f"Deep page — {depth} clicks from the homepage.",
                 "Pages buried far from the homepage get crawled less often.",
                 ["Link it closer to a top-level hub or the main navigation."])
        else:
            link_quality = "ok"

        # --- headline + verdict + structured recommendation ---
        primary_w = max(warnings, key=_warn_key) if warnings else None
        action = _verdict(pt, idx, warnings, primary_w)
        rec = _primary_rec(action, pt, url, primary_w)
        # synthesis: for real pages with several issues, list the secondary ones (Idea 4)
        if primary_w and action in ("fix", "review"):
            more = []
            for w in sorted(warnings, key=_warn_key, reverse=True):
                if w is primary_w or _ACTION_OF_TYPE.get(w["type"]) == "ignore":
                    continue
                more.append({"type": w["type"], "fix": (w["fix"][0] if w["fix"] else "")})
            if more:
                rec["more"] = more[:3]

        if primary_w:
            if action == "ignore":
                ign = next((w for w in sorted(warnings, key=_warn_key, reverse=True)
                            if _ACTION_OF_TYPE.get(w["type"]) == "ignore"), None)
                issue_type = (ign or primary_w)["type"]
            else:
                issue_type = primary_w["type"]
        else:
            issue_type = ("healthy" if action == "ok"
                          else "unchecked" if action == "unchecked" else "not_indexed")
        severity = {"fix": (primary_w["severity"] if primary_w else RED),
                    "review": YELLOW, "ignore": INFO, "ok": GREEN, "unchecked": INFO}[action]
        recommendation = rec.get("problem", "")

        # --- nudge eligibility ---
        blockers = (is_feed(url) or has_query(url) or is_archive(url) or is_utility(url)
                    or has_template_var(url) or url in dupes
                    or (http is not None and http != 200)
                    or "noindex" in meta_robots or "disallow" in robots)
        # Eligible = a technically-valid content page that isn't already indexed.
        # "unknown" (not yet GSC-checked) counts, so pages are selectable right after a crawl.
        nudge_eligible = bool(idx != "indexed" and not blockers)

        out[url] = {
            "index_status": idx,
            "page_type": pt,
            "link_quality": link_quality,
            "click_depth": depth,
            "inbound_count": inbound_count,
            "outbound_count": len(outbound.get(url, [])),
            "issue_type": issue_type,
            "severity": severity,
            "recommendation": recommendation,
            "action": action,
            "rec": rec,
            "warnings": warnings,
            "nudge_eligible": nudge_eligible,
        }
    return out


# --------------------------------------------------------------------------- #
# dashboard rollup
# --------------------------------------------------------------------------- #
_BUCKET_LABELS = {
    "server_error": "Server errors", "not_found": "Not found (404)", "forbidden": "Blocked (403)",
    "redirect": "Redirects", "template_var": "Broken URL templates", "feed": "Feed URLs",
    "param_url": "Parameter URLs", "archive": "Low-value archives", "utility": "Utility pages",
    "duplicate_pattern": "Duplicate URL architecture", "wrong_canonical": "Wrong canonical",
    "noindex": "Blocked by noindex/robots", "discovered": "Discovered – not indexed",
    "crawled_not_indexed": "Crawled – not indexed", "orphan": "Orphan pages",
    "weak_links": "Weakly linked", "deep": "Deep pages", "healthy": "Indexed & healthy",
    "unchecked": "Not checked yet", "not_indexed": "Not indexed (other)",
    "not_in_sitemap": "Not in sitemap", "lost_page": "Lost page (404/redirect)",
}


def summarize(diagnosed: dict[str, dict]) -> dict:
    counts: dict[str, int] = {}
    idx = {"indexed": 0, "not_indexed": 0, "requested": 0, "unknown": 0}
    for d in diagnosed.values():
        counts[d["issue_type"]] = counts.get(d["issue_type"], 0) + 1
        idx[d["index_status"]] = idx.get(d["index_status"], 0) + 1
    buckets = [
        {"key": k, "label": _BUCKET_LABELS.get(k, k), "count": v,
         "severity": next((dd["severity"] for dd in diagnosed.values() if dd["issue_type"] == k), "info")}
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return {"index": idx, "buckets": buckets, "total": len(diagnosed)}
