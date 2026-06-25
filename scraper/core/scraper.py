"""
core/scraper.py — Generic scraper engine.

Provides:
  - Request-first → Playwright fallback content fetching
  - Text density scoring for automatic content detection (no hardcoded selectors)
  - JSON API response interception (catches sites that load text via fetch/XHR)
  - Next-chapter link auto-detection
  - TOC chapter link collection
  - Progress file for crash recovery / resume
  - scrape_url_list()       — sequential scrape of a known URL list
  - scrape_url_list_async() — parallel scrape of a known URL list
  - scrape_follow_links()   — scrape by following next-chapter links
"""
import asyncio
import json
import os
import random
import re
import time
from urllib.parse import urlparse

import requests

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False

from playwright.sync_api import sync_playwright
from core.browser import (
    make_context,
    get_text,
    USER_AGENT,
    SELECTORS_TEXT,
    BLOCKED_RESOURCES,
)

# Comma-joined selector used to wait for chapter content to appear.
# With domcontentloaded (instead of networkidle) we wait on the content itself
# rather than for all network traffic to settle — much faster on ad-heavy sites.
_CONTENT_WAIT = ", ".join(SELECTORS_TEXT)
_CONTENT_WAIT_TIMEOUT = 8000

HEADERS = {"User-Agent": USER_AGENT}

NEXT_TEXTS = [
    "next chapter", "next →", "next->", "next >", "next page", "→", ">>",
]

_NOISE_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "button"]
_CONTENT_TAGS = ["div", "article", "section", "main"]

_CHAPTER_LINK_SELECTORS = [
    "a[href*='chapter']",
    "a[href*='/ch-']",
    "a[href*='/c-']",
    "a[href*='/ep-']",
]

_WRITE_BATCH = 30          # chapters gathered before each disk write in async mode
_PROGRESS_CHECKPOINT = 25  # chapters between progress-file saves in sync mode


# ── Content extraction ────────────────────────────────────────────────────────

def extract_content_from_html(html: str) -> str:
    """
    Returns the most content-dense block from raw HTML.
    Scores every candidate tag by text-length / markup-length ratio.
    Falls back to joining all <p> tags if BeautifulSoup is not installed.
    """
    if not _BS4:
        return re.sub(r"<[^>]+>", " ", html).strip()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    best, best_score = None, 0.0
    for tag in soup.find_all(_CONTENT_TAGS):
        text = tag.get_text(strip=True)
        if len(text) < 200:
            continue
        score = len(text) / max(len(str(tag)), 1)
        if score > best_score:
            best, best_score = tag, score

    if best:
        return "\n".join(
            line.strip() for line in best.get_text("\n").splitlines() if line.strip()
        )
    return "\n".join(
        p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)
    )


# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_via_requests(url: str) -> str:
    """
    Plain HTTP GET. Returns extracted text when the response looks like a full
    page (>2 KB body, >300 chars of text). Returns empty string otherwise so
    the caller falls back to Playwright.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        if len(r.text) < 2000:
            return ""
        text = extract_content_from_html(r.text)
        return text if len(text) > 300 else ""
    except Exception:
        return ""


def _make_json_listener(bucket: list):
    """Returns a Playwright response handler that appends JSON payloads to bucket."""
    def on_response(response):
        if "json" in response.headers.get("content-type", ""):
            try:
                bucket.append(response.json())
            except Exception:
                pass
    return on_response


def _text_from_json(payloads: list) -> str:
    """
    Walks captured JSON payloads looking for a string value >500 chars —
    the most likely candidate for chapter text. Strips HTML tags before returning.
    """
    def walk(obj, depth=0) -> str:
        if depth > 6:
            return ""
        if isinstance(obj, str) and len(obj) > 500:
            return re.sub(r"<[^>]+>", "\n", obj).strip()
        if isinstance(obj, dict):
            for v in obj.values():
                found = walk(v, depth + 1)
                if found:
                    return found
        if isinstance(obj, list):
            for item in obj:
                found = walk(item, depth + 1)
                if found:
                    return found
        return ""

    for payload in payloads:
        result = walk(payload)
        if result:
            return result
    return ""


def fetch_text_from_page(page, url: str, json_bucket: list) -> str:
    """
    Navigates a Playwright page to url and returns chapter text.
    Priority: JSON API → known selectors → content-density scoring.
    json_bucket must be cleared by the caller before each call.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(_CONTENT_WAIT, state="attached", timeout=_CONTENT_WAIT_TIMEOUT)
    except Exception:
        pass  # content may load via JSON/density fallback instead

    api_text = _text_from_json(json_bucket)
    if len(api_text) > 300:
        return api_text

    known = get_text(page)
    if len(known) > 300:
        return known

    return extract_content_from_html(page.content())


