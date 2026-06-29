"""
sites/novellive.py — novellive.app scraper (Cloudflare-protected).

novellive.app sits behind Cloudflare's interactive "Just a moment..." challenge,
which vanilla Playwright can't pass — it loops forever, reloading the page
instead of ever letting the real site load. This module uses core.cloudflare
(patchright + a persistent context) to clear the challenge once, then follows
'Next Chapter' links, reusing the cf_clearance cookie for every chapter in the
same browser session so only the first page pays the challenge cost.

Page shape (verified):
  - chapter text  : div.m-read div.txt   (~70 <p> tags per chapter)
  - next chapter  : <a> whose text is "Next Chapter", carrying an absolute href

Two modes:
  [next] Follow 'Next Chapter' links from a start URL. Always sequential — you
         can't know chapter N+1's URL until chapter N is loaded.
  [toc]  Harvest the full chapter list from the paginated book page, then scrape
         in PARALLEL. Safe under Cloudflare because every tab in one persistent
         context shares the cf_clearance cookie: one tab solves the challenge and
         the rest ride free.

CLI usage:
    python main.py novellive --url CH1_URL  --name NAME --mode next
                             [--delay 1.5] [--max 5000] [--epub] [--headless]

    python main.py novellive --url BOOK_URL --name NAME --mode toc
                             [--range 1-200] [--workers 3] [--delay 0.5]
                             [--epub] [--headless]
"""
import asyncio
import os
import random
import re
import time
from urllib.parse import urlparse

from core.cloudflare import (
    cf_context,
    cf_context_async,
    wait_for_cloudflare,
    wait_for_cloudflare_async,
)
from core.epub_writer import txt_to_epub
from core.scraper import Progress, extract_content_from_html, _order_chapters

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_BASE = os.path.join(BASE_DIR, "output")
# Persistent browser profile — keeps the cf_clearance cookie between runs.
PROFILE_DIR = os.path.join(BASE_DIR, ".cf_profile")

CONTENT_SELECTOR = "div.m-read div.txt"
NEXT_SELECTOR = "a:has-text('Next Chapter')"


# ── Per-page helpers ──────────────────────────────────────────────────────────

def _extract_text(page) -> str:
    """Returns clean chapter text from div.m-read div.txt, with a density fallback."""
    try:
        loc = page.locator(CONTENT_SELECTOR).first
        if loc.count() > 0:
            text = loc.inner_text(timeout=5000).lstrip("﻿").strip()
            if len(text) > 200:
                return text
    except Exception:
        pass
    # Fallback: density-score the whole reading column if the .txt child moved.
    try:
        html = page.locator("div.m-read").first.inner_html(timeout=5000)
        return extract_content_from_html(html)
    except Exception:
        return ""


