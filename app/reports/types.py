"""Shared dataclasses for report generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class ReportRow:
    seq: int
    s3_key: str
    image_path: str | None = None
    note: str | None = None
    vendor: str | None = None
    date: str | None = None
    category: str | None = None
    subtotal: Decimal | None = None
    vat: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = None


@dataclass(slots=True)
class ReportData:
    trip_name: str
    user_id: int
    rows: list[ReportRow] = field(default_factory=list)
