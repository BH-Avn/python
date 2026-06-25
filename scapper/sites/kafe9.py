"""
sites/kafe9.py — 9kafe EPUB downloader.

Uses Playwright to browse a novel's page on 9kafe.com, extracts
Google Drive download links for each part, then downloads the EPUB
files via requests.
"""
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install requests")
    sys.exit(1)

from playwright.sync_api import sync_playwright

from core.browser import make_context
from core.cache import KafeCache

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE_DIR, "cache.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloaded_epubs")


# ── Link conversion ─────────────────────────────────────────────────────────

def _convert_drive_link(gdrive_url: str):
    """
    Converts a Google Drive share URL to a direct-download URL.
    Handles both ?id=ID and /file/d/ID/view formats.
    Returns None if no file ID can be extracted.
    """
    m = re.search(r"id=([a-zA-Z0-9_-]+)", gdrive_url)
    if not m:
        m = re.search(r"/d/([a-zA-Z0-9_-]+)", gdrive_url)
    if not m:
        return None
    file_id = m.group(1)
    return f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"


# ── Scraping ────────────────────────────────────────────────────────────────

def _scrape_novel_page(novel_url: str, slug: str) -> list:
    """
    Iterates over /download/0/, /download/1/, etc. using Playwright,
    finds each Google Drive link, and returns a list of
    { filename, direct_download_url } dicts.

    Stops after MAX_FAILURES consecutive pages with no Drive link.
    """
    epubs = []
    base = novel_url.rstrip("/")
    MAX_FAILURES = 3

    print("\nStarting Playwright scraper to fetch Drive links...")
    try:
        with sync_playwright() as p:
            browser, ctx = make_context(p, block_resources=False)
            page = ctx.new_page()

            # Auto-close any ad popups
            page.on("popup", lambda popup: popup.close())

            # Image/media/font blocking is handled at the context level by
            # make_context(block_resources=True).

            i = 0
            consecutive_failures = 0

            while True:
                if consecutive_failures >= MAX_FAILURES:
                    print(
                        f"Hit {MAX_FAILURES} consecutive failures. "
                        "Assuming end of list."
                    )
                    break

                page_url = f"{base}/download/{i}/"
                print(f"[{i}] Checking: {page_url}")

                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_selector(
                        "a[href*='drive.google.com']",
                        state="attached",
                        timeout=10000,
                    )
                    a_loc = page.locator("a[href*='drive.google.com']").first
                    gdrive_url = a_loc.get_attribute("href")
                    if not gdrive_url:
                        raise Exception("No href attribute found.")
                except Exception as e:
                    print(f"  No Drive link. ({str(e).splitlines()[0]})")
                    consecutive_failures += 1
                    i += 1
                    continue

                consecutive_failures = 0
                direct_url = _convert_drive_link(gdrive_url)

                if direct_url:
                    filename = f"{slug}_part{i}.epub"
                    epubs.append({"filename": filename, "direct_download_url": direct_url})
                    print(f"  ✓ Found: {filename}")
                else:
                    print(f"  Could not extract Drive ID from: {gdrive_url}")

                i += 1
                time.sleep(1)  # polite delay

            browser.close()
    except Exception as e:
        print(f"Critical error during scraping: {e}")

    return epubs


# ── Interactive selection ────────────────────────────────────────────────────

def _show_selection(epubs: list) -> list:
    """Prints available files and returns the user's selection."""
    print("\nAvailable EPUB files:")
    for idx, ep in enumerate(epubs, 1):
        print(f"  {idx}. {ep['filename']}")

    while True:
        choice = input(
            "\nEnter numbers (comma-separated) for specific files, "
            "or press Enter to download ALL.\nSelection: "
        ).strip()

        if not choice:
            return epubs

        try:
            indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
            invalid = [x for x in indices if x < 1 or x > len(epubs)]
            if invalid:
                print(f"Out-of-range: {invalid}. Try again.")
                continue
            return [epubs[i - 1] for i in sorted(set(indices))]
        except ValueError:
            print("Invalid input. Enter numbers separated by commas.")


# ── Download ─────────────────────────────────────────────────────────────────

