"""Pydantic models for the receipt extraction contract."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["meals", "lodging", "transport", "fuel", "office", "other"]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    amount: float


class ExtractedReceipt(BaseModel):
    """Structured receipt data returned by the model."""

    model_config = ConfigDict(extra="ignore")

    vendor: str | None = None
    date: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    vat: float | None = None
    total: float | None = None
    category: Category = "other"
    line_items: list[LineItem] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _DATE_RE.match(v):
            raise ValueError("date must be YYYY-MM-DD")
        return v

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.upper()
        if not _CURRENCY_RE.match(v):
            raise ValueError("currency must be ISO-4217 (3 uppercase letters)")
        return v


EXTRACTION_PROMPT = """\
You are a receipt-extraction system. Inspect the receipt image and return JSON ONLY
matching this exact schema. Do not include prose, markdown fences, or commentary.

Schema:
{
  "vendor": string|null,
  "date": "YYYY-MM-DD"|null,
  "currency": ISO-4217|null,
  "subtotal": number|null,
  "vat": number|null,
  "total": number|null,
  "category": "meals"|"lodging"|"transport"|"fuel"|"office"|"other",
  "line_items": [{"name": string, "amount": number}],
  "confidence": 0..1
}

Rules:
- Use null when a field is not legible.
- date must be ISO format YYYY-MM-DD.
- currency must be a three-letter ISO-4217 code (e.g. EUR, USD, CHF).
- amounts are decimal numbers without thousands separators or currency symbols.
- category is the best single fit from the enum.
- confidence is your own estimate of overall extraction quality between 0 and 1.
"""
