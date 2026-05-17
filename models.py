"""
Output models that the candidate's code must produce.

DO NOT MODIFY THIS FILE.
"""

from __future__ import annotations

from datetime import date
from pydantic import BaseModel


class DocumentRef(BaseModel):
    """A reference to a downloadable document."""

    name: str
    url: str  # Must be an absolute URL (http://...)


class TenderResult(BaseModel):
    """A single tender extracted from the portal listing + detail pages."""

    tender_id: int
    cig: str | None = None
    cup: str | None = None
    title: str
    amount: float | None = None
    deadline: date | None = None
    pub_date: date | None = None
    contracting_body: str | None = None
    procedure_type: str | None = None
    detail_url: str
    documents: list[DocumentRef] = []


class CIGDetail(BaseModel):
    """Enrichment data fetched from the ANAC CIG detail page."""

    cig: str
    numero_gara: str | None = None
    procedure_type: str | None = None
    description: str | None = None
    cpv_codes: list[dict] | None = None
    amount: float | None = None
    start_date: str | None = None
    contracting_body: str | None = None
    contracting_body_cf: str | None = None