def _next_url(page, current_url: str) -> str:
    """Returns the absolute 'Next Chapter' URL, or '' at the end of the book."""
    try:
        loc = page.locator(NEXT_SELECTOR).first
        if loc.count() == 0:
            return ""
        href = (loc.get_attribute("href") or "").strip()
        # On the last chapter the button reloads the page instead of advancing.
        if not href or href.startswith("javascript:"):
            return ""
        if href.startswith("http"):
            return href
        parsed = urlparse(current_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base + (href if href.startswith("/") else "/" + href)
    except Exception:
        return ""


# ── TOC harvesting (for parallel mode) ────────────────────────────────────────

_WRITE_BATCH = 30  # chapters gathered before each disk write in parallel mode


def _book_root(url: str) -> str:
    """Normalises any chapter/paginated URL down to https://host/book/<slug>."""
    m = re.match(r"(https?://[^/]+/book/[^/]+)", url)
    return m.group(1) if m else url.rstrip("/")


def _collect_toc_urls(url: str, headless: bool = False) -> list:
    """
    Returns every chapter URL for a book, oldest-first.

    novellive paginates its chapter list at /book/<slug>/<page>. We read the
    highest page number from the pagination links, walk every page collecting
    chapter hrefs, then order them by chapter number. The cf_clearance cookie is
    shared across pages, so only the first page pays the Cloudflare cost.
    """
    book_root = _book_root(url)
    seen, urls = set(), []

    with cf_context(PROFILE_DIR, headless=headless) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto(book_root, wait_until="domcontentloaded", timeout=60000)
        if not wait_for_cloudflare(page, "a[href*=chapter]"):
            print("Cloudflare did not clear on the book page.")
            return []

        # Highest page number among /book/<slug>/<n> pagination links.
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []
        pages = [
            int(m.group(1))
            for h in hrefs
            if (m := re.search(r"/book/[^/]+/(\d+)$", h or ""))
        ]
        max_page = max(pages) if pages else 1
        print(f"Chapter list spans {max_page} page(s).")

        for pg in range(1, max_page + 1):
            if pg > 1:
                page.goto(f"{book_root}/{pg}", wait_until="domcontentloaded", timeout=60000)
                if not wait_for_cloudflare(page, "a[href*=chapter]"):
                    print(f"  page {pg}: Cloudflare did not clear — skipping")
                    continue
            try:
                chapter_hrefs = page.eval_on_selector_all(
                    "a[href*=chapter]", "els => els.map(e => e.href)"
                )
            except Exception:
                chapter_hrefs = []
            added = 0
            for h in chapter_hrefs:
                if h and re.search(r"/chapter-\d+", h) and h not in seen:
                    seen.add(h)
                    urls.append(h)
                    added += 1
            print(f"  page {pg}/{max_page}: +{added} ({len(urls)} total)")

    return _order_chapters(urls)


# ── Parallel scraping (TOC mode) ──────────────────────────────────────────────

async def _extract_text_async(page) -> str:
    """Async chapter-text extraction (mirror of _extract_text)."""
    try:
        loc = page.locator(CONTENT_SELECTOR).first
        if await loc.count() > 0:
            text = (await loc.inner_text(timeout=5000)).lstrip("﻿").strip()
            if len(text) > 200:
                return text
    except Exception:
        pass
    try:
        html = await page.locator("div.m-read").first.inner_html(timeout=5000)
        return extract_content_from_html(html)
    except Exception:
        return ""


async def _scrape_one_async(sem, ctx, url: str, num: int, delay: float) -> tuple:
    """Scrapes one chapter in its own tab. Returns (num, text)."""
    async with sem:
        page = await ctx.new_page()
        text = ""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if await wait_for_cloudflare_async(page, CONTENT_SELECTOR):
                text = await _extract_text_async(page)
        except Exception as e:
            print(f"  ERR Ch {num}: {e}")
        finally:
            await page.close()
        print(f"  {'✓' if text else '✗'} Ch {num} — {len(text)} chars")
    # Politeness delay runs after the slot is released.
    await asyncio.sleep(delay + random.uniform(0, delay * 0.4))
    return num, text


async def _run_parallel(urls, output_file, progress_file, start_num, delay, workers, headless):
    progress = Progress(progress_file)
    resume = progress.done_count > 0 and os.path.exists(output_file)
    if resume:
        print(f"Resuming — {progress.done_count} chapters already done.")

    pending = [
        (i, u) for i, u in enumerate(urls, start=start_num) if not progress.is_done(i)
    ]
    if not pending:
        print("All chapters already done.")
        return

    print(f"Scraping {len(pending)} chapters with {workers} parallel worker(s)...")

    async with cf_context_async(PROFILE_DIR, headless=headless) as ctx:
        # Warm-up: solve Cloudflare ONCE before fanning out, so workers don't all
        # hit the challenge at the same time (which looks like a bot attack).
        warm = await ctx.new_page()
        try:
            await warm.goto(pending[0][1], wait_until="domcontentloaded", timeout=60000)
            if not await wait_for_cloudflare_async(warm, CONTENT_SELECTOR):
                print("Cloudflare did not clear during warm-up — aborting.")
                return
        finally:
            await warm.close()
        print("Cloudflare cleared — cookie shared across workers.")

        sem = asyncio.Semaphore(workers)
        file_mode = "a" if resume else "w"
        with open(output_file, file_mode, encoding="utf-8") as f:
            for batch_start in range(0, len(pending), _WRITE_BATCH):
                batch = pending[batch_start : batch_start + _WRITE_BATCH]
                tasks = [_scrape_one_async(sem, ctx, u, i, delay) for i, u in batch]
                raw = await asyncio.gather(*tasks, return_exceptions=True)

                for num, text in sorted(
                    [r for r in raw if isinstance(r, tuple)], key=lambda x: x[0]
                ):
                    if text:
                        f.write(f"\n\n=== CHAPTER {num} ===\n\n{text}")
                        f.flush()
                        progress.mark_done(num, save=False)
                    else:
                        progress.mark_failed(num, save=False)

                progress.save()
                done = min(batch_start + _WRITE_BATCH, len(pending))
                print(f"  [{done}/{len(pending)}] chapters written.")

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone → {output_file}")


def scrape_toc_parallel(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int = 1,
    delay: float = 0.5,
    workers: int = 3,
    headless: bool = False,
):
    """Parallel scrape of a known chapter-URL list, sharing one Cloudflare cookie."""
    asyncio.run(
        _run_parallel(urls, output_file, progress_file, start_num, delay, workers, headless)
    )


# ── Core scrape loop (next-links mode) ────────────────────────────────────────

def scrape(
    start_url: str,
    output_file: str,
    progress_file: str,
    delay: float = 1.5,
    max_chapters: int = 5000,
    headless: bool = False,
):
    """
    Follows 'Next Chapter' links from start_url, clearing Cloudflare on each
    page, and appends each chapter to output_file. Resumable via progress_file.
    """
    progress = Progress(progress_file)
    resume = (
        progress.done_count > 0
        and os.path.exists(output_file)
        and bool(progress.last_url)
    )

    if resume:
        current_url = progress.last_url   # last COMPLETED chapter — advance past it
        chapter_num = progress.done_count + 1
        advance_first = True
        print(f"Resuming from chapter {chapter_num}...")
    else:
        current_url = start_url
        chapter_num = 1
        advance_first = False

    with cf_context(PROFILE_DIR, headless=headless) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if advance_first:
            print("Finding resume point from last completed chapter...")
            page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
            if not wait_for_cloudflare(page, CONTENT_SELECTOR):
                print("Cloudflare did not clear on the resume page — aborting.")
                return
            nxt = _next_url(page, current_url)
            if not nxt:
                print("No next link on last completed chapter — nothing to resume.")
                return
            current_url = nxt

        file_mode = "a" if resume else "w"
        with open(output_file, file_mode, encoding="utf-8") as f:
            while chapter_num <= max_chapters:
                print(f"Ch {chapter_num} → {current_url}")
                try:
                    page.goto(current_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    print(f"  ERR goto: {e}")
                    progress.mark_failed(chapter_num)
                    break

                if not wait_for_cloudflare(page, CONTENT_SELECTOR):
                    print("  ✗ Cloudflare did not clear — stopping.")
                    progress.mark_failed(chapter_num)
                    break

                text = _extract_text(page)
                if text:
                    f.write(f"\n\n=== CHAPTER {chapter_num} ===\n\n{text}")
                    f.flush()
                    progress.mark_done(chapter_num, current_url)
                    print(f"  ✓ {len(text)} chars")
                else:
                    progress.mark_failed(chapter_num)
                    print("  ✗ empty — marked failed")

                nxt = _next_url(page, current_url)
                if not nxt or nxt == current_url:
                    print("No next chapter link found — reached the end.")
                    break

                chapter_num += 1
                current_url = nxt
                time.sleep(delay)

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone → {output_file}")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _parse_range(range_str: str, total: int) -> tuple:
    """Returns (start, end) 1-based inclusive bounds from a '1-50' string."""
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
    print("\nNovelLive (novellive.app) — Cloudflare-protected (uses patchright)")
    print("  [1] Follow 'Next Chapter' links  (start from a chapter URL, sequential)")
    print("  [2] Table of Contents  (start from the book URL, parallel scraping)")
    mode = input("Select mode (1/2): ").strip()
    if mode not in ("1", "2"):
        print("Invalid mode. Enter 1 or 2.")
        return

    url = input("URL: ").strip()
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

    if mode == "1":
        delay_raw = input("Delay between chapters in seconds (press Enter for 1.5): ").strip()
        delay = float(delay_raw) if delay_raw else 1.5
        print(f"\nMode: follow next-chapter links | Output: {output_txt}\n")
        scrape(url, output_txt, progress_file, delay=delay)
    else:
        print("\nFetching chapter list from the book page...")
        urls = _collect_toc_urls(url)
        if not urls:
            print("No chapters found. Use mode [1] (follow next links) instead.")
            return
        print(f"Found {len(urls)} chapters.")

        range_raw = input(f"Enter range (e.g. 1-50) or Enter for ALL {len(urls)}: ").strip()
        start, end = _parse_range(range_raw, len(urls))
        urls = urls[start - 1 : end]
        if not urls:
            print("No URLs in selected range.")
            return

        workers_raw = input("Parallel workers (Enter for 3; keep ≤5 to stay under Cloudflare): ").strip()
        try:
            workers = max(1, int(workers_raw)) if workers_raw else 3
        except ValueError:
            workers = 3
        delay_raw = input("Delay per worker in seconds (press Enter for 0.5): ").strip()
        delay = float(delay_raw) if delay_raw else 0.5

        print(f"\nScraping {len(urls)} chapters with {workers} worker(s) → {output_txt}\n")
        scrape_toc_parallel(
            urls, output_txt, progress_file, start_num=start, delay=delay, workers=workers
        )

    if os.path.exists(output_txt):
        if input("\nConvert to EPUB? (y/n): ").strip().lower() == "y":
            txt_to_epub(output_txt, os.path.join(novel_dir, f"{novel_name}.epub"))


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args):
    """Entry point called by main.py when CLI args are provided."""
    novel_name = args.name
    novel_dir = os.path.join(OUTPUT_BASE, novel_name)
    os.makedirs(novel_dir, exist_ok=True)
    output_txt = os.path.join(novel_dir, f"{novel_name}.txt")
    progress_file = os.path.join(novel_dir, f"{novel_name}.progress.json")

    mode = getattr(args, "mode", "next")
    headless = getattr(args, "headless", False)

    if mode == "toc":
        print("Fetching chapter list from the book page...")
        urls = _collect_toc_urls(args.url, headless=headless)
        if not urls:
            print("No chapters found. Try --mode next instead.")
            return
        print(f"Found {len(urls)} chapters.")
        start, end = _parse_range(getattr(args, "chapter_range", None) or "", len(urls))
        urls = urls[start - 1 : end]
        if not urls:
            print("No URLs in selected range.")
            return

        workers = getattr(args, "workers", 3)
        print(f"Scraping {len(urls)} chapters with {workers} worker(s) → {output_txt}\n")
        scrape_toc_parallel(
            urls, output_txt, progress_file,
            start_num=start,
            delay=getattr(args, "delay", 0.5),
            workers=workers,
            headless=headless,
        )
    else:
        print(f"Mode: follow next-chapter links | Output: {output_txt}\n")
        scrape(
            args.url, output_txt, progress_file,
            delay=getattr(args, "delay", 1.5),
            max_chapters=getattr(args, "max_chapters", 5000),
            headless=headless,
        )

    if getattr(args, "epub", False) and os.path.exists(output_txt):
        txt_to_epub(output_txt, os.path.join(novel_dir, f"{novel_name}.epub"))
