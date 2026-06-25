"""
sites/novelarrow.py — Novelarrow chapter scraper.

Fetches chapter URLs from a contents page, scrapes chapters in parallel
(async) or sequentially, saves to .txt, optionally converts to .epub.

CLI usage:
    python main.py novelarrow --url URL --name NAME [--range 1-100]
                              [--workers 3] [--delay 0.5] [--epub]
                              [--output-dir /path]
"""
import os
import re
import time
from urllib.parse import urlparse

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


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def _fetch_chapter_urls(page, base: str) -> list:
    """
    Navigates to the novel's contents page, optionally expands the chapter list,
    then returns all /chapter/ hrefs deduplicated and oldest-first.
    """
    page.goto(base, wait_until="networkidle", timeout=30000)
    time.sleep(2)

    try:
        page.locator("button:has-text('Chapters')").first.click(timeout=3000)
        time.sleep(2)
    except Exception:
        pass

    links = page.locator("a[href*='/chapter/']").all()
    all_hrefs = [l.get_attribute("href") for l in links if l.get_attribute("href")]

    urls: list = []
    seen: set = set()
    for href in reversed(all_hrefs):
        if href not in seen:
            seen.add(href)
            urls.append(href)
    urls.reverse()

    # Self-healing fallback if the /chapter/ harvest comes up empty
    if not urls:
        print("[Cache] Fixed selector found nothing — trying auto-detection...")
        urls = auto_detect_chapter_urls(page, base)

    return urls


def _get_urls(
    contents_url: str,
    cache: NovelCache,
    cache_data: dict,
    allow_refresh: bool = True,
) -> tuple:
    """
    Returns (urls, cached_len) — fetches from site if not cached.
    When allow_refresh is False, skips the refresh prompt (used in CLI mode).
    """
    cached = cache_data.get(contents_url, [])
    cached_len = len(cached)

    if cached_len == 0:
        print("\n[Cache] Novel not found. Fetching chapter list from the web...")
        with sync_playwright() as p:
            browser, ctx = make_context(p, block_resources=False)
            nav = ctx.new_page()
            all_urls = _fetch_chapter_urls(nav, contents_url)
            nav.close()
            browser.close()

        if not all_urls:
            print("Error: No chapters found on the website.")
            return [], 0

        cache_data[contents_url] = all_urls
        cache.save(cache_data)
        return all_urls, len(all_urls)

    print(f"\n[Cache] Found {cached_len} chapters saved for this novel.")
    return cached, cached_len


def _maybe_refresh(
    contents_url: str,
    cached: list,
    cached_len: int,
    cache: NovelCache,
    cache_data: dict,
) -> tuple:
    """Checks for new chapters on the web and updates cache if found."""
    print("\nChecking for new chapters...")
    with sync_playwright() as p:
        browser, ctx = make_context(p, block_resources=False)
        nav = ctx.new_page()
        all_urls = _fetch_chapter_urls(nav, contents_url)
        nav.close()
        browser.close()

    if len(all_urls) > cached_len:
        new_count = len(all_urls) - cached_len
        cache_data[contents_url] = all_urls
        cache.save(cache_data)
        print(f"Cache updated. {new_count} new chapters. Total: {len(all_urls)}.")
        return all_urls, len(all_urls)

    print("No new chapters found. Cache is up to date.")
    return cached, cached_len


def _parse_range(range_str: str, total: int) -> tuple:
    """Returns (start_1based, end_1based) from a '1-100' string."""
    if not range_str:
        return 1, total
    try:
        s, e = range_str.split("-")
        return int(s.strip()), int(e.strip())
    except (ValueError, AttributeError):
        print(f"Invalid range format. Using all {total} chapters.")
        return 1, total


