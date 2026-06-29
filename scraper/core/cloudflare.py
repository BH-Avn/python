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
import time

# Title substrings shown while Cloudflare is still challenging / the SPA is
# still booting. Any of these (or a title that raises mid-navigation) means
# "not ready yet".
CF_TITLES = ("just a moment", "checking your browser", "loading", "verifying")


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