# ── Link detection ────────────────────────────────────────────────────────────

def find_next_link(page) -> str:
    """
    Searches for a 'next chapter' anchor. Returns href or empty string.
    """
    for text in NEXT_TEXTS:
        try:
            loc = page.locator(f"a:has-text('{text}')").first
            if loc.count() > 0:
                href = loc.get_attribute("href") or ""
                if href:
                    return href
        except Exception:
            continue
    return ""


def find_chapter_links(page, base_domain: str) -> list:
    """
    Extracts chapter links from a TOC/index page.
    Returns a deduplicated, ordered list of absolute URLs.
    """
    seen, urls = set(), []
    for sel in _CHAPTER_LINK_SELECTORS:
        for loc in page.locator(sel).all():
            href = loc.get_attribute("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)
            full = href if href.startswith("http") else base_domain + href
            urls.append(full)
        if urls:
            break
    return urls


# ── Auto-detection (site-agnostic chapter-list discovery) ─────────────────────
#
# Instead of trusting a hardcoded selector/endpoint, we gather candidate link
# sets — every HTML/JSON response the page makes while loading, plus the
# rendered DOM — and score each on how much it *looks like* a chapter list:
#   - volume        : a chapter list has many links
#   - chapter ratio : how many hrefs contain the word "chapter"
#   - shared prefix : chapter links of one novel share a common path
#   - numbering     : chapter links carry sequential numbers
# The highest-scoring set wins. This verifies the SHAPE of the data, not its
# location, so it survives site redesigns (dropdown → AJAX → plain list).

def _extract_chapter_number(path: str):
    """Returns the chapter number embedded in a path, or None."""
    m = re.search(r"chapter[-_/]?(\d+)", path.lower())
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d+)(?:[-_./]|$)", path)
    return int(m.group(1)) if m else None


def _normalize_hrefs(hrefs: list, domain: str) -> list:
    """Absolute-ize, drop off-domain/junk links, dedupe preserving order."""
    base_netloc = urlparse(domain).netloc
    seen, out = set(), []
    for h in hrefs:
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if h.startswith("http"):
            full = h
        elif h.startswith("/"):
            full = domain + h
        else:
            full = domain + "/" + h
        if urlparse(full).netloc != base_netloc:
            continue
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _score_chapter_links(urls: list) -> float:
    """Scores a normalized URL set on how chapter-list-like it is (0–4)."""
    n = len(urls)
    if n < 5:
        return 0.0

    paths = [urlparse(u).path for u in urls]
    score = 0.0

    # 1. Volume — more links is more list-like (capped at 50)
    score += min(n, 50) / 50.0

    # 2. 'chapter' keyword ratio
    score += sum("chapter" in p.lower() for p in paths) / n

    # 3. Shared path prefix — chapters of one novel live under one path
    prefix = os.path.commonprefix(paths)
    if len(prefix) >= 2:
        score += sum(p.startswith(prefix) for p in paths) / n

    # 4. Sequential numbering present on at least half the links
    numbered = sum(_extract_chapter_number(p) is not None for p in paths)
    if numbered >= n * 0.5:
        score += 1.0

    return score


def _order_chapters(urls: list) -> list:
    """Sorts oldest-first by chapter number when most links are numbered."""
    if not urls:
        return urls
    keyed = [(_extract_chapter_number(urlparse(u).path), i, u) for i, u in enumerate(urls)]
    if sum(1 for num, _, _ in keyed if num is not None) >= len(urls) * 0.7:
        keyed.sort(key=lambda t: (t[0] if t[0] is not None else float("inf"), t[1]))
        return [u for _, _, u in keyed]
    return urls


