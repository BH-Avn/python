"""
core/cf_bypass.py — Cloudflare-protected scraping via zendriver.

Playwright's bundled Chromium gets stuck in an infinite "Just a moment..."
loop on Cloudflare-protected sites, and a cf_clearance cookie earned by one
automated browser doesn't reliably transfer to a different one (verified:
a cookie solved by zendriver is rejected when replayed in Playwright, even
with a matching User-Agent — Cloudflare appears to bind trust to signals of
the solving session itself, not just IP+UA). So instead of solving-then-
handing-off, CFSession solves the challenge AND does the actual chapter
fetching within that same zendriver browser session, the same way a real
site visitor would.

CFSession stays alive for an entire scrape run — solving costs several
seconds, so it only has to happen once per run, not once per chapter.
"""
import asyncio
import os
import random
import time
from enum import Enum
from urllib.parse import urlparse

import zendriver
from zendriver.core.element import Element

from core.scraper import Progress, extract_content_from_html, NEXT_TEXTS
from core.browser import SELECTORS_TEXT

_CONTENT_WAIT_TIMEOUT = 8.0
_MIN_CONTENT_LENGTH = 500   # real chapters run several thousand chars; below this, the
                            # page likely hadn't finished rendering when we read it
_MAX_FETCH_ATTEMPTS = 3

# If the Chrome process behind a session crashes mid-run, zendriver's CDP calls
# can await a response that will never arrive — hanging that worker forever
# instead of raising. Every call that talks to the browser goes through this
# so a dead connection surfaces as a normal, catchable TimeoutError.
_OP_TIMEOUT = 20.0


async def _guarded(coro, timeout: float = _OP_TIMEOUT):
    return await asyncio.wait_for(coro, timeout=timeout)


class _ChallengePlatform(Enum):
    JAVASCRIPT = "non-interactive"
    MANAGED = "managed"
    INTERACTIVE = "interactive"


async def _detect_challenge(driver):
    html = await _guarded(driver.main_tab.get_content())
    for platform in _ChallengePlatform:
        if f"cType: '{platform.value}'" in html:
            return platform
    return None


async def _click_turnstile_widget(driver) -> None:
    """Finds the Turnstile checkbox in its shadow DOM and clicks it, like a human would."""
    try:
        widget_input = await driver.main_tab.find("input", timeout=3)
    except Exception:
        return

    if widget_input.parent is None or not widget_input.parent.shadow_roots:
        return

    challenge = Element(widget_input.parent.shadow_roots[0], driver.main_tab, widget_input.parent.tree)
    challenge = challenge.children[0]

    if isinstance(challenge, Element) and "display: none;" not in challenge.attrs.get("style", ""):
        try:
            await challenge.get_position()
        except Exception:
            return
        await challenge.mouse_click()


class CFSession:
    """A live zendriver browser that solves a Cloudflare challenge once and
    then fetches every subsequent chapter through that same session."""

    def __init__(self, timeout: float = 30.0):
        self._driver = None
        self._timeout = timeout

    async def start(self, seed_url: str) -> None:
        config = zendriver.Config(headless=False)
        self._driver = zendriver.Browser(config)
        await _guarded(self._driver.start(), timeout=45.0)
        await _guarded(self._driver.get(seed_url))
        await self._clear_challenge()

    async def _clear_challenge(self) -> None:
        start = time.time()
        while (
            await _detect_challenge(self._driver) is not None
            and (time.time() - start) < self._timeout
        ):
            await _click_turnstile_widget(self._driver)
            await asyncio.sleep(1)

    async def fetch_text(self, url: str) -> str:
        """
        Navigates to url and returns extracted chapter text, reloading and
        retrying (up to _MAX_FETCH_ATTEMPTS times) whenever the extracted text
        comes back under _MIN_CONTENT_LENGTH — under concurrent-worker CPU
        load, the content div sometimes hasn't rendered yet when we read the
        page, and a short wait alone isn't a reliable enough signal that it has.
        """
        tab = self._driver.main_tab
        text = ""

        for attempt in range(_MAX_FETCH_ATTEMPTS):
            await _guarded(tab.get(url))

            for sel in SELECTORS_TEXT:
                try:
                    await tab.select(sel, timeout=_CONTENT_WAIT_TIMEOUT / len(SELECTORS_TEXT))
                    break
                except Exception:
                    continue

            if await _detect_challenge(self._driver) is not None:
                await self._clear_challenge()
                await _guarded(tab.get(url))
                await asyncio.sleep(1)

            html = await _guarded(tab.get_content())
            text = extract_content_from_html(html)

            if len(text) >= _MIN_CONTENT_LENGTH:
                return text

            if attempt < _MAX_FETCH_ATTEMPTS - 1:
                await asyncio.sleep(1.5)

        return text  # last attempt's result even if still short — caller decides done/failed

    async def find_next_href(self) -> str:
        for text in NEXT_TEXTS:
            try:
                el = await self._driver.main_tab.find(text, best_match=True, timeout=2)
            except Exception:
                continue
            href = (el.attrs or {}).get("href") if el else None
            if href:
                return href
        return ""

    async def stop(self) -> None:
        if self._driver:
            try:
                await _guarded(self._driver.stop(), timeout=10.0)
            except Exception:
                pass  # best-effort cleanup — a hung/crashed browser shouldn't block the caller
            self._driver = None


