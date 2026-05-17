"""
Level 3 — ANAC Dettaglio CIG enrichment.

Explore /cig/{CIG} with DevTools to understand the SPA flow.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from candidate.parsers import is_valid_cig, parse_amount
from models import CIGDetail


_API_PATH = "api/v1/operations/consultaCIG/1.0/exec"
_RATE_LIMIT_SECONDS = 5.1
_RATE_LIMIT_ATTEMPTS = 5
_logger = logging.getLogger(__name__)
_rate_lock = asyncio.Lock()
_last_request_by_base: dict[str, float] = {}


async def enrich_cig(cig: str, base_url: str) -> CIGDetail | None:
    """Fetch detailed ANAC data for a single CIG via the SPA at base_url/cig/{cig}."""
    normalized_cig = (cig or "").strip().upper()
    if not is_valid_cig(normalized_cig):
        return None

    root_url = base_url.rstrip("/") + "/"

    try:
        payload = await _fetch_payload_via_playwright(root_url, normalized_cig)
    except Exception:
        payload = await _fetch_payload_via_http(root_url, normalized_cig)

    bando = _extract_bando(payload)
    if bando is None:
        return None

    return _build_detail(normalized_cig, bando)


async def enrich_batch(cigs: list[str], base_url: str) -> dict[str, CIGDetail | None]:
    """Fetch ANAC details for multiple CIGs, respecting rate limits."""
    results: dict[str, CIGDetail | None] = {}
    for cig in cigs:
        key = (cig or "").strip().upper()
        results[key] = await enrich_cig(key, base_url)
    return results


async def _fetch_payload_via_playwright(root_url: str, cig: str) -> Any:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                ),
                extra_http_headers={"Accept-Language": "it-IT,it;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            await page.goto(urljoin(root_url, f"cig/{cig}"), wait_until="domcontentloaded")
            await page.locator("#consent-check").wait_for(state="attached", timeout=8_000)
            await page.locator("#consent-check").check()
            await page.wait_for_function(
                "() => { const btn = document.getElementById('cerca-btn'); return btn && !btn.disabled; }",
                timeout=8_000,
            )

            for attempt in range(_RATE_LIMIT_ATTEMPTS):
                response = await _rate_limited_playwright_search(page, root_url)
                if response.status == 403:
                    text = await response.text()
                    if "Request Rejected" in text and attempt < _RATE_LIMIT_ATTEMPTS - 1:
                        await _cooldown_before_retry(
                            attempt,
                            page=page,
                            reason="ANAC/F5 rate limit",
                        )
                        continue

                if not response.ok:
                    raise RuntimeError(
                        f"CIG API returned HTTP {response.status}: {await response.text()}"
                    )
                return await response.json()
        finally:
            await browser.close()

    raise RuntimeError(f"Unable to enrich CIG {cig} with Playwright")


async def _fetch_payload_via_http(root_url: str, cig: str) -> Any:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
    ) as client:
        await client.get(urljoin(root_url, f"cig/{cig}"))
        response = await _post_cig_api(client, root_url, cig)

    return response.json()


async def _post_cig_api(
    client: httpx.AsyncClient,
    root_url: str,
    cig: str,
    *,
    attempts: int = _RATE_LIMIT_ATTEMPTS,
) -> httpx.Response:
    url = urljoin(root_url, _API_PATH)
    last_response: httpx.Response | None = None

    for attempt in range(attempts):
        response = await _rate_limited_post(client, root_url, url, cig)

        if _is_rate_limited(response) and attempt < attempts - 1:
            last_response = response
            await _cooldown_before_retry(attempt, reason="ANAC/F5 rate limit")
            continue

        response.raise_for_status()
        return response

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError(f"Unable to enrich CIG {cig}")


async def _rate_limited_post(
    client: httpx.AsyncClient,
    root_url: str,
    url: str,
    cig: str,
) -> httpx.Response:
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_by_base.get(root_url, 0)
        if elapsed < _RATE_LIMIT_SECONDS:
            await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
        response = await client.post(
            url,
            json={"cig": cig},
            cookies={"_mosparo_session": f"validated_{int(time.time() * 1000)}"},
        )
        _last_request_by_base[root_url] = time.monotonic()
        return response


def _is_rate_limited(response: httpx.Response) -> bool:
    return response.status_code == 403 and "Request Rejected" in response.text


async def _cooldown_before_retry(
    attempt: int,
    *,
    page: Any | None = None,
    reason: str,
) -> None:
    delay = _cooldown_seconds(attempt)
    _logger.info("%s detected, cooling down for %.1f seconds before retry", reason, delay)

    remaining = int(delay)
    while remaining > 0:
        await _set_page_cooldown_message(page, remaining)
        await asyncio.sleep(1)
        remaining -= 1

    remainder = delay - int(delay)
    if remainder > 0:
        await asyncio.sleep(remainder)

    await _set_page_cooldown_message(page, 0)


def _cooldown_seconds(attempt: int) -> float:
    return _RATE_LIMIT_SECONDS * min(attempt + 1, 3)


async def _set_page_cooldown_message(page: Any | None, seconds_remaining: int) -> None:
    if page is None:
        return

    if seconds_remaining > 0:
        message = (
            "Rate limit temporaneo rilevato. "
            f"Attendo {seconds_remaining}s prima di riprovare..."
        )
        disabled = True
    else:
        message = "Cooldown completato. Riprovo la ricerca..."
        disabled = False

    try:
        await page.evaluate(
            """({ message, disabled }) => {
                const result = document.getElementById('result');
                const button = document.getElementById('cerca-btn');
                if (result) result.textContent = message;
                if (button) button.disabled = disabled;
            }""",
            {"message": message, "disabled": disabled},
        )
    except Exception:
        return


async def _rate_limited_playwright_search(page: Any, root_url: str) -> Any:
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_by_base.get(root_url, 0)
        if elapsed < _RATE_LIMIT_SECONDS:
            await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)

        async with page.expect_response(
            lambda response: _API_PATH in response.url
            and response.request.method.upper() == "POST",
            timeout=10_000,
        ) as response_info:
            await page.locator("#cerca-btn").click()

        response = await response_info.value
        _last_request_by_base[root_url] = time.monotonic()
        return response


def _extract_bando(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        for item in payload:
            bando = _extract_bando(item)
            if bando is not None:
                return bando
        return None

    if not isinstance(payload, dict):
        return None

    if "bando" in payload:
        bando = payload["bando"]
        if isinstance(bando, dict):
            return bando
        if isinstance(bando, list):
            return _extract_bando(bando)
        return None

    if "CIG" in payload:
        return payload

    return None


def _build_detail(requested_cig: str, bando: dict[str, Any]) -> CIGDetail:
    station = bando.get("STAZIONE_APPALTANTE")
    if not isinstance(station, dict):
        station = {}

    return CIGDetail(
        cig=str(bando.get("CIG") or requested_cig),
        numero_gara=_optional_str(bando.get("NUMERO_GARA")),
        procedure_type=_optional_str(bando.get("TIPO_SCELTA_CONTRAENTE")),
        description=_optional_str(bando.get("OGGETTO_GARA")),
        cpv_codes=bando.get("CPV") if isinstance(bando.get("CPV"), list) else None,
        amount=_amount_value(bando.get("IMPORTO")),
        start_date=_optional_str(bando.get("DATA_AVVIO")),
        contracting_body=_optional_str(station.get("DENOMINAZIONE")),
        contracting_body_cf=_optional_str(station.get("CF")),
    )


def _amount_value(raw: Any) -> float | None:
    if isinstance(raw, int | float):
        return float(raw)
    if raw is None:
        return None
    return parse_amount(str(raw))


def _optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None
