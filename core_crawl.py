#!/usr/bin/env python3
"""
core_crawl.py — full-site crawler + internal link graph.

Two jobs in one pass:
  1. Discover the client's pages (sitemap discovery → URLs + lastmod), then a
     bounded breadth-first crawl of the whole site starting from the homepage +
     every sitemap URL, following internal links.
  2. Build the internal link graph — for every page, which internal pages it links
     to (outbound) and, by inversion, which pages link to it (inbound).

Pure HTTP, stdlib + httpx only (no API/LLM cost). A real browser User-Agent is used
so ordinary WordPress sites behind a WAF don't silently 403.
"""
from __future__ import annotations

import asyncio
import gzip
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, urlunparse

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
FETCH_CONCURRENCY = 8
REQUEST_TIMEOUT = 15
DEFAULT_MAX_PAGES = 2000
HARD_MAX_PAGES = 20000
_PARSE_CAP = 800_000
_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip",
             ".mp4", ".mp3", ".css", ".js", ".ico", ".woff", ".woff2", ".ttf", ".xml")


# --------------------------------------------------------------------------- #
# HTML parsing — <title>, first <h1>, and all internal <a href> + anchor text
# --------------------------------------------------------------------------- #
class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._in_h1 = False
        self._title_done = False
        self._h1_done = False
        self._a_depth = 0
        self._cur_href = ""
        self._cur_anchor = ""
        self.title = ""
        self.h1 = ""
        self.links: list[tuple[str, str]] = []   # (href, anchor)
        self.meta_robots = ""
        self.published_time = ""
        self.modified_time = ""

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        d = dict(attrs)
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True
        elif tag == "a":
            href = (d.get("href") or "").strip()
            if href:
                self._a_depth += 1
                self._cur_href = href
                self._cur_anchor = ""
        elif tag == "meta":
            prop = (d.get("property") or d.get("name") or "").lower()
            content = d.get("content") or ""
            if prop == "robots":
                self.meta_robots = content.lower()
            elif prop == "article:published_time":
                self.published_time = content
            elif prop == "article:modified_time":
                self.modified_time = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True
        elif tag == "a" and self._a_depth > 0:
            self._a_depth -= 1
            self.links.append((self._cur_href, " ".join(self._cur_anchor.split())[:120]))
            self._cur_href = ""

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data
        if self._a_depth > 0:
            self._cur_anchor += data


def _parse_page(html: str) -> _PageParser:
    p = _PageParser()
    try:
        p.feed(html[:_PARSE_CAP])
    except Exception:
        pass
    p.title = " ".join(p.title.split())
    p.h1 = " ".join(p.h1.split())
    return p


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def normalize_base(url: str) -> str | None:
    s = (url or "").strip()
    if not s:
        return None
    if "//" not in s:
        s = "https://" + s
    p = urlparse(s)
    if not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def _host(netloc: str) -> str:
    return netloc.lower().split(":")[0].removeprefix("www.")


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Absolute, fragment-stripped, canonical-scheme/host URL. None for non-page links."""
    s = (url or "").strip()
    if not s or s.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    if base:
        s = urljoin(base, s)
    p = urlparse(s)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    netloc = p.netloc.lower()
    # drop default ports
    if netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))


def same_site(url: str, base: str) -> bool:
    try:
        return _host(urlparse(url).netloc) == _host(urlparse(base).netloc)
    except Exception:
        return False


def _ensure_scheme(url: str) -> str:
    s = (url or "").strip()
    return s if "//" in s else "https://" + s


def looks_like_sitemap_url(url: str) -> bool:
    p = urlparse(_ensure_scheme((url or "").strip()))
    path = (p.path or "").lower()
    if not path or path == "/":
        return False
    return path.endswith(".xml") or path.endswith(".xml.gz") or "sitemap" in path


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: Any, name: str) -> str:
    for child in el:
        if _local_tag(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _skip_asset(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(_SKIP_EXT)


# --------------------------------------------------------------------------- #
# Sitemap discovery
# --------------------------------------------------------------------------- #
async def _discover_sitemaps(client: Any, base: str) -> list[str]:
    found: list[str] = []
    try:
        r = await client.get(base + "/robots.txt")
        if r.status_code < 400:
            for line in r.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    found.append(line.split(":", 1)[1].strip())
    except Exception:
        pass
    if not found:
        found = [base + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")]
    return found


async def _collect_sitemap_urls(
    client: Any, sitemap_urls: list[str], cap: int,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Walk sitemaps (+ indexes) → {url: {lastmod, source}}. Diagnostics on the side."""
    out: dict[str, dict[str, str]] = {}
    stats = {"seen": 0, "ok": 0, "failed": 0}
    seen: set[str] = set()
    queue = list(sitemap_urls)
    while queue and len(out) < cap:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        stats["seen"] += 1
        try:
            r = await client.get(sm)
            if r.status_code >= 400:
                stats["failed"] += 1
                continue
            content = r.content
            if sm.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
                try:
                    content = gzip.decompress(content)
                except Exception:
                    pass
            root = ET.fromstring(content)
        except Exception:
            stats["failed"] += 1
            continue
        stats["ok"] += 1
        if _local_tag(root.tag) == "sitemapindex":
            for el in root:
                loc = _child_text(el, "loc")
                if loc:
                    queue.append(loc)
        else:
            for el in root:
                loc = _child_text(el, "loc")
                if not loc:
                    continue
                nu = normalize_url(loc)
                if nu:
                    out[nu] = {"lastmod": _child_text(el, "lastmod"), "source": sm}
                if len(out) >= cap:
                    break
    return out, stats


