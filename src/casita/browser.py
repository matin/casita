"""Browser context with anti-bot defaults.

Real Chrome UA, viewport, locale, timezone, and a few `navigator` patches
to avoid the obvious headless-chromium tells. Not a guarantee against
modern bot detection, but enough for most rental sites.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""

# Persistent profile dir — keeps Zillow/PerimeterX cookies between runs so the
# user only has to solve the captcha once.
PROFILE_DIR = Path(__file__).parent.parent.parent / ".chrome-profile"


@asynccontextmanager
async def context(headless: bool = False, persistent: bool = True):
    """Browser context. Defaults to persistent profile so captcha-clears stick."""
    async with async_playwright() as p:
        if persistent:
            PROFILE_DIR.mkdir(exist_ok=True)
            ctx: BrowserContext = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="America/Los_Angeles",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            await ctx.add_init_script(STEALTH_JS)
            try:
                yield ctx
            finally:
                await ctx.close()
        else:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            await ctx.add_init_script(STEALTH_JS)
            try:
                yield ctx
            finally:
                await ctx.close()
                await browser.close()