def _scrape(
    cached_urls: list,
    base_domain: str,
    safe_name: str,
    novel_folder: str,
    start_idx: int,
    end_idx: int,
    workers: int,
    delay: float,
    epub: bool,
):
    """Shared scrape logic used by both run() and run_cli()."""
    output_txt = os.path.join(novel_folder, f"{safe_name}.txt")
    progress_file = os.path.join(novel_folder, f"{safe_name}.progress.json")

    target_hrefs = cached_urls[start_idx - 1 : end_idx]
    full_urls = [
        h if h.startswith("http") else base_domain + h for h in target_hrefs
    ]

    if not full_urls:
        print("No chapters in selected range.")
        return

    print(f"\nScraping chapters {start_idx} → {end_idx} with {workers} worker(s)...")

    if workers > 1:
        scrape_url_list_async(
            full_urls, output_txt, progress_file,
            start_num=start_idx, delay=delay, workers=workers,
        )
    else:
        scrape_url_list(
            full_urls, output_txt, progress_file,
            start_num=start_idx, delay=delay,
        )

    if epub:
        output_epub = os.path.join(novel_folder, f"{safe_name}.epub")
        txt_to_epub(output_txt, output_epub)


# ── Interactive entry point ───────────────────────────────────────────────────

def run():
    """Entry point called by main.py (interactive mode)."""
    contents_url = input("Contents page URL: ").strip()
    novel_name = input("Novel name (used for folder & file): ").strip()
    base_dir = input(
        "Base save directory (press Enter to use default output/ folder): "
    ).strip()

    safe_name = _sanitize(novel_name)
    novel_folder = (
        os.path.join(OUTPUT_BASE, safe_name)
        if not base_dir
        else os.path.join(base_dir, "output", safe_name)
    )
    os.makedirs(novel_folder, exist_ok=True)
    print(f"\n[Info] Files will be saved to: {novel_folder}")

    parsed = urlparse(contents_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"

    cache = NovelCache(CACHE_PATH)
    cache_data = cache.load()
    cached_urls, cached_len = _get_urls(contents_url, cache, cache_data)

    if not cached_urls:
        return

    range_input = input(
        f"Enter range (e.g., 1-{cached_len}) or press Enter for all: "
    ).strip()

    start_idx, end_idx = 1, cached_len
    needs_refresh = False

    if range_input:
        start_idx, end_idx = _parse_range(range_input, cached_len)
        if end_idx > cached_len:
            needs_refresh = True
    else:
        if input("Check the web for newly released chapters? (y/n): ").strip().lower() == "y":
            needs_refresh = True

    if needs_refresh:
        cached_urls, cached_len = _maybe_refresh(
            contents_url, cached_urls, cached_len, cache, cache_data
        )
        end_idx = min(end_idx, cached_len)

    if start_idx > end_idx or start_idx < 1:
        print("Error: Invalid range bounds.")
        return

    workers_raw = input("Parallel workers (1 = sequential, 3 = fast, 5 = fastest): ").strip()
    try:
        workers = max(1, int(workers_raw)) if workers_raw else 3
    except ValueError:
        workers = 3

    delay = 0.5 if workers > 1 else 2.0
    epub = input("Convert to EPUB when done? (y/n): ").strip().lower() == "y"

    _scrape(cached_urls, base_domain, safe_name, novel_folder,
            start_idx, end_idx, workers, delay, epub)


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args):
    """Entry point called by main.py when CLI args are provided."""
    contents_url = args.url
    novel_name = args.name
    safe_name = _sanitize(novel_name)

    output_dir = getattr(args, "output_dir", None)
    novel_folder = (
        os.path.join(OUTPUT_BASE, safe_name)
        if not output_dir
        else os.path.join(output_dir, "output", safe_name)
    )
    os.makedirs(novel_folder, exist_ok=True)
    print(f"[Info] Files will be saved to: {novel_folder}")

    parsed = urlparse(contents_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"

    cache = NovelCache(CACHE_PATH)
    cache_data = cache.load()
    cached_urls, cached_len = _get_urls(contents_url, cache, cache_data, allow_refresh=False)

    if not cached_urls:
        return

    range_str = getattr(args, "chapter_range", None) or ""
    start_idx, end_idx = _parse_range(range_str, cached_len)
    end_idx = min(end_idx, cached_len)

    workers = getattr(args, "workers", 3)
    delay = getattr(args, "delay", 0.5 if workers > 1 else 2.0)
    epub = getattr(args, "epub", False)

    _scrape(cached_urls, base_domain, safe_name, novel_folder,
            start_idx, end_idx, workers, delay, epub)
