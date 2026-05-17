"""
Level 2 — Maggioli PortaleAppalti scraper.

Explore the portal at http://127.0.0.1:18080/PortaleAppalti/it/homepage.wp
with a real browser before writing code.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from candidate.parsers import is_valid_cig, parse_amount, parse_date
from models import DocumentRef
from models import TenderResult


async def scrape_portal(base_url: str) -> list[TenderResult]:
    """Scrape all tenders from the Maggioli-style portal at base_url."""
    root_url = base_url.rstrip("/") + "/"
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
        await _fetch_text(client, urljoin(root_url, "PortaleAppalti/it/homepage.wp"))

        listing_url = urljoin(root_url, "PortaleAppalti/it/ppgare_bandi_lista.wp")
        detail_links = await _collect_detail_links(client, listing_url)

        results: list[TenderResult] = []
        for detail_url, listing_title in detail_links:
            html = await _fetch_text(client, detail_url, retry_statuses={503})
            results.append(_parse_detail_page(html, detail_url, listing_title))

    return sorted(results, key=lambda tender: tender.tender_id)


async def _collect_detail_links(
    client: httpx.AsyncClient,
    listing_url: str,
) -> list[tuple[str, str | None]]:
    pending = [listing_url]
    seen_pages: set[str] = set()
    details_by_id: dict[int, tuple[str, str | None]] = {}

    while pending:
        page_url = pending.pop(0)
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)

        html = await _fetch_text(client, page_url)
        soup = BeautifulSoup(html, "html.parser")

        for row in soup.select("#tender-list tbody tr"):
            link = row.select_one('a[href*="ppgare_bando_dettaglio.wp"]')
            if link is None or not link.get("href"):
                continue

            detail_url = urljoin(page_url, link["href"])
            tender_id = _extract_tender_id(detail_url)
            if tender_id is None:
                continue

            title_cell = row.find("td")
            listing_title = _clean_text(title_cell.get_text(" ", strip=True) if title_cell else "")
            details_by_id.setdefault(tender_id, (detail_url, listing_title))

        for link in soup.select("#pagination a[href]"):
            next_url = urljoin(page_url, link["href"])
            if next_url not in seen_pages and next_url not in pending:
                pending.append(next_url)

    return [details_by_id[key] for key in sorted(details_by_id)]


async def _fetch_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    retry_statuses: Iterable[int] = (),
    attempts: int = 4,
) -> str:
    retry_codes = set(retry_statuses)
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = await client.get(url)
            if response.status_code in retry_codes and attempt < attempts - 1:
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            await asyncio.sleep(0.25 * (attempt + 1))

    if last_error:
        raise last_error
    raise RuntimeError(f"Unable to fetch {url}")


def _parse_detail_page(html: str, detail_url: str, listing_title: str | None) -> TenderResult:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("noscript, [style*='display:none'], [style*='display: none']"):
        node.decompose()

    fields = _extract_fields(soup)
    tender_id = _extract_tender_id(detail_url)
    if tender_id is None:
        raise ValueError(f"Could not extract tender id from {detail_url}")

    raw_cig = _first_field(fields, "cig")
    cig = raw_cig.strip().upper() if raw_cig and is_valid_cig(raw_cig) else None

    amount_raw = _first_field(fields, "amount")
    amount = parse_amount(amount_raw or "")
    if amount is None:
        amount = parse_amount(_extract_script_amount(soup) or "")

    title = _extract_title(soup)
    if not title and listing_title and listing_title != "(senza oggetto)":
        title = listing_title

    return TenderResult(
        tender_id=tender_id,
        cig=cig,
        cup=_none_if_blank(_first_field(fields, "cup")),
        title=title,
        amount=amount,
        deadline=parse_date(_first_field(fields, "deadline") or ""),
        pub_date=parse_date(_first_field(fields, "pub_date") or ""),
        contracting_body=_none_if_blank(_first_field(fields, "contracting_body")),
        procedure_type=_none_if_blank(_first_field(fields, "procedure_type")),
        detail_url=detail_url,
        documents=_extract_documents(soup, detail_url),
    )


def _extract_fields(soup: BeautifulSoup) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}

    for row in soup.select("table.dettaglio-bando tr"):
        header = row.find("th")
        value = row.find("td")
        _add_field(fields, _clean_text(header.get_text(" ", strip=True) if header else ""), value)

    dts = soup.select("dl.dati-gara dt")
    for dt in dts:
        dd = dt.find_next_sibling("dd")
        _add_field(fields, _clean_text(dt.get_text(" ", strip=True)), dd)

    for item in soup.select(".campo-dato"):
        label = item.find("label")
        value = item.select_one(".valore") or item.find("span")
        _add_field(fields, _clean_text(label.get_text(" ", strip=True) if label else ""), value)

    return fields


def _add_field(fields: dict[str, list[str]], raw_label: str, value_node: object) -> None:
    key = _canonical_field(raw_label)
    if key is None or value_node is None:
        return

    text = _clean_text(value_node.get_text(" ", strip=True))  # type: ignore[attr-defined]
    fields.setdefault(key, []).append(text)


def _canonical_field(label: str) -> str | None:
    normalized = _normalize_label(label)
    if "cup" in normalized:
        return "cup"
    if "cig" in normalized:
        return "cig"
    if "importo" in normalized:
        return "amount"
    if "scadenza" in normalized or "termine" in normalized:
        return "deadline"
    if "data pubblicazione" in normalized or "pubblicazione" in normalized:
        return "pub_date"
    if "stazione appaltante" in normalized:
        return "contracting_body"
    if "procedura" in normalized:
        return "procedure_type"
    return None


def _first_field(fields: dict[str, list[str]], key: str) -> str | None:
    for value in fields.get(key, []):
        if value:
            return value
    return None


def _extract_title(soup: BeautifulSoup) -> str:
    selectors = [
        "h1.titolo-gara",
        "h2.oggetto-gara",
        "body > h1",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        text = _clean_text(node.get_text(" ", strip=True) if node else "")
        if text:
            return text
    return ""


def _extract_documents(soup: BeautifulSoup, detail_url: str) -> list[DocumentRef]:
    documents: list[DocumentRef] = []
    seen_urls: set[str] = set()

    for link in soup.select(".allegati a[href]"):
        _append_document(
            documents,
            seen_urls,
            link.get_text(" ", strip=True),
            link["href"],
            detail_url,
        )

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()
        for docs_json in re.findall(r"var\s+docs\s*=\s*(\[[\s\S]*?\]);", script_text):
            try:
                docs = json.loads(docs_json)
            except json.JSONDecodeError:
                continue
            for doc in docs:
                if isinstance(doc, dict):
                    _append_document(
                        documents,
                        seen_urls,
                        str(doc.get("name") or ""),
                        str(doc.get("url") or ""),
                        detail_url,
                    )

    return documents


def _append_document(
    documents: list[DocumentRef],
    seen_urls: set[str],
    name: str,
    href: str,
    detail_url: str,
) -> None:
    name = _clean_text(name)
    href = href.strip()
    if not name or not href:
        return

    absolute_url = urljoin(detail_url, href)
    if absolute_url in seen_urls:
        return
    seen_urls.add(absolute_url)
    documents.append(DocumentRef(name=name, url=absolute_url))


def _extract_script_amount(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        match = re.search(r"\btextContent\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match and parse_amount(match.group(1)) is not None:
            return match.group(1)
    return None


def _extract_tender_id(url: str) -> int | None:
    raw_values = parse_qs(urlparse(url).query).get("id")
    if not raw_values:
        return None
    try:
        return int(raw_values[0])
    except (TypeError, ValueError):
        return None


def _none_if_blank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_label(value: str) -> str:
    return _clean_text(value).lower().replace(":", "")
