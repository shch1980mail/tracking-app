import re
import structlog
from playwright.async_api import async_playwright
from app.config import settings
from app.services.providers.base import Coords, GpsProvider

log = structlog.get_logger()

_RE_JSON = re.compile(r'"lat"\s*:\s*(\d+\.\d+).*?"lng"\s*:\s*(\d+\.\d+)')
_RE_NSEW = re.compile(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)')


class AxentaProvider(GpsProvider):
    name = "axenta.cloud"
    url_marker = "axenta.cloud"

    async def fetch(self, url: str) -> Coords:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.playwright_headless)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=settings.playwright_timeout_ms, wait_until="domcontentloaded")
                await page.wait_for_timeout(8000)
                html = await page.content()

                m = _RE_JSON.search(html)
                if m:
                    return Coords(float(m.group(1)), float(m.group(2)))

                await page.mouse.click(900, 400)
                await page.wait_for_timeout(2000)
                html = await page.content()
                m = _RE_NSEW.search(html)
                if m:
                    return Coords(float(m.group(1)), float(m.group(2)))

                log.warning("axenta_coords_not_found", url=url)
                return Coords(None, None)
            except Exception as e:
                log.error("axenta_error", url=url, error=str(e))
                return Coords(None, None)
            finally:
                await browser.close()
