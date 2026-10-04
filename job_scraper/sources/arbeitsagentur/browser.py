"""Load the BA's rendered search list, including its "more results" pages."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright


@dataclass(frozen=True)
class SearchPage:
    html: str
    complete: bool


async def fetch_all_search_results(url: str) -> SearchPage:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "www.arbeitsagentur.de"
        or parsed.path != "/jobsuche/suche"
    ):
        raise ValueError("Expected a BA Jobsuche search URL.")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"BA search returned HTTP {response.status if response else 'unknown'}")
            await page.locator("#suchergebnis-h1-anzeige").wait_for(timeout=20_000)

            reject_cookies = page.get_by_role("button", name="Alle Cookies ablehnen")
            try:
                await reject_cookies.wait_for(state="visible", timeout=8_000)
                await reject_cookies.click()
            except PlaywrightTimeoutError:
                pass

            cards = page.locator('[id^="ergebnisliste-item-"][id$="-heading"]')
            more = page.get_by_role("button", name="Weitere Ergebnisse")
            while await more.count() and await more.is_visible() and await more.is_enabled():
                before = await cards.count()
                await more.evaluate("button => button.click()")
                await page.wait_for_function(
                    "previous => document.querySelectorAll('[id^=\"ergebnisliste-item-\"][id$=\"-heading\"]').length > previous",
                    arg=before,
                    timeout=20_000,
                )

            return SearchPage(html=await page.content(), complete=True)
        finally:
            await browser.close()
