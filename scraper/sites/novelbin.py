"""
sites/novelbin.py — Novelbin chapter scraper.

Fetches the full chapter list from novelbin's AJAX archive endpoint
(/ajax/chapter-archive?novelId=<slug>), then scrapes chapters in parallel
(async) or sequentially, saves to .txt, and optionally converts to .epub.

CLI usage:
    python main.py novelbin --url URL --name NAME [--range 1-100]
                            [--workers 3] [--delay 0.5] [--epub]
"""
import os
import re
import time
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

from core.browser import make_context
from core.cache import NovelCache
from core.epub_writer import txt_to_epub
from core.scraper import (
    auto_detect_chapter_urls,
    scrape_url_list,
    scrape_url_list_async,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE_DIR, "novel_cache.json")
OUTPUT_BASE = os.path.join(BASE_DIR, "output")


def _fetch_chapter_urls(page, url: str) -> list:
    """
    Returns the full list of absolute chapter URLs (oldest first).

    novelbin removed the old chr-jump <select> dropdown, so we read the novel
    id from the page (any [data-novel-id] element — it holds the slug) and
    fetch the complete list from the AJAX archive:
        {domain}/ajax/chapter-archive?novelId={slug}
    Works whether `url` is the novel landing page or an individual chapter page.
    """
    print("Fetching chapter list from site...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    # Novel id (slug) lives in a data-novel-id attribute, e.g. on #rating
    novel_id = ""
    try:
        el = page.locator("[data-novel-id]").first
        if el.count() > 0:
            novel_id = el.get_attribute("data-novel-id") or ""
    except Exception:
        pass

    # Fallback: pull the slug straight from the /novel-book/<slug> path
    if not novel_id:
        m = re.search(r"/novel-book/([^/]+)", url)
        if m:
            novel_id = m.group(1)

    if not novel_id:
        print("  Could not determine novel id from page or URL.")
        return []

    archive_url = f"{domain}/ajax/chapter-archive?novelId={novel_id}"
    print(f"  Fetching archive: {archive_url}")

    try:
        resp = page.context.request.get(archive_url, timeout=30000)
        if not resp.ok:
            print(f"  Archive request failed: HTTP {resp.status}")
            return []
        body = resp.text()
    except Exception as e:
        print(f"  Archive request error: {e}")
        return []

    # Archive is a flat HTML list of <a href="...chapter..."> in reading order
    hrefs = re.findall(r'href="([^"]+)"', body)
    seen, urls = set(), []
    for h in hrefs:
        if "chapter" not in h.lower():
            continue
        full = h if h.startswith("http") else domain + h
        if full not in seen:
            seen.add(full)
            urls.append(full)

    # Self-healing fallback: if novelbin ever changes the archive too, fall back
    # to shape-based auto-detection instead of failing outright.
    if not urls:
        print("  Archive empty — trying auto-detection...")
        urls = auto_detect_chapter_urls(page, url)

    return urls


def _get_urls(chapter_url: str, base_url: str, cache: NovelCache, cache_data: dict) -> list:
    """
    Returns the full chapter URL list for base_url, using cache when available.
    Offers to refresh / append on cache hit.
    """
    if base_url in cache_data and cache_data[base_url]:
        cached = cache_data[base_url]
        print(f"\nFound {len(cached)} chapters in cache.")
        print("  [1] Use cached URLs")
        print("  [2] Visit site to refresh / append new chapters")
        choice = input("Select option (1/2): ").strip()

        if choice == "2":
            with sync_playwright() as p:
                browser, ctx = make_context(p, block_resources=False)
                nav = ctx.new_page()
                scraped = _fetch_chapter_urls(nav, chapter_url)
                nav.close()
                browser.close()

            existing = set(cached)
            new_urls = [u for u in scraped if u not in existing]
            cached.extend(new_urls)
            print(f"Appended {len(new_urls)} new URLs. Total: {len(cached)}.")
            cache_data[base_url] = cached
            cache.save(cache_data)

        return cached

    with sync_playwright() as p:
        browser, ctx = make_context(p, block_resources=False)
        nav = ctx.new_page()
        all_urls = _fetch_chapter_urls(nav, chapter_url)
        nav.close()
        browser.close()

    if not all_urls:
        print("No chapters found.")
        return []

    print(f"Found {len(all_urls)} unique chapters. Saving to cache.")
    cache_data[base_url] = all_urls
    cache.save(cache_data)
    return all_urls


def _parse_range(range_str: str, total: int) -> tuple:
    """Returns (start_idx, end_idx) as 0-based indices from a '10-50' string."""
    start_idx, end_idx = 0, total
    if not range_str:
        return start_idx, end_idx
    parts = range_str.split("-")
    if len(parts) == 2:
        if parts[0].strip().isdigit():
            start_idx = max(0, int(parts[0].strip()) - 1)
        if parts[1].strip().isdigit():
            end_idx = min(total, int(parts[1].strip()))
    else:
        print("Invalid range format. Using ALL chapters.")
    return start_idx, end_idx


def _scrape(
    all_urls: list,
    base_url: str,
    novel_name: str,
    novel_dir: str,
    range_str: str,
    workers: int,
    delay: float,
    epub: bool,
):
    """Shared scrape logic used by both run() and run_cli()."""
    output_file = os.path.join(novel_dir, f"{novel_name}.txt")
    progress_file = os.path.join(novel_dir, f"{novel_name}.progress.json")

    start_idx, end_idx = _parse_range(range_str, len(all_urls))
    urls_to_scrape = all_urls[start_idx:end_idx]

    if not urls_to_scrape:
        print("No chapters in selected range.")
        return

    print(
        f"\nScraping {len(urls_to_scrape)} chapters "
        f"(ch {start_idx + 1} → {end_idx}) with {workers} worker(s)..."
    )

    # Build full absolute URLs
    full_urls = [urljoin(base_url + "/", u) for u in urls_to_scrape]

    if workers > 1:
        scrape_url_list_async(
            full_urls, output_file, progress_file,
            start_num=start_idx + 1, delay=delay, workers=workers,
        )
    else:
        scrape_url_list(
            full_urls, output_file, progress_file,
            start_num=start_idx + 1, delay=delay,
        )

    if epub:
        epub_file = os.path.join(novel_dir, f"{novel_name}.epub")
        txt_to_epub(output_file, epub_file)


# ── Interactive entry point ───────────────────────────────────────────────────

def run():
    """Entry point called by main.py (interactive mode)."""
    chapter_url = input("Chapter URL (to fetch list from or start at): ").strip()
    novel_name = input("Novel name (used for folder and file names): ").strip()

    novel_dir = os.path.join(OUTPUT_BASE, novel_name)
    os.makedirs(novel_dir, exist_ok=True)

    match = re.match(r"^(https?://[^/]+/[^/]+/[^/]+)", chapter_url)
    if not match:
        print("Could not parse base URL from the provided chapter URL. Exiting.")
        return
    base_url = match.group(1)
    print(f"Detected base URL: {base_url}")

    cache = NovelCache(CACHE_PATH)
    cache_data = cache.load()
    all_urls = _get_urls(chapter_url, base_url, cache, cache_data)

    if not all_urls:
        return

    print(f"\nTotal available chapters: {len(all_urls)}")
    range_str = input(
        "Enter range (e.g. '10-50', '10-', '-50') or press Enter for ALL: "
    ).strip()

    workers_raw = input("Parallel workers (1 = sequential, 3 = fast, 5 = fastest): ").strip()
    try:
        workers = max(1, int(workers_raw)) if workers_raw else 3
    except ValueError:
        workers = 3

    delay = 0.5 if workers > 1 else 2.0

    epub = input("Convert to EPUB when done? (y/n): ").strip().lower() == "y"

    _scrape(all_urls, base_url, novel_name, novel_dir, range_str, workers, delay, epub)


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args):
    """Entry point called by main.py when CLI args are provided."""
    chapter_url = args.url
    novel_name = args.name

    novel_dir = os.path.join(OUTPUT_BASE, novel_name)
    os.makedirs(novel_dir, exist_ok=True)

    match = re.match(r"^(https?://[^/]+/[^/]+/[^/]+)", chapter_url)
    if not match:
        print("Could not parse base URL from the provided chapter URL.")
        return
    base_url = match.group(1)
    print(f"Detected base URL: {base_url}")

    cache = NovelCache(CACHE_PATH)
    cache_data = cache.load()
    all_urls = _get_urls(chapter_url, base_url, cache, cache_data)

    if not all_urls:
        return

    _scrape(
        all_urls, base_url, novel_name, novel_dir,
        range_str=getattr(args, "chapter_range", None) or "",
        workers=getattr(args, "workers", 3),
        delay=getattr(args, "delay", 0.5),
        epub=getattr(args, "epub", False),
    )