def auto_detect_chapter_urls(page, url: str) -> list:
    """
    Site-agnostic chapter-list discovery.

    Listens to every HTML/JSON response the page makes while loading, plus the
    'chapter'-filtered rendered DOM, scores each candidate set, and returns the
    winner as absolute URLs, oldest-first. Returns [] if nothing scores.

    Self-healing: it verifies the shape of the data (volume, shared prefix,
    numbering) rather than a fixed selector, so it keeps working when a site
    moves its chapter list from a dropdown to an AJAX call to a plain list.
    """
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list = []  # each entry is a raw href list

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "html" not in ct and "json" not in ct:
                return
            body = resp.text()
        except Exception:
            return
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', body)
        if len(hrefs) >= 5:
            candidates.append(hrefs)

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # Rendered DOM as an extra candidate (covers client-rendered lists)
    try:
        dom_hrefs = [
            a.get_attribute("href")
            for a in page.locator("a[href*='chapter']").all()
        ]
        if dom_hrefs:
            candidates.append([h for h in dom_hrefs if h])
    except Exception:
        pass

    best, best_score = [], 0.0
    for hrefs in candidates:
        cleaned = _normalize_hrefs(hrefs, domain)
        s = _score_chapter_links(cleaned)
        if s > best_score:
            best, best_score = cleaned, s

    return _order_chapters(best)


# ── Progress tracking ─────────────────────────────────────────────────────────

class Progress:
    """
    Persists done/failed chapter indices and last URL to a JSON sidecar file.

    Indices are held in sets for O(1) membership and dedup (matters on long
    novels — a list-backed version is O(n) per check and O(n^2) to build the
    resume set). Writes are NOT automatic: callers mark with save=False in hot
    loops and call save() at checkpoints, so the file isn't rewritten on every
    single chapter. Trade-off: a crash loses progress since the last checkpoint
    and those chapters are re-scraped on resume.
    """

    def __init__(self, path: str):
        self.path = path
        data = self._load()
        self._done = set(data.get("done", []))
        self._failed = set(data.get("failed", []))
        self._last_url = data.get("last_url", "")

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"done": [], "failed": [], "last_url": ""}

    def save(self):
        """Writes the current state to disk. Call at checkpoints, not per chapter."""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "done": sorted(self._done),
                    "failed": sorted(self._failed),
                    "last_url": self._last_url,
                },
                f,
                indent=2,
            )

    def mark_done(self, idx: int, url: str = "", save: bool = True):
        self._done.add(idx)
        self._failed.discard(idx)  # a retried chapter that now succeeds
        if url:
            self._last_url = url
        if save:
            self.save()

    def mark_failed(self, idx: int, save: bool = True):
        if idx not in self._done:
            self._failed.add(idx)
        if save:
            self.save()

    def is_done(self, idx: int) -> bool:
        return idx in self._done

    @property
    def done_count(self) -> int:
        return len(self._done)

    @property
    def last_url(self) -> str:
        return self._last_url

    @property
    def failed(self) -> list:
        return sorted(self._failed)


# ── Sequential scraping ───────────────────────────────────────────────────────

