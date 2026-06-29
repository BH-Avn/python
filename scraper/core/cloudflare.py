"""
core/cloudflare.py — Cloudflare-aware browser context using patchright.

Some sites (e.g. novellive.app) serve an interactive Cloudflare "Just a
moment..." JS challenge that vanilla Playwright cannot pass: Cloudflare detects
the automation (the CDP Runtime.enable leak, navigator.webdriver, etc.) and
loops the challenge forever — the page keeps reloading instead of ever issuing
a cf_clearance cookie. No amount of *waiting* helps, because the challenge is
failing, not loading slowly.

patchright is a drop-in patched Playwright that hides those automation leaks.
Combined with a PERSISTENT context (so the cf_clearance cookie sticks between
runs) and the real Chrome channel, it clears the challenge in ~5–12 s and then
reuses the clearance for every later page in the same context — so only the
first chapter pays the challenge cost.

Requires:
    pip install patchright
    patchright install chromium
"""
import contextlib
import os
import random
import time

# Title substrings shown while Cloudflare is still challenging / the SPA is
# still booting. Any of these (or a title that raises mid-navigation) means
# "not ready yet".
CF_TITLES = ("just a moment", "checking your browser", "loading", "verifying")

# Cloudflare error 1015 — "You are being rate limited" (served as HTTP 429).
# A shared cf_clearance cookie gets you past the challenge but NOT past the
# site's rate-based throttling: too many requests per minute from one IP and
# Cloudflare returns 1015 instead of content. Detected by HTTP 429 (authorita-
# tive) or these body markers, and handled with exponential backoff + retry.
# https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1015/
RATE_LIMIT_MARKERS = ("error 1015", "you are being rate limited", "rate limited")


_INSTALL_HINT = (
    "patchright is required for Cloudflare-protected sites but isn't installed.\n"
    "  pip install patchright\n  patchright install chromium"
)


def _import_patchright():
    """Lazily import patchright so the rest of the suite works without it."""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc


def _import_patchright_async():
    """Lazily import the async patchright API (used for parallel scraping)."""
    try:
        from patchright.async_api import async_playwright
        return async_playwright
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc


@contextlib.contextmanager
def cf_context(profile_dir: str, headless: bool = False):
    """
    Yields a persistent patchright browser context that can pass Cloudflare.

    IMPORTANT: do NOT set a custom user_agent or add_init_script here — those are
    themselves automation tells that re-trigger the challenge. patchright's
    defaults already look like a real Chrome, so we keep the context vanilla.

    The profile_dir persists cookies (including cf_clearance) between runs, so a
    second run of the same novel often skips the challenge entirely.
    """
    sync_playwright = _import_patchright()
    os.makedirs(profile_dir, exist_ok=True)

    with sync_playwright() as p:
        try:
            # Real Google Chrome gives the best stealth; fall back to the
            # bundled chromium if Chrome isn't installed.
            ctx = p.chromium.launch_persistent_context(
                profile_dir, channel="chrome", headless=headless, no_viewport=True
            )
        except Exception:
            ctx = p.chromium.launch_persistent_context(
                profile_dir, headless=headless, no_viewport=True
            )
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def wait_for_cloudflare(page, content_selector: str = "", timeout: float = 45) -> bool:
    """
    Blocks until the Cloudflare interstitial clears and real content is present.

    Returns True on success, False on timeout. While the challenge runs the page
    repeatedly navigates/reloads, so page.title()/evaluate() can raise — we treat
    any error or a challenge-y title as "still challenged" and keep polling.

    If content_selector is given we wait for that element to exist (most
    reliable); otherwise we wait for the body to carry a meaningful amount of
    text.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = (page.title() or "").lower()
        except Exception:
            title = ""  # mid-navigation during a challenge reload

        challenged = (not title) or any(t in title for t in CF_TITLES)
        if not challenged:
            if content_selector:
                try:
                    if page.locator(content_selector).count() > 0:
                        return True
                except Exception:
                    pass
            else:
                try:
                    if page.evaluate("document.body.innerText.length") > 800:
                        return True
                except Exception:
                    pass
        time.sleep(1.0)
    return False


def is_rate_limited(page, response) -> bool:
    """
    True if the current page is a Cloudflare 1015 rate-limit block.

    HTTP 429 is authoritative; we also sniff the body for the 1015 markers in
    case the status is masked behind a custom error page.
    """
    try:
        if response is not None and response.status == 429:
            return True
    except Exception:
        pass
    try:
        body = (page.content() or "").lower()
    except Exception:
        return False
    return any(m in body for m in RATE_LIMIT_MARKERS)


def goto_and_clear(
    page,
    url: str,
    content_selector: str = "",
    timeout: float = 60000,
    retries: int = 4,
    backoff: float = 20.0,
) -> bool:
    """
    Navigates to url, transparently handling both the Cloudflare challenge and
    1015 rate-limiting. Returns True once real content is present, False if it
    couldn't get through after `retries` attempts.

    On a 1015 the page is reloaded after an exponential backoff (backoff,
    2×, 4×, … plus jitter) — the only sane response to rate limiting is to slow
    down and wait it out.
    """
    for attempt in range(retries + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            response = None

        if is_rate_limited(page, response):
            wait = backoff * (2 ** attempt) + random.uniform(0, backoff)
            print(
                f"  ⏳ Cloudflare 1015 rate limit — backing off {wait:.0f}s "
                f"(attempt {attempt + 1}/{retries})"
            )
            time.sleep(wait)
            continue

        if wait_for_cloudflare(page, content_selector):
            return True
        # Cleared the challenge gate but content never showed — brief pause, retry.
        time.sleep(backoff)
    return False


# ── Async variants (for parallel scraping) ────────────────────────────────────
#
# Parallelism is safe under Cloudflare because every page opened in the SAME
# persistent context shares its cf_clearance cookie: warm up one page to solve
# the challenge, and every worker tab afterwards rides the same clearance.

@contextlib.asynccontextmanager
async def cf_context_async(profile_dir: str, headless: bool = False):
    """Async counterpart to cf_context() — yields a persistent patchright context."""
    async_playwright = _import_patchright_async()
    os.makedirs(profile_dir, exist_ok=True)

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                profile_dir, channel="chrome", headless=headless, no_viewport=True
            )
        except Exception:
            ctx = await p.chromium.launch_persistent_context(
                profile_dir, headless=headless, no_viewport=True
            )
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()


async def wait_for_cloudflare_async(page, content_selector: str = "", timeout: float = 45) -> bool:
    """Async counterpart to wait_for_cloudflare()."""
    import asyncio

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        challenged = (not title) or any(t in title for t in CF_TITLES)
        if not challenged:
            if content_selector:
                try:
                    if await page.locator(content_selector).count() > 0:
                        return True
                except Exception:
                    pass
            else:
                try:
                    if await page.evaluate("document.body.innerText.length") > 800:
                        return True
                except Exception:
                    pass
        await asyncio.sleep(1.0)
    return False


async def is_rate_limited_async(page, response) -> bool:
    """Async counterpart to is_rate_limited()."""
    try:
        if response is not None and response.status == 429:
            return True
    except Exception:
        pass
    try:
        body = (await page.content() or "").lower()
    except Exception:
        return False
    return any(m in body for m in RATE_LIMIT_MARKERS)


async def goto_and_clear_async(
    page,
    url: str,
    content_selector: str = "",
    timeout: float = 60000,
    retries: int = 4,
    backoff: float = 20.0,
) -> bool:
    """Async counterpart to goto_and_clear() — challenge- and 1015-aware navigation."""
    import asyncio

    for attempt in range(retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            response = None

        if await is_rate_limited_async(page, response):
            wait = backoff * (2 ** attempt) + random.uniform(0, backoff)
            print(
                f"  ⏳ Cloudflare 1015 rate limit on Ch — backing off {wait:.0f}s "
                f"(attempt {attempt + 1}/{retries})"
            )
            await asyncio.sleep(wait)
            continue

        if await wait_for_cloudflare_async(page, content_selector):
            return True
        await asyncio.sleep(backoff)
    return False
