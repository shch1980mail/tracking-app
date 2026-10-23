"""gs.baltgps.ru — координаты из HTML после загрузки карты.

Стратегия:
1. Открываем ссылку, ждём загрузки карты.
2. Ищем в HTML паттерн вида `N<lat>, E<lng>` (в т.ч. с пробелом).
3. Если не нашли — кликаем в центр карты (это заставляет фронтенд
   отобразить popup с координатами) и повторяем поиск.
4. Всё в try/except — при ошибке возвращаем пустые координаты.
"""
import re
import structlog
from playwright.async_api import async_playwright

from app.config import settings
from app.services.providers.base import Coords, GpsProvider

log = structlog.get_logger()

# N55.123456, E37.123456 / N55.123456 E37.123456 / N55.123456, E37.123456
_RE_NSEW = re.compile(r"N(\d+\.\d+)[,\s]*E(\d+\.\d+)")
# На случай, если координаты придут как "55.123456, 37.123456"
_RE_PLAIN = re.compile(r"(\d{2}\.\d{4,})\s*[,;]\s*(\d{2}\.\d{4,})")


def _extract_coords(html: str) -> Coords:
    m = _RE_NSEW.search(html)
    if m:
        return Coords(float(m.group(1)), float(m.group(2)))

    # fallback: последняя пара plain-координат (обычно в popup на карте)
    matches = _RE_PLAIN.findall(html)
    if matches:
        lat_s, lng_s = matches[-1]
        lat, lng = float(lat_s), float(lng_s)
        if 40.0 <= lat <= 82.0 and 19.0 <= lng <= 180.0:  # разумные границы для РФ
            return Coords(lat, lng)

    return Coords(None, None)


class GsBaltgpsProvider(GpsProvider):
    name = "gs.baltgps.ru"
    url_marker = "gs.baltgps.ru"

    async def fetch(self, url: str) -> Coords:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=settings.playwright_headless)
            page = await browser.new_page()
            try:
                await page.goto(
                    url,
                    timeout=settings.playwright_timeout_ms,
                    wait_until="domcontentloaded",
                )
                # Карта грузится асинхронно — ждём появления канваса/маркеров
                try:
                    await page.wait_for_selector(
                        "canvas, .leaflet-container, #map",
                        timeout=settings.playwright_timeout_ms,
                    )
                except Exception:
                    # Селектор не нашёлся — не страшно, просто ждём таймаут
                    pass

                await page.wait_for_timeout(5000)
                html = await page.content()
                coords = _extract_coords(html)
                if coords.ok:
                    log.info("gs_coords_from_html", lat=coords.lat, lng=coords.lng)
                    return coords

                # Кликаем по центру карты — фронт показывает popup с координатами
                log.info("gs_coords_not_found_in_html_clicking")
                try:
                    await page.mouse.click(900, 400)
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    log.warning("gs_click_failed", error=str(e))

                html = await page.content()
                coords = _extract_coords(html)
                if coords.ok:
                    log.info("gs_coords_after_click", lat=coords.lat, lng=coords.lng)
                else:
                    log.warning("gs_coords_not_found", url=url)
                return coords

            except Exception as e:
                log.error("gs_provider_error", url=url, error=str(e))
                return Coords(None, None)
            finally:
                await browser.close()