def scrape_url_list(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int = 1,
    delay: float = 2.0,
):
    """
    Scrapes a known list of chapter URLs one at a time (workers=1).
    Tries requests first, falls back to a shared Playwright page.
    Supports resume via progress_file.
    """
    progress = Progress(progress_file)
    resume = progress.done_count > 0 and os.path.exists(output_file)

    if resume:
        print(f"Resuming — {progress.done_count} chapters already done, skipping them.")

    with sync_playwright() as p:
        browser, ctx = make_context(p)
        page = ctx.new_page()

        json_bucket: list = []
        page.on("response", _make_json_listener(json_bucket))

        file_mode = "a" if resume else "w"
        processed = 0
        with open(output_file, file_mode, encoding="utf-8") as f:
            for i, url in enumerate(urls, start=start_num):
                if progress.is_done(i):
                    print(f"  Ch {i} — skipping (already done)")
                    continue

                print(f"Ch {i} → {url}")
                text = fetch_via_requests(url)

                if not text:
                    json_bucket.clear()
                    try:
                        text = fetch_text_from_page(page, url, json_bucket)
                    except Exception as e:
                        print(f"  ERR: {e}")
                        progress.mark_failed(i, save=False)
                        continue

                if text:
                    f.write(f"\n\n=== CHAPTER {i} ===\n\n{text}")
                    f.flush()
                    progress.mark_done(i, save=False)
                    print(f"  ✓ {len(text)} chars")
                else:
                    progress.mark_failed(i, save=False)
                    print(f"  ✗ empty — marked failed")

                # Checkpoint progress periodically rather than on every chapter
                processed += 1
                if processed % _PROGRESS_CHECKPOINT == 0:
                    progress.save()

                time.sleep(delay + random.uniform(0, delay * 0.4))

        progress.save()  # final checkpoint
        page.close()
        browser.close()

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone → {output_file}")


def scrape_follow_links(
    start_url: str,
    output_file: str,
    progress_file: str,
    delay: float = 2.0,
    max_chapters: int = 5000,
):
    """
    Scrapes chapters by following 'next chapter' DOM links.
    Always uses Playwright. Scraping and link discovery happen in the same
    page visit — no double loading. Supports resume via progress_file.
    """
    progress = Progress(progress_file)
    resume = progress.done_count > 0 and os.path.exists(output_file)

    if resume and progress.last_url:
        print(f"Resuming from chapter {progress.done_count + 1}...")
        seed_url = progress.last_url
        chapter_num = progress.done_count + 1
        advance_first = True
    else:
        seed_url = start_url
        chapter_num = 1
        advance_first = False

    with sync_playwright() as p:
        browser, ctx = make_context(p)
        page = ctx.new_page()

        json_bucket: list = []
        page.on("response", _make_json_listener(json_bucket))

        current_url = seed_url

        if advance_first:
            print("Finding resume point from last completed page...")
            try:
                page.goto(current_url, wait_until="networkidle", timeout=30000)
                next_href = find_next_link(page)
                if not next_href:
                    print("Cannot resume: no next link found on last completed page.")
                    browser.close()
                    return
                parsed = urlparse(current_url)
                base = f"{parsed.scheme}://{parsed.netloc}"
                current_url = (
                    next_href if next_href.startswith("http") else base + next_href
                )
                print(f"Resuming at: {current_url}")
            except Exception as e:
                print(f"Failed to find resume point: {e}")
                browser.close()
                return

        file_mode = "a" if resume else "w"
        with open(output_file, file_mode, encoding="utf-8") as f:
            while chapter_num <= max_chapters:
                print(f"Ch {chapter_num} → {current_url}")
                json_bucket.clear()

                try:
                    text = fetch_text_from_page(page, current_url, json_bucket)
                except Exception as e:
                    print(f"  ERR: {e}")
                    progress.mark_failed(chapter_num)
                    break

                if text:
                    f.write(f"\n\n=== CHAPTER {chapter_num} ===\n\n{text}")
                    f.flush()
                    progress.mark_done(chapter_num, current_url)
                    print(f"  ✓ {len(text)} chars")
                else:
                    progress.mark_failed(chapter_num)
                    print(f"  ✗ empty — marked failed")

                next_href = find_next_link(page)
                if not next_href:
                    print("No next chapter link found — reached the end.")
                    break

                parsed = urlparse(current_url)
                base = f"{parsed.scheme}://{parsed.netloc}"
                next_url = (
                    next_href if next_href.startswith("http") else base + next_href
                )

                if next_url == current_url:
                    print("Next link points to same page — stopping.")
                    break

                chapter_num += 1
                current_url = next_url
                time.sleep(delay + random.uniform(0, delay * 0.4))

        page.close()
        browser.close()

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone → {output_file}")


# ── Async parallel scraping ───────────────────────────────────────────────────