# ── Follow-next-link scraping ─────────────────────────────────────────────────

async def _scrape_follow_links_async(
    start_url: str,
    output_file: str,
    progress_file: str,
    delay: float,
    max_chapters: int,
) -> None:
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

    session = CFSession()
    print("Launching browser and solving Cloudflare challenge...")
    try:
        await session.start(seed_url)
    except Exception as e:
        print(f"FAILED TO START: {e}")
        await session.stop()
        return

    current_url = seed_url

    if advance_first:
        print("Finding resume point from last completed page...")
        next_href = await session.find_next_href()
        if not next_href:
            print("Cannot resume: no next link found on last completed page.")
            await session.stop()
            return
        parsed = urlparse(current_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        current_url = next_href if next_href.startswith("http") else base + next_href
        print(f"Resuming at: {current_url}")

    file_mode = "a" if resume else "w"
    with open(output_file, file_mode, encoding="utf-8") as f:
        while chapter_num <= max_chapters:
            print(f"Ch {chapter_num} -> {current_url}")

            try:
                text = await session.fetch_text(current_url)
            except Exception as e:
                print(f"  ERR: {e}")
                progress.mark_failed(chapter_num)
                break

            if len(text) >= _MIN_CONTENT_LENGTH:
                f.write(f"\n\n=== CHAPTER {chapter_num} ===\n\n{text}")
                f.flush()
                progress.mark_done(chapter_num, current_url)
                print(f"  OK {len(text)} chars")
            else:
                progress.mark_failed(chapter_num)
                print(f"  TOO SHORT ({len(text)} chars) - marked failed")

            next_href = await session.find_next_href()
            if not next_href:
                print("No next chapter link found — reached the end.")
                break

            parsed = urlparse(current_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            next_url = next_href if next_href.startswith("http") else base + next_href

            if next_url == current_url:
                print("Next link points to same page — stopping.")
                break

            chapter_num += 1
            current_url = next_url
            await asyncio.sleep(delay + random.uniform(0, delay * 0.4))

    await session.stop()

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone -> {output_file}")


def scrape_follow_links_cf(
    start_url: str,
    output_file: str,
    progress_file: str,
    delay: float = 2.0,
    max_chapters: int = 5000,
) -> None:
    """Cloudflare-aware equivalent of core.scraper.scrape_follow_links."""
    asyncio.run(_scrape_follow_links_async(start_url, output_file, progress_file, delay, max_chapters))


# ── Known URL list scraping (TOC mode) ────────────────────────────────────────

async def _scrape_url_list_async(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int,
    delay: float,
) -> None:
    progress = Progress(progress_file)
    resume = progress.done_count > 0 and os.path.exists(output_file)

    if resume:
        print(f"Resuming — {progress.done_count} chapters already done.")

    session = CFSession()
    print("Launching browser and solving Cloudflare challenge...")
    try:
        await session.start(urls[0])
    except Exception as e:
        print(f"FAILED TO START: {e}")
        await session.stop()
        return

    file_mode = "a" if resume else "w"
    with open(output_file, file_mode, encoding="utf-8") as f:
        for i, url in enumerate(urls, start=start_num):
            if progress.is_done(i):
                continue

            print(f"Ch {i} -> {url}")
            try:
                text = await session.fetch_text(url)
            except Exception as e:
                print(f"  ERR: {e}")
                progress.mark_failed(i)
                continue

            if len(text) >= _MIN_CONTENT_LENGTH:
                f.write(f"\n\n=== CHAPTER {i} ===\n\n{text}")
                f.flush()
                progress.mark_done(i, url)
                print(f"  OK {len(text)} chars")
            else:
                progress.mark_failed(i)
                print(f"  TOO SHORT ({len(text)} chars) - marked failed")

            await asyncio.sleep(delay + random.uniform(0, delay * 0.4))

    await session.stop()

    if progress.failed:
        print(f"\nWarning: {len(progress.failed)} chapter(s) failed: {progress.failed}")
    print(f"\nDone -> {output_file}")


def scrape_url_list_cf(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int = 1,
    delay: float = 2.0,
) -> None:
    """Cloudflare-aware equivalent of core.scraper.scrape_url_list."""
    asyncio.run(_scrape_url_list_async(urls, output_file, progress_file, start_num, delay))


# ── Parallel known-URL-list scraping (multiple independent CFSessions) ───────
#
# A cf_clearance cookie only stays valid within the session that solved its
# own challenge, so parallelism can't share one browser/cookie the way the
# Playwright async path does. Instead, each worker gets its own real Chrome
# window and solves its own challenge, then scrapes a contiguous chunk of
# chapters sequentially. Chunks (not an interleaved queue) keep each worker's
# output file internally ordered, so merging afterward is a straight
# concatenation — no need to buffer or reorder the whole novel in memory.

def _split_chunks(items: list, workers: int) -> list:
    if workers <= 1 or len(items) <= 1:
        return [items]
    chunk_size = -(-len(items) // workers)  # ceil division
    return [items[i : i + chunk_size] for i in range(0, len(items), chunk_size)]


async def _run_chunk(
    chunk: list, part_output_file: str, part_progress_file: str, delay: float, label: str
) -> None:
    progress = Progress(part_progress_file)
    pending = [(num, url) for num, url in chunk if not progress.is_done(num)]

    if not pending:
        print(f"[{label}] already complete.")
        return

    resume = progress.done_count > 0 and os.path.exists(part_output_file)
    session = CFSession()
    print(f"[{label}] launching browser and solving Cloudflare challenge...")
    try:
        await session.start(pending[0][1])
    except Exception as e:
        print(f"[{label}] FAILED TO START: {e} — {len(pending)} chapter(s) left pending for a retry pass.")
        await session.stop()
        return

    file_mode = "a" if resume else "w"
    with open(part_output_file, file_mode, encoding="utf-8") as f:
        for num, url in pending:
            print(f"[{label}] Ch {num} -> {url}")
            try:
                text = await session.fetch_text(url)
            except Exception as e:
                print(f"[{label}]   ERR Ch {num}: {e}")
                progress.mark_failed(num)
                continue

            if len(text) >= _MIN_CONTENT_LENGTH:
                f.write(f"\n\n=== CHAPTER {num} ===\n\n{text}")
                f.flush()
                progress.mark_done(num, url)
                print(f"[{label}]   OK {len(text)} chars")
            else:
                progress.mark_failed(num)
                print(f"[{label}]   TOO SHORT ({len(text)} chars) - marked failed")

            await asyncio.sleep(delay + random.uniform(0, delay * 0.4))

    await session.stop()

    if progress.failed:
        print(f"[{label}] Warning: {len(progress.failed)} chapter(s) failed: {progress.failed}")


async def _scrape_url_list_parallel_async(
    urls: list, output_file: str, progress_file: str, start_num: int, delay: float, workers: int
) -> None:
    numbered = list(enumerate(urls, start=start_num))
    chunks = [c for c in _split_chunks(numbered, workers) if c]

    part_files = []
    tasks = []
    for i, chunk in enumerate(chunks):
        part_output = f"{output_file}.part{i}"
        part_progress = f"{progress_file}.part{i}"
        label = f"W{i + 1} ({chunk[0][0]}-{chunk[-1][0]})"
        part_files.append(part_output)
        tasks.append(_run_chunk(chunk, part_output, part_progress, delay, label))

    print(f"Running {len(tasks)} parallel Cloudflare-bypass workers...")
    # return_exceptions=True: one worker's crash/hang-timeout must not cancel or
    # block the others — their completed chapters are still valid and should
    # still get merged. A worker that raises just means its remaining chapters
    # weren't fetched this run (they stay "not done" in its progress file).
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Worker {i + 1} did not finish cleanly: {result}")

    print("Merging worker outputs into final file...")
    with open(output_file, "w", encoding="utf-8") as out:
        for part in part_files:
            if os.path.exists(part):
                with open(part, encoding="utf-8") as pf:
                    out.write(pf.read())

    print(f"\nDone -> {output_file}")


def scrape_url_list_cf_parallel(
    urls: list,
    output_file: str,
    progress_file: str,
    start_num: int = 1,
    delay: float = 2.0,
    workers: int = 3,
) -> None:
    """
    Like scrape_url_list_cf, but splits urls into `workers` contiguous chunks
    and runs one independent CFSession (real Chrome window) per chunk
    concurrently, merging outputs in order once every chunk finishes.
    """
    asyncio.run(
        _scrape_url_list_parallel_async(urls, output_file, progress_file, start_num, delay, workers)
    )
