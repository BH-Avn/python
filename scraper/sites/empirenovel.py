"""
sites/empirenovel.py — Empire Novel chapter scraper.

empirenovel.com sits behind a Cloudflare managed challenge, so a plain request
returns 403 and Playwright is fingerprinted and blocked. This module therefore
takes a different route from the other site modules: it solves the challenge
once with CF-Clearance-Scraper (an external CLI, see README), then reuses the
resulting cf_clearance cookie with a plain requests.Session for every chapter.

No browser is launched per chapter, which is what makes long runs practical.

Two constraints on the clearance cookie:
  - it is bound to the IP address that obtained it
  - it is bound to the User-Agent that obtained it
Both are handled by storing the UA alongside the cookie and always sending the
pair together. If the cookie is rejected mid-run, it is re-solved and the run
continues from where it stopped.

Chapter URLs are flat and numeric:  /novel/<slug>/<chapter-number>
"""
import json
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

from core.epub_writer import txt_to_epub
from core.scraper import Progress

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_BASE = os.path.join(BASE_DIR, "output")
SOLVER = os.path.join(BASE_DIR, "tools", "cf-clearance-scraper", "main.py")
COOKIE_PATH = os.path.join(BASE_DIR, "cf_cookie.json")

DOMAIN = "https://www.empirenovel.com"
COOKIE_DOMAIN = ".empirenovel.com"
CONTENT_SELECTOR = "div#read-novel"

SOLVER_HINT = (
    "CF-Clearance-Scraper not found at:\n"
    "  {path}\n\n"
    "Clone it from the scraper/ directory:\n"
    "  git clone https://github.com/Xewdy444/CF-Clearance-Scraper.git "
    "tools/cf-clearance-scraper\n"
    "  pip install -r tools/cf-clearance-scraper/requirements.txt"
)


def _sanitize(name: str) -> str:
    """Strips characters that are illegal in Windows/Linux file and folder names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def _slug_from(value: str) -> str:
    """Accepts either a full novel/chapter URL or a bare slug; returns the slug."""
    match = re.search(r"/novel/([^/?#]+)", value)
    return match.group(1) if match else value.strip().strip("/")


# ── Cloudflare clearance ──────────────────────────────────────────────────────

def _solve(url: str) -> dict:
    """
    Runs the CF-Clearance-Scraper CLI against `url`, returning
    {"cf_clearance": ..., "user_agent": ...}. Raises if the solver is missing
    or exits non-zero.
    """
    if not os.path.exists(SOLVER):
        raise RuntimeError(SOLVER_HINT.format(path=SOLVER))

    print("[CF] Solving Cloudflare challenge...")
    subprocess.run(
        [sys.executable, SOLVER, "-f", COOKIE_PATH, "-t", "120", url],
        check=True,
    )
    return _load_cookie(required=True)


def _load_cookie(required: bool = False):
    """
    Returns the most recent stored cookie/UA pair, or None when unavailable
    and `required` is False.
    """
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            entry = json.load(f)[COOKIE_DOMAIN][-1]
        return {"cf_clearance": entry["cf_clearance"], "user_agent": entry["user_agent"]}
    except Exception:
        if required:
            raise RuntimeError(f"Solver did not write a usable cookie to {COOKIE_PATH}")
        return None


def _session(creds: dict) -> requests.Session:
    """Builds a session carrying the clearance cookie and its matching User-Agent."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": creds["user_agent"],
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    session.cookies.set("cf_clearance", creds["cf_clearance"], domain=COOKIE_DOMAIN)
    return session


def _blocked(resp: requests.Response) -> bool:
    """True if the response is a Cloudflare challenge rather than real content."""
    return resp.status_code in (403, 503) or "Just a moment" in resp.text[:2000]


# ── Page parsing ──────────────────────────────────────────────────────────────

def _latest_chapter(session: requests.Session, slug: str):
    """Reads the novel index and returns the highest chapter number, or None."""
    resp = session.get(f"{DOMAIN}/novel/{slug}", timeout=30)
    if resp.status_code != 200 or _blocked(resp):
        return None

    pattern = re.compile(re.escape(f"/{slug}/") + r"(\d+)")
    numbers = {
        int(m.group(1))
        for a in BeautifulSoup(resp.text, "lxml").find_all("a", href=True)
        if (m := pattern.search(a["href"]))
    }
    return max(numbers) if numbers else None


def _chapter_text(html: str) -> str:
    """Extracts the chapter body from a chapter page."""
    node = BeautifulSoup(html, "lxml").select_one(CONTENT_SELECTOR)
    if node is None:
        return ""
    paragraphs = [p.get_text(" ", strip=True) for p in node.find_all("p")]
    return "\n\n".join(p for p in paragraphs if p)


def _parse_range(range_str: str, default_start: int = 1):
    """
    Parses "2337-2354", "2337-" (open ended) or "2337" into (start, end).
    `end` is None when the range is open ended, meaning "up to the latest".
    """
    if not range_str:
        return default_start, None

    text = range_str.strip()
    if "-" not in text:
        return int(text), None

    start_str, end_str = text.split("-", 1)
    start = int(start_str) if start_str.strip() else default_start
    end = int(end_str) if end_str.strip() else None
    return start, end


