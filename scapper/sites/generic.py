"""
sites/generic.py — Generic novel scraper (menu option 5).

Works on most websites without site-specific configuration.

Two modes:
  [1] Next-links — start at chapter 1 URL, follow 'next chapter' links
                   automatically. Sequential only (can't parallelise link
                   discovery without visiting each page twice).
  [2] TOC        — provide a table-of-contents page; engine collects all
                   chapter links then scrapes them in parallel.

CLI usage:
    python main.py generic --url URL --name NAME --mode toc
                           [--range 1-100] [--workers 3] [--delay 0.5] [--epub]

    python main.py generic --url URL --name NAME --mode next [--delay 2.0]
"""
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from core.browser import make_context
from core.epub_writer import txt_to_epub
from core.scraper import (
    auto_detect_chapter_urls,
    find_chapter_links,
    scrape_follow_links,
    scrape_url_list,
    scrape_url_list_async,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_BASE = os.path.join(BASE_DIR, "output")


def _collect_toc_urls(toc_url: str) -> list:
    """
    Loads the TOC page and returns all chapter URLs.

    Primary: auto_detect_chapter_urls — scores intercepted responses + DOM and
    picks the best chapter-list candidate (self-healing across redesigns).
    Fallback: find_chapter_links — fixed href-pattern selectors, in case the
    page renders nothing interceptable.
    """
    parsed = urlparse(toc_url)
    base_domain = f"{parsed.scheme}://{parsed.netloc}"

    with sync_playwright() as p:
        browser, ctx = make_context(p, block_resources=False)
        page = ctx.new_page()
        urls = auto_detect_chapter_urls(page, toc_url)
        if not urls:
            print("Auto-detect found nothing — trying fixed selectors...")
            urls = find_chapter_links(page, base_domain)
        page.close()
        browser.close()

    return urls


def _parse_range(range_str: str, total: int) -> tuple:
    if not range_str:
        return 1, total
    try:
        s, e = range_str.split("-")
        return int(s.strip()), int(e.strip())
    except (ValueError, AttributeError):
        print("Invalid range format. Using all chapters.")
        return 1, total


# ── Interactive entry point ───────────────────────────────────────────────────

def run():
    """Entry point called by main.py (interactive mode)."""
    print("\nGeneric Scraper — works on most novel websites")
    print("  [1] Follow 'next chapter' links  (start from chapter 1 URL)")
    print("  [2] Table of Contents / index page  (parallel scraping available)")
    mode = input("Select mode (1/2): ").strip()

    if mode not in ("1", "2"):
        print("Invalid mode. Enter 1 or 2.")
        return

    url = input("Enter URL: ").strip()
    if not url.startswith("http"):
        print("URL must start with http or https.")
        return

    novel_name = input("Novel name (used for folder and file names): ").strip()
    if not novel_name:
        print("Novel name cannot be empty.")
        return

    novel_dir = os.path.join(OUTPUT_BASE, novel_name)
    os.makedirs(novel_dir, exist_ok=True)
    output_txt = os.path.join(novel_dir, f"{novel_name}.txt")
    progress_file = os.path.join(novel_dir, f"{novel_name}.progress.json")

    # ── Mode 1: follow next-chapter links (always sequential) ────────────────
    if mode == "1":
        delay_raw = input("Delay between chapters in seconds (press Enter for 2.0): ").strip()
        delay = float(delay_raw) if delay_raw else 2.0

        print(f"\nMode: follow next-chapter links")
        print(f"Output: {output_txt}\n")
        scrape_follow_links(url, output_txt, progress_file, delay=delay)

    # ── Mode 2: table of contents (parallel available) ────────────────────────
    else:
        print("\nFetching chapter list from TOC page...")
        urls = _collect_toc_urls(url)

        if not urls:
            print("No chapter links found on that page.")
            print(
                "Tip: the scraper looks for href patterns containing 'chapter', "
                "'/ch-', '/c-', or '/ep-'. Try mode [1] (follow next links) instead."
            )
            return

        print(f"Found {len(urls)} chapters.")

        range_raw = input(
            f"Enter range (e.g. 1-50) or press Enter for ALL {len(urls)}: "
        ).strip()
        start, end = _parse_range(range_raw, len(urls))
        urls = urls[start - 1 : end]

        if not urls:
            print("No URLs in selected range.")
            return

        workers_raw = input(
            "Parallel workers (1 = sequential, 3 = fast, 5 = fastest): "
        ).strip()
        try:
            workers = max(1, int(workers_raw)) if workers_raw else 3
        except ValueError:
            workers = 3

        delay_raw = input(
            f"Delay per worker in seconds (press Enter for {'0.5' if workers > 1 else '2.0'}): "
        ).strip()
        delay = float(delay_raw) if delay_raw else (0.5 if workers > 1 else 2.0)

        print(f"\nScraping {len(urls)} chapters with {workers} worker(s) → {output_txt}\n")

        if workers > 1:
            scrape_url_list_async(
                urls, output_txt, progress_file,
                start_num=start, delay=delay, workers=workers,
            )
        else:
            scrape_url_list(
                urls, output_txt, progress_file,
                start_num=start, delay=delay,
            )

    if os.path.exists(output_txt):
        if input("\nConvert to EPUB? (y/n): ").strip().lower() == "y":
            txt_to_epub(output_txt, os.path.join(novel_dir, f"{novel_name}.epub"))


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args):
    """Entry point called by main.py when CLI args are provided."""
    url = args.url
    novel_name = args.name
    mode = getattr(args, "mode", "toc")

    novel_dir = os.path.join(OUTPUT_BASE, novel_name)
    os.makedirs(novel_dir, exist_ok=True)
    output_txt = os.path.join(novel_dir, f"{novel_name}.txt")
    progress_file = os.path.join(novel_dir, f"{novel_name}.progress.json")

    workers = getattr(args, "workers", 3)
    delay = getattr(args, "delay", 0.5 if workers > 1 else 2.0)
    epub = getattr(args, "epub", False)

    if mode == "next":
        print(f"Mode: follow next-chapter links | Output: {output_txt}\n")
        scrape_follow_links(url, output_txt, progress_file, delay=delay)

    else:
        print("Fetching chapter list from TOC page...")
        urls = _collect_toc_urls(url)

        if not urls:
            print("No chapter links found. Try --mode next instead.")
            return

        print(f"Found {len(urls)} chapters.")
        range_str = getattr(args, "chapter_range", None) or ""
        start, end = _parse_range(range_str, len(urls))
        urls = urls[start - 1 : end]

        if not urls:
            print("No URLs in selected range.")
            return

        print(f"Scraping {len(urls)} chapters with {workers} worker(s)...\n")

        if workers > 1:
            scrape_url_list_async(
                urls, output_txt, progress_file,
                start_num=start, delay=delay, workers=workers,
            )
        else:
            scrape_url_list(
                urls, output_txt, progress_file,
                start_num=start, delay=delay,
            )

    if epub and os.path.exists(output_txt):
        txt_to_epub(output_txt, os.path.join(novel_dir, f"{novel_name}.epub"))