def _download_epub(url: str, filepath: str) -> None:
    """Streams an EPUB from a direct URL to disk."""
    filename = os.path.basename(filepath)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, headers=headers, timeout=60, stream=True)
        r.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"  ✓ Saved: {filename} ({size_mb:.1f} MB)")
    except requests.exceptions.RequestException as e:
        print(f"  Network error: {e}")
    except OSError as e:
        print(f"  Disk error: {e}")


# ── Entry point ──────────────────────────────────────────────────────────────

def run():
    """Entry point called by main.py."""
    url_input = input("Enter the novel URL (e.g., https://9kafe.com/.../): ").strip()
    if not url_input.startswith("http"):
        print("Error: URL must start with http or https.")
        return

    base_url = url_input.rstrip("/")
    slug = base_url.split("/")[-1]

    novel_name = input("Novel name (used for the download folder): ").strip()
    if not novel_name:
        novel_name = slug  # fall back to URL slug if left blank
    # Strip characters illegal on Windows/Linux
    import re as _re
    novel_name = _re.sub(r'[\\/*?:"<>|]', "", novel_name).strip()

    cache = KafeCache(CACHE_PATH)
    cache_data = cache.load()
    epubs = []

    # ── Cache routing ───────────────────────────────────────────────────────
    if base_url in cache_data.get("novels", {}):
        print("\nThis URL already exists in cache.")
        while True:
            choice = input("  [1] Use cached data\n  [2] Re-scrape website\nChoose: ").strip()
            if choice == "1":
                epubs = cache_data["novels"][base_url].get("drive_links", [])
                print("Loaded from cache.")
                break
            elif choice == "2":
                epubs = _scrape_novel_page(base_url, slug)
                cache.update(base_url, epubs)
                break
            print("Enter 1 or 2.")
    else:
        epubs = _scrape_novel_page(base_url, slug)
        cache.update(base_url, epubs)

    if not epubs:
        print("No EPUB links found or loaded. Exiting.")
        return

    selected = _show_selection(epubs)
    if not selected:
        print("Nothing selected.")
        return

    # ── Download ────────────────────────────────────────────────────────────
    folder = os.path.join(DOWNLOAD_DIR, novel_name)
    os.makedirs(folder, exist_ok=True)
    print(f"\nFiles will be saved to: {folder}")

    for item in selected:
        filepath = os.path.join(folder, item["filename"])

        if os.path.exists(filepath):
            print(f"\n{item['filename']} already exists.")
            while True:
                c = input("  [1] Skip\n  [2] Redownload and overwrite\nChoose: ").strip()
                if c == "1":
                    print("  Skipped.")
                    break
                elif c == "2":
                    print(f"\nDownloading → {item['filename']} ...")
                    _download_epub(item["direct_download_url"], filepath)
                    break
                print("Enter 1 or 2.")
        else:
            print(f"\nDownloading → {item['filename']} ...")
            _download_epub(item["direct_download_url"], filepath)

        time.sleep(1)  # polite delay between requests

    print("\nAll operations completed.")


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_cli(args):
    """
    Entry point called by main.py when CLI args are provided.
    Downloads all EPUB parts without interactive prompts.
    """
    import re as _re

    url_input = args.url
    if not url_input.startswith("http"):
        print("Error: URL must start with http or https.")
        return

    base_url = url_input.rstrip("/")
    slug = base_url.split("/")[-1]

    novel_name = args.name or slug
    novel_name = _re.sub(r'[\\/*?:"<>|]', "", novel_name).strip()

    cache = KafeCache(CACHE_PATH)
    cache_data = cache.load()

    if base_url in cache_data.get("novels", {}):
        print("Loading from cache...")
        epubs = cache_data["novels"][base_url].get("drive_links", [])
    else:
        epubs = _scrape_novel_page(base_url, slug)
        cache.update(base_url, epubs)

    if not epubs:
        print("No EPUB links found or loaded.")
        return

    folder = os.path.join(DOWNLOAD_DIR, novel_name)
    os.makedirs(folder, exist_ok=True)
    print(f"Downloading {len(epubs)} file(s) to: {folder}")

    for item in epubs:
        filepath = os.path.join(folder, item["filename"])
        if os.path.exists(filepath):
            print(f"  Skipping {item['filename']} (already exists)")
            continue
        print(f"\nDownloading → {item['filename']} ...")
        _download_epub(item["direct_download_url"], filepath)
        time.sleep(1)

    print("\nAll operations completed.")