async def _scrape_one_async(sem, ctx, url: str, chapter_num: int, delay: float) -> tuple:
    """
    Scrapes one chapter. The semaphore slot is held only for the actual fetch;
    the politeness delay runs after the slot is released so a sleeping worker
    doesn't block another chapter from starting.
    Returns (chapter_num, text).
    """
    async with sem:
        text = await asyncio.to_thread(fetch_via_requests, url)

        if not text:
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector(
                        _CONTENT_WAIT, state="attached", timeout=_CONTENT_WAIT_TIMEOUT
                    )
                except Exception:
                    pass  # fall through to density extraction

                for sel in SELECTORS_TEXT:
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            t = (await loc.inner_text(timeout=5000)).strip()
                            if t:
                                text = t
                                break
                    except Exception:
                        continue

                if not text:
                    html = await page.content()
                    text = extract_content_from_html(html)
            except Exception as e:
                print(f"  ERR Ch {chapter_num}: {e}")
                text = ""
            finally:
                await page.close()

        print(f"  {'✓' if text else '✗'} Ch {chapter_num} — {len(text)} chars")

    # Slot released — sleep without occupying a concurrency slot
    await asyncio.sleep(delay + random.uniform(0, delay * 0.4))
    return chapter_num, text


async def _run_parallel(urls, output_file, progress_file, start_num, delay, workers):
    from playwright.async_api import async_playwright as _async_pw

    progress = Progress(progress_file)
    resume = progress.done_count > 0 and os.path.exists(output_file)

    if resume:
        print(f"Resuming — {progress.done_count} chapters already done.")

    pending = [
        (i, url) for i, url in enumerate(urls, start=start_num)
        if not progress.is_done(i)
    ]

    if not pending:
        print("All chapters already done.")
        return

    print(f"Scraping {len(pending)} chapters with {workers} parallel workers...")

    async def _block_route(route):
        if route.request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    async with _async_pw() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(user_agent=USER_AGENT)
        await ctx.route("**/*", _block_route)  # block images/media/fonts
        sem = asyncio.Semaphore(workers)

        file_mode = "a" if resume else "w"
        with open(output_file, file_mode, encoding="utf-8") as f:
            for batch_start in range(0, len(pending), _WRITE_BATCH):
                batch = pending[batch_start : batch_start + _WRITE_BATCH]
                tasks = [
                    _scrape_one_async(sem, ctx, url, i, delay) for i, url in batch
                ]
                raw = await asyncio.gather(*tasks, return_exceptions=True)

                # Sort by chapter number — results arrive out of order
                for result in sorted(
                    [r for r in raw if isinstance(r, tuple)], key=lambda x: x[0]
                ):
                    num, text = result
                    if text:
                        f.write(f"\n\n=== CHAPTER {num} ===\n\n{text}")
                        f.flush()
                        progress.mark_done(num, save=False)
                    else:
                        progress.mark_failed(num, save=False)

                progress.save()  # one checkpoint per batch, not per chapter
                done_so_far = min(batch_start + _WRITE_BATCH, len(pending))
                print(f"  [{done_so_far}/{len(pending)}] chapters written.")

        await ctx.close()
        await browser.close()

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone → {output_file}")


def scrape_url_list_async(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int = 1,
    delay: float = 0.5,
    workers: int = 3,
):
    """
    Parallel version of scrape_url_list using asyncio + async Playwright.

    Up to `workers` chapters are fetched concurrently. Each worker tries plain
    HTTP first (non-blocking via asyncio.to_thread), then opens a Playwright
    page as fallback. Results are sorted by chapter number before writing so
    the output file is always in correct order. Processed in batches of
    _WRITE_BATCH to keep memory use flat regardless of novel length.

    Args:
        urls:          Ordered list of chapter URLs.
        output_file:   Path to the output .txt file.
        progress_file: Path to the JSON progress file.
        start_num:     Chapter number label for urls[0].
        delay:         Per-worker sleep after each chapter. Default 0.5 s —
                       lower than sequential 2.0 s since concurrency spreads load.
        workers:       Max concurrent chapters. 3–5 is a safe starting range.
    """
    asyncio.run(_run_parallel(urls, output_file, progress_file, start_num, delay, workers))
