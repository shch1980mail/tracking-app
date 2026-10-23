"""maps.baltgps.ru — координаты через Wialon API + перехват SID через Playwright.

Логика:
1. Извлекаем токен из URL (`?t=...`).
2. Через `token/login` получаем список unit_id.
3. Берём SID из TTL-кэша. Если его нет (или протух) — запускаем Playwright,
   перехватываем `sid` из запросов/ответов страницы, кладём в кэш.
4. Через `core/search_item` получаем `pos.x` (lng) и `pos.y` (lat).
5. Если SID протух — инвалидируем кэш и один раз повторяем с новым SID.
"""
import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from playwright.async_api import async_playwright

from app.config import settings
from app.services.providers.base import Coords, GpsProvider
from app.services.sid_cache import sid_cache

log = structlog.get_logger()

BASE_URL = "https://maps.baltgps.ru"
_RE_SID = re.compile(r"[?&]sid=([a-f0-9]{20,})")


# ---------- token/login: получаем список unit_id ----------

async def _get_units_from_token(token: str) -> list[int]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/wialon/ajax.html",
                params={
                    "svc": "token/login",
                    "params": json.dumps({"token": token}),
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("maps_token_login_error", error=str(e))
            return []

    if "token" not in data:
        log.warning("maps_token_login_no_token", response=data)
        return []

    try:
        inner = json.loads(data["token"])
        return inner.get("items", []) or []
    except Exception as e:
        log.error("maps_token_parse_error", error=str(e))
        return []


# ---------- Playwright: перехват SID ----------

async def _capture_sid(url: str) -> str | None:
    captured: dict[str, str | None] = {"sid": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.playwright_headless)
        context = await browser.new_context()
        page = await context.new_page()

        def extract(url_: str) -> str | None:
            if "sid=" not in url_:
                return None
            m = _RE_SID.search(url_)
            return m.group(1) if m else None

        def on_request(request):
            if captured["sid"]:
                return
            sid = extract(request.url)
            if sid:
                captured["sid"] = sid
                log.info("maps_sid_captured_from_request", sid_prefix=sid[:16])

        def on_response(response):
            if captured["sid"]:
                return
            sid = extract(response.url)
            if sid:
                captured["sid"] = sid
                log.info("maps_sid_captured_from_response", sid_prefix=sid[:16])

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(
                url,
                timeout=settings.playwright_timeout_ms,
                wait_until="domcontentloaded",
            )
            # Ждём, пока фронт выполнит первый AJAX к Wialon
            await page.wait_for_timeout(8000)

            # Если всё ещё нет — пробуем триггернуть перерисовку
            if not captured["sid"]:
                try:
                    await page.mouse.click(900, 400)
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass
        except Exception as e:
            log.error("maps_sid_capture_error", error=str(e))
        finally:
            await browser.close()

    return captured["sid"]


async def _get_or_refresh_sid(token: str, url: str) -> str | None:
    """Возвращает SID из кэша или добывает новый через Playwright (с локом)."""
    sid = sid_cache.get(token)
    if sid:
        return sid

    async with sid_cache.lock(token):
        # double-check: пока ждали лок, кто-то мог уже положить SID
        sid = sid_cache.get(token)
        if sid:
            return sid

        log.info("maps_refreshing_sid", token_prefix=token[:12])
        sid = await _capture_sid(url)
        if sid:
            sid_cache.put(token, sid)
            return sid

        log.warning("maps_sid_not_captured")
        return None


# ---------- core/search_item ----------

async def _get_position(sid: str, unit_id: int) -> tuple[float | None, float | None, str]:
    """Возвращает (lat, lng, status). status ∈ {ok, sid_expired, unknown}."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(
                f"{BASE_URL}/wialon/ajax.html",
                params={
                    "svc": "core/search_item",
                    "params": json.dumps({"id": unit_id, "flags": 1025}),
                    "sid": sid,
                },
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("maps_search_item_error", error=str(e))
            return None, None, "unknown"

    if "error" in data:
        # Код 1/4/7 — обычно протухший/невалидный SID
        log.warning("maps_search_item_api_error", error=data.get("error"))
        return None, None, "sid_expired"

    item = data.get("item") or {}
    pos = item.get("pos") or {}
    if "x" in pos and "y" in pos:
        # В Wialon x = lng, y = lat
        return float(pos["y"]), float(pos["x"]), "ok"

    return None, None, "unknown"


# ---------- Provider ----------

class MapsBaltgpsProvider(GpsProvider):
    name = "maps.baltgps.ru"
    url_marker = "maps.baltgps.ru"

    def _extract_token(self, url: str) -> str | None:
        qs = parse_qs(urlparse(url).query)
        token = qs.get("t", [None])[0]
        return token

    async def fetch(self, url: str) -> Coords:
        token = self._extract_token(url)
        if not token:
            log.warning("maps_no_token_in_url", url=url)
            return Coords(None, None)

        units = await _get_units_from_token(token)
        if not units:
            log.warning("maps_no_units", token_prefix=token[:12])
            return Coords(None, None)

        unit_id = units[0]

        sid = await _get_or_refresh_sid(token, url)
        if not sid:
            return Coords(None, None)

        lat, lng, status = await _get_position(sid, unit_id)
        if status == "ok" and lat is not None and lng is not None:
            return Coords(lat, lng)

        if status == "sid_expired":
            # SID протух — инвалидируем и один раз пробуем заново
            log.info("maps_sid_expired_refreshing", token_prefix=token[:12])
            sid_cache.invalidate(token)
            sid = await _get_or_refresh_sid(token, url)
            if not sid:
                return Coords(None, None)
            lat, lng, status = await _get_position(sid, unit_id)
            if status == "ok" and lat is not None and lng is not None:
                return Coords(lat, lng)

        log.warning("maps_coords_not_found", status=status, unit_id=unit_id)
        return Coords(None, None)