# ── Scraping ──────────────────────────────────────────────────────────────────

def scrape(slug, start, end, novel_name, delay=2.0, epub=False):
    """
    Scrapes chapters [start..end] of `slug` into output/<novel_name>/.
    `end` of None resolves to the latest chapter on the site. Progress is kept
    in a .progress.json sidecar, so an interrupted run resumes where it left
    off rather than starting over.
    """
    creds = _load_cookie() or _solve(f"{DOMAIN}/novel/{slug}/{start}")
    session = _session(creds)

    # Validate the stored cookie before committing to a long run.
    if _blocked(session.get(f"{DOMAIN}/novel/{slug}/{start}", timeout=30)):
        print("[CF] Stored cookie rejected; re-solving...")
        session = _session(_solve(f"{DOMAIN}/novel/{slug}/{start}"))

    if end is None:
        end = _latest_chapter(session, slug)
        if end is None:
            raise RuntimeError("Could not determine the latest chapter.")
        print(f"[Info] Latest chapter on site: {end}")

    if start > end:
        raise ValueError(f"Start ({start}) is past the latest chapter ({end}).")

    safe_name = _sanitize(novel_name)
    novel_dir = os.path.join(OUTPUT_BASE, safe_name)
    os.makedirs(novel_dir, exist_ok=True)
    output_txt = os.path.join(novel_dir, f"{safe_name}.txt")
    output_epub = os.path.join(novel_dir, f"{safe_name}.epub")
    progress = Progress(os.path.join(novel_dir, f"{safe_name}.progress.json"))

    if progress.done_count:
        print(f"[Resume] {progress.done_count} chapter(s) already scraped; skipping those.")

    print(f"[Info] Chapters {start}-{end} ({end - start + 1}) -> {output_txt}\n")

    mode = "a" if progress.done_count else "w"
    scraped = failed = 0

    with open(output_txt, mode, encoding="utf-8") as out:
        for num in range(start, end + 1):
            if progress.is_done(num):
                continue

            url = f"{DOMAIN}/novel/{slug}/{num}"
            try:
                resp = session.get(url, timeout=30)

                if _blocked(resp):
                    print(f"  [CF] Challenge at chapter {num}; re-solving...")
                    session = _session(_solve(url))
                    resp = session.get(url, timeout=30)

                if resp.status_code != 200:
                    print(f"Chapter {num}: HTTP {resp.status_code}  [skipped]")
                    progress.mark_failed(num, save=False)
                    failed += 1
                    continue

                text = _chapter_text(resp.text)
                if not text:
                    print(f"Chapter {num}: no text found  [skipped]")
                    progress.mark_failed(num, save=False)
                    failed += 1
                    continue

                out.write(f"\n\n=== CHAPTER {num} ===\n\n{text}")
                out.flush()
                progress.mark_done(num, url, save=False)
                scraped += 1
                print(f"Chapter {num}: {len(text):,} chars")

            except Exception as e:
                print(f"Chapter {num}: ERROR {e}  [skipped]")
                progress.mark_failed(num, save=False)
                failed += 1

            if scraped % 10 == 0:
                progress.save()

            time.sleep(delay)

    progress.save()
    print(f"\nDone. {scraped} scraped, {failed} failed -> {output_txt}")
    if progress.failed:
        print(f"Failed chapters: {progress.failed}")

    if epub and os.path.exists(output_txt):
        txt_to_epub(output_txt, output_epub)

    return output_txt


# ── Entry points ──────────────────────────────────────────────────────────────

def run_cli(args):
    """Entry point called by main.py when CLI args are provided."""
    slug = _slug_from(args.url)
    start, end = _parse_range(getattr(args, "chapter_range", None) or "")
    scrape(
        slug=slug,
        start=start,
        end=end,
        novel_name=args.name,
        delay=getattr(args, "delay", 2.0),
        epub=getattr(args, "epub", False),
    )


def run():
    """Entry point called by main.py (interactive mode)."""
    print("\nEmpire Novel — empirenovel.com")
    print("Cloudflare-protected; the challenge is solved once, then reused.\n")

    raw = input("Novel URL or slug (e.g. lord-of-the-truth): ").strip()
    if not raw:
        print("A URL or slug is required.")
        return
    slug = _slug_from(raw)

    novel_name = input(f"Novel name [{slug}]: ").strip() or slug
    start_raw = input("Start chapter [1]: ").strip()
    end_raw = input("End chapter (press Enter for latest): ").strip()
    delay_raw = input("Delay between chapters in seconds (Enter for 2.0): ").strip()
    epub = input("Convert to EPUB when done? (y/n): ").strip().lower() == "y"

    scrape(
        slug=slug,
        start=int(start_raw) if start_raw else 1,
        end=int(end_raw) if end_raw else None,
        novel_name=novel_name,
        delay=float(delay_raw) if delay_raw else 2.0,
        epub=epub,
    )
