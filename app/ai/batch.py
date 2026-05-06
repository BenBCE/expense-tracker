"""Anthropic Batch API integration for receipt extraction."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.messages.batch_create_params import Request as BatchRequest
from pydantic import ValidationError

from app.ai.schema import EXTRACTION_PROMPT, ExtractedReceipt
from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ReceiptInput:
    receipt_id: int
    image_bytes: bytes
    media_type: str = "image/jpeg"


@dataclass(slots=True)
class BatchOutcome:
    succeeded: dict[int, ExtractedReceipt]
    failed: dict[int, str]
    raw: dict[int, dict[str, Any]]


def _custom_id(receipt_id: int) -> str:
    return f"receipt-{receipt_id}"


def _custom_id_to_receipt_id(custom_id: str) -> int:
    return int(custom_id.removeprefix("receipt-"))


def _build_message_params(image_b64: str, media_type: str) -> dict[str, Any]:
    settings = get_settings()
    return {
        "model": settings.anthropic_model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    }


def _build_requests(receipts: list[ReceiptInput]) -> list[BatchRequest]:
    requests: list[BatchRequest] = []
    for r in receipts:
        b64 = base64.b64encode(r.image_bytes).decode("ascii")
        params = _build_message_params(b64, r.media_type)
        requests.append({"custom_id": _custom_id(r.receipt_id), "params": params})
    return requests


def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=get_settings().anthropic_api_key)


async def submit_batch(receipts: list[ReceiptInput]) -> str:
    """Create a batch on Anthropic. Returns batch_id."""
    if not receipts:
        raise ValueError("submit_batch requires at least one receipt")
    requests = _build_requests(receipts)
    async with _client() as client:
        batch = await client.messages.batches.create(requests=requests)
    log.info("anthropic_batch_submitted", batch_id=batch.id, count=len(requests))
    return batch.id


async def poll_batch(batch_id: str) -> str:
    """Returns processing_status: 'in_progress' | 'canceling' | 'ended'."""
    async with _client() as client:
        batch = await client.messages.batches.retrieve(batch_id)
    return batch.processing_status


def _extract_text_from_message(message: dict[str, Any]) -> str:
    """Pull the first text block out of a Claude message response."""
    parts: list[str] = []
    for block in message.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_message(message: dict[str, Any]) -> ExtractedReceipt:
    text = _strip_code_fences(_extract_text_from_message(message))
    if not text:
        raise ValueError("empty model response")
    data = json.loads(text)
    return ExtractedReceipt.model_validate(data)


async def fetch_results(batch_id: str) -> BatchOutcome:
    """Pull all per-request results for a completed batch."""
    succeeded: dict[int, ExtractedReceipt] = {}
    failed: dict[int, str] = {}
    raw: dict[int, dict[str, Any]] = {}
    async with _client() as client:
        async for entry in await client.messages.batches.results(batch_id):
            entry_dump = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            try:
                receipt_id = _custom_id_to_receipt_id(entry_dump["custom_id"])
            except (KeyError, ValueError) as exc:
                log.warning("batch_entry_bad_custom_id", error=str(exc), entry=entry_dump)
                continue
            raw[receipt_id] = entry_dump
            result = entry_dump.get("result") or {}
            result_type = result.get("type")
            if result_type != "succeeded":
                failed[receipt_id] = result_type or "unknown_error"
                continue
            message = result.get("message") or {}
            try:
                succeeded[receipt_id] = _parse_message(message)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                log.warning("batch_entry_parse_failed", receipt_id=receipt_id, error=str(exc))
                failed[receipt_id] = f"parse_error: {exc}"
    return BatchOutcome(succeeded=succeeded, failed=failed, raw=raw)


async def sync_extract(receipt: ReceiptInput) -> ExtractedReceipt:
    """Fallback: extract a single receipt synchronously via the Messages API."""
    b64 = base64.b64encode(receipt.image_bytes).decode("ascii")
    params = _build_message_params(b64, receipt.media_type)
    async with _client() as client:
        message = await client.messages.create(**params)
    text = _strip_code_fences(
        "\n".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    )
    if not text:
        raise ValueError("empty model response")
    return ExtractedReceipt.model_validate(json.loads(text))


async def sync_extract_with_retry(
    receipt: ReceiptInput, retries: int = 1, backoff: float = 2.0
) -> ExtractedReceipt:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await sync_extract(receipt)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(backoff * (attempt + 1))
    assert last_exc is not None
    raise last_exc
