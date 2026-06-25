"""
core/browser.py — Shared Playwright browser factory and text extraction.

All site scrapers import from here instead of copy-pasting the same setup.
"""
from playwright.sync_api import Browser, BrowserContext

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Combined selector list covering Novelbin, Novelarrow, and common novel sites.
# Tried in order; first successful match wins.
SELECTORS_TEXT = [
    "div#chr-content",
    "div.chr-c",
    "div.reading-content",
    "div.prose",
    "div#content",
    "article p",
]


# Resource types blocked to speed up page loads — images, video/audio, and
# fonts are never needed for text scraping and waste bandwidth + load time.
BLOCKED_RESOURCES = {"image", "media", "font"}


def _block_route(route):
    """Sync route handler: aborts blocked resource types, continues the rest."""
    if route.request.resource_type in BLOCKED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def make_context(playwright, block_resources: bool = True) -> tuple:
    """
    Launches a Chromium browser and returns a (browser, context) pair
    pre-configured with the shared desktop user-agent.

    When block_resources is True (default), images, media, and fonts are
    blocked at the context level so every page in the context loads faster.
    """
    browser: Browser = playwright.chromium.launch(headless=False)
    ctx: BrowserContext = browser.new_context(user_agent=USER_AGENT)
    if block_resources:
        ctx.route("**/*", _block_route)
    return browser, ctx


def get_text(page) -> str:
    """
    Tries each selector in SELECTORS_TEXT and returns the first match's
    inner text. Falls back to joining all <p> tags if nothing matches.
    Returns an empty string if all attempts fail.
    """
    for sel in SELECTORS_TEXT:
        try:
            if page.locator(sel).count() > 0:
                return page.locator(sel).inner_text(timeout=5000).strip()
        except Exception:
            continue
    try:
        return "\n".join(page.locator("p").all_inner_texts()).strip()
    except Exception:
        return ""