# --------------------------------------------------------------------------- #
# Page fetch
# --------------------------------------------------------------------------- #
async def _fetch(client: Any, url: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        title = h1 = ""
        links: list[tuple[str, str]] = []
        status: int | None = None
        redirected_to = ""
        meta_robots = published_time = modified_time = ""
        lastmod_hdr = ""
        try:
            r = await client.get(url)
            status = r.history[0].status_code if r.history else r.status_code
            if r.history:
                redirected_to = str(r.url)
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code < 400 and ("html" in ctype or not ctype):
                # parse off the event loop — big HTML is CPU-heavy and would otherwise
                # block the server (making the UI hang) during a crawl.
                parsed = await asyncio.to_thread(_parse_page, r.text)
                title, h1, meta_robots = parsed.title, parsed.h1, parsed.meta_robots
                published_time, modified_time = parsed.published_time, parsed.modified_time
                links = parsed.links
                lastmod_hdr = r.headers.get("last-modified", "")
        except Exception:
            pass
        return {
            "url": url, "title": title or h1, "http_status": status,
            "links": links, "lastmod_hdr": lastmod_hdr,
            "redirected_to": redirected_to, "meta_robots": meta_robots,
            "published_time": published_time, "modified_time": modified_time,
        }


# --------------------------------------------------------------------------- #
# Public entry point — full-site BFS crawl + link graph
# --------------------------------------------------------------------------- #
async def crawl_site(
    url: str, max_pages: int = DEFAULT_MAX_PAGES, progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    import httpx

    raw = (url or "").strip()
    base = normalize_base(raw)
    if not base:
        raise RuntimeError("Invalid website URL.")
    cap = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), HARD_MAX_PAGES))
    direct_sitemap = looks_like_sitemap_url(raw)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, headers=DEFAULT_HEADERS, follow_redirects=True,
    ) as client:
        # 1. sitemap discovery
        if progress:
            progress(phase="sitemap", message="Reading the sitemap…")
        sitemaps = [_ensure_scheme(raw)] if direct_sitemap else await _discover_sitemaps(client, base)
        sitemap_map, sm_stats = await _collect_sitemap_urls(client, sitemaps, cap)

        # 2. BFS crawl (homepage + all sitemap URLs as seeds)
        pages: dict[str, dict[str, Any]] = {}
        edges: list[tuple[str, str, str]] = []
        visited: set[str] = set()
        home = normalize_url(base + "/")
        frontier: list[str] = []
        for seed in ([home] if home else []) + list(sitemap_map.keys()):
            if seed and seed not in visited and not _skip_asset(seed):
                visited.add(seed)
                frontier.append(seed)

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        while frontier and len(pages) < cap:
            batch, frontier = frontier[:200], frontier[200:]
            results = await asyncio.gather(*[_fetch(client, u, sem) for u in batch])
            next_frontier: list[str] = []
            for res in results:
                u = res["url"]
                sm = sitemap_map.get(u, {})
                pages[u] = {
                    "url": u,
                    "title": res["title"],
                    "http_status": res["http_status"],
                    "lastmod": sm.get("lastmod") or res["lastmod_hdr"] or "",
                    "in_sitemap": u in sitemap_map,
                    "sitemap_source": sm.get("source", ""),
                    "redirected_to": res["redirected_to"],
                    "meta_robots": res["meta_robots"],
                    "published_time": res["published_time"],
                    "modified_time": res["modified_time"],
                }
                for href, anchor in res["links"]:
                    nu = normalize_url(href, base=u)
                    if not nu or nu == u or not same_site(nu, base) or _skip_asset(nu):
                        continue   # skip self-links (a page linking to itself isn't an inbound link)
                    edges.append((u, nu, anchor))
                    if nu not in visited and len(visited) < cap:
                        visited.add(nu)
                        next_frontier.append(nu)
            frontier = next_frontier + frontier
            if progress:
                progress(phase="crawl", done=len(pages), total=min(len(visited), cap),
                         message=f"Crawled {len(pages)} pages, found {len(edges)} internal links…")

    note = ""
    if not pages:
        if sm_stats["failed"] and not sm_stats["ok"]:
            note = ("Couldn't read the sitemap and the homepage returned nothing — the site "
                    "may block automated requests (anti-bot/WAF). Try the exact sitemap URL.")
        else:
            note = "No pages found."
    return {
        "pages": list(pages.values()),
        "edges": edges,                       # (from_url, to_url, anchor), normalized
        "count": len(pages),
        "sitemap_count": len(sitemap_map),
        "edge_count": len(edges),
        "truncated": len(visited) >= cap,
        "note": note,
    }
