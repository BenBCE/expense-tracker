"""End-of-trip orchestration: batch submit, poll, build, deliver."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Bot

from app.ai import batch as batch_mod
from app.ai.schema import ExtractedReceipt
from app.config import get_settings
from app.db.models import Expense, Receipt, Trip
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.reports.pdf import build_pdf
from app.reports.types import ReportData, ReportRow
from app.reports.xlsx import build_xlsx
from app.reports.zip import build_zip, bundle_all
from app.storage import s3 as s3mod

log = get_logger(__name__)


@dataclass(slots=True)
class _PendingReceipt:
    id: int
    s3_key: str


async def _list_pending_receipts(trip_id: int) -> list[_PendingReceipt]:
    async with session_scope() as session:
        res = await session.execute(
            select(Receipt.id, Receipt.s3_key)
            .where(
                Receipt.trip_id == trip_id,
                Receipt.deleted_at.is_(None),
                Receipt.status.in_(["pending", "failed"]),
            )
            .order_by(Receipt.seq)
        )
        return [_PendingReceipt(id=r.id, s3_key=r.s3_key) for r in res.all()]


async def _load_inputs(receipts: list[_PendingReceipt]) -> list[batch_mod.ReceiptInput]:
    settings = get_settings()
    inputs: list[batch_mod.ReceiptInput] = []
    for r in receipts:
        data = await s3mod.download_bytes(settings.s3_bucket_receipts, r.s3_key)
        inputs.append(batch_mod.ReceiptInput(receipt_id=r.id, image_bytes=data))
    return inputs


async def kick_off(trip_id: int, bot: Bot) -> None:
    """Submit batch and flip trip to processing. Idempotent if status not active."""
    log.info("end_trip_kick_off", trip_id=trip_id)
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.status != "active":
            return
        user_id = trip.user_id

    pending = await _list_pending_receipts(trip_id)
    if not pending:
        async with session_scope() as session:
            trip = await session.get(Trip, trip_id)
            if trip:
                trip.status = "done"
                trip.ended_at = datetime.now(timezone.utc)
        await bot.send_message(chat_id=user_id, text="No receipts to process. Trip closed.")
        return

    try:
        inputs = await _load_inputs(pending)
        batch_id = await batch_mod.submit_batch(inputs)
    except Exception as exc:  # noqa: BLE001
        log.exception("batch_submit_failed", trip_id=trip_id, error=str(exc))
        await bot.send_message(
            chat_id=user_id,
            text="⚠️ Failed to queue receipts for processing. Please try /end_trip again.",
        )
        return

    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip:
            return
        trip.status = "processing"
        trip.batch_id = batch_id
        trip.ended_at = datetime.now(timezone.utc)


async def resend_report(trip_id: int, bot: Bot) -> None:
    settings = get_settings()
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.status != "done" or not trip.report_keys:
            return
        user_id = trip.user_id
        keys = dict(trip.report_keys)

    for label, key in keys.items():
        try:
            url = await s3mod.presign_url(
                settings.s3_bucket_reports, key, settings.presign_ttl_seconds
            )
            await bot.send_message(chat_id=user_id, text=f"{label}: {url}")
        except Exception as exc:  # noqa: BLE001
            log.exception("resend_presign_failed", trip_id=trip_id, key=key, error=str(exc))


async def _persist_extraction(receipt_id: int, ex: ExtractedReceipt, raw: dict[str, Any]) -> None:
    async with session_scope() as session:
        stmt = (
            pg_insert(Expense)
            .values(
                receipt_id=receipt_id,
                vendor=ex.vendor,
                date=ex.date,
                subtotal=ex.subtotal,
                vat=ex.vat,
                total=ex.total,
                currency=ex.currency,
                category=ex.category,
                line_items=[li.model_dump() for li in ex.line_items],
                confidence=ex.confidence,
                raw_json=raw,
            )
            .on_conflict_do_update(
                index_elements=[Expense.receipt_id],
                set_={
                    "vendor": ex.vendor,
                    "date": ex.date,
                    "subtotal": ex.subtotal,
                    "vat": ex.vat,
                    "total": ex.total,
                    "currency": ex.currency,
                    "category": ex.category,
                    "line_items": [li.model_dump() for li in ex.line_items],
                    "confidence": ex.confidence,
                    "raw_json": raw,
                },
            )
        )
        await session.execute(stmt)
        receipt = await session.get(Receipt, receipt_id)
        if receipt:
            receipt.status = "processed"


async def _mark_failed(receipt_id: int, raw: dict[str, Any]) -> None:
    async with session_scope() as session:
        receipt = await session.get(Receipt, receipt_id)
        if receipt:
            receipt.status = "failed"
        stmt = (
            pg_insert(Expense)
            .values(receipt_id=receipt_id, raw_json=raw)
            .on_conflict_do_update(
                index_elements=[Expense.receipt_id], set_={"raw_json": raw}
            )
        )
        await session.execute(stmt)


async def _retry_failed_sync(failed: dict[int, str]) -> None:
    if not failed:
        return
    settings = get_settings()
    for receipt_id in list(failed.keys()):
        async with session_scope() as session:
            receipt = await session.get(Receipt, receipt_id)
            if not receipt:
                continue
            s3_key = receipt.s3_key
        try:
            data = await s3mod.download_bytes(settings.s3_bucket_receipts, s3_key)
            ex = await batch_mod.sync_extract_with_retry(
                batch_mod.ReceiptInput(receipt_id=receipt_id, image_bytes=data),
                retries=1,
            )
            await _persist_extraction(receipt_id, ex, {"sync_retry": True})
            failed.pop(receipt_id, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("sync_retry_failed", receipt_id=receipt_id, error=str(exc))
            await _mark_failed(receipt_id, {"sync_retry_error": str(exc)})


async def _gather_report_data(trip_id: int, image_dir: Path) -> ReportData:
    settings = get_settings()
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        res = await session.execute(
            select(Receipt, Expense)
            .outerjoin(Expense, Expense.receipt_id == Receipt.id)
            .where(Receipt.trip_id == trip_id, Receipt.deleted_at.is_(None))
            .order_by(Receipt.seq)
        )
        rows: list[ReportRow] = []
        downloads: list[tuple[Receipt, Path]] = []
        for receipt, expense in res.all():
            local_path = image_dir / f"{receipt.seq:03d}.jpg"
            downloads.append((receipt, local_path))
            rows.append(
                ReportRow(
                    seq=receipt.seq,
                    s3_key=receipt.s3_key,
                    image_path=str(local_path),
                    note=receipt.note,
                    vendor=expense.vendor if expense else None,
                    date=expense.date if expense else None,
                    category=expense.category if expense else None,
                    subtotal=Decimal(str(expense.subtotal)) if expense and expense.subtotal is not None else None,
                    vat=Decimal(str(expense.vat)) if expense and expense.vat is not None else None,
                    total=Decimal(str(expense.total)) if expense and expense.total is not None else None,
                    currency=expense.currency if expense else None,
                )
            )
        report = ReportData(trip_name=trip.name, user_id=trip.user_id, rows=rows)

    for receipt, path in downloads:
        try:
            data = await s3mod.download_bytes(settings.s3_bucket_receipts, receipt.s3_key)
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("image_download_failed", receipt_id=receipt.id, error=str(exc))
    return report


def _safe_filename(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", " "):
            keep.append(ch)
        else:
            keep.append("_")
    cleaned = "".join(keep).strip().replace(" ", "_")
    return cleaned or "trip"


async def _build_and_deliver(trip_id: int, bot: Bot) -> None:
    settings = get_settings()
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        assert trip is not None
        user_id = trip.user_id
        trip_name = trip.name

    safe = _safe_filename(trip_name)
    with TemporaryDirectory(prefix="trip-", dir="/tmp") as tmp:
        tmp_path = Path(tmp)
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        data = await _gather_report_data(trip_id, image_dir)

        xlsx_path = tmp_path / f"{safe}.xlsx"
        pdf_path = tmp_path / f"{safe}.pdf"
        zip_path = tmp_path / f"{safe}-receipts.zip"

        await asyncio.to_thread(build_xlsx, data, xlsx_path)
        await asyncio.to_thread(build_pdf, data, pdf_path)
        await asyncio.to_thread(build_zip, data, zip_path)

        deliverables = [xlsx_path, pdf_path, zip_path]
        report_keys: dict[str, str] = {}
        for path in deliverables:
            key = s3mod.report_key(user_id, trip_id, path.name)
            await s3mod.upload_file(
                settings.s3_bucket_reports,
                key,
                str(path),
                content_type=_guess_content_type(path),
            )
            report_keys[path.name] = key

        async with session_scope() as session:
            trip = await session.get(Trip, trip_id)
            if trip:
                trip.report_keys = report_keys
                trip.status = "done"

        total_size = sum(p.stat().st_size for p in deliverables)
        limit = settings.bundle_size_limit_mb * 1024 * 1024
        try:
            if total_size <= limit:
                for path in deliverables:
                    with path.open("rb") as fh:
                        await bot.send_document(chat_id=user_id, document=fh, filename=path.name)
            else:
                combined = tmp_path / f"{safe}-bundle.zip"
                bundle_all(deliverables, combined)
                key = s3mod.report_key(user_id, trip_id, combined.name)
                await s3mod.upload_file(
                    settings.s3_bucket_reports,
                    key,
                    str(combined),
                    content_type="application/zip",
                )
                report_keys[combined.name] = key
                async with session_scope() as session:
                    trip = await session.get(Trip, trip_id)
                    if trip:
                        trip.report_keys = report_keys
                if combined.stat().st_size <= limit:
                    with combined.open("rb") as fh:
                        await bot.send_document(
                            chat_id=user_id, document=fh, filename=combined.name
                        )
                else:
                    raise RuntimeError("bundle exceeds telegram limit")
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_send_failed", trip_id=trip_id, error=str(exc))
            for label, key in report_keys.items():
                try:
                    url = await s3mod.presign_url(
                        settings.s3_bucket_reports, key, settings.presign_ttl_seconds
                    )
                    await bot.send_message(chat_id=user_id, text=f"{label}: {url}")
                except Exception:
                    log.exception("presign_send_failed", key=key)


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf": "application/pdf",
        ".zip": "application/zip",
    }.get(suffix, "application/octet-stream")


async def process_completed_batch(trip_id: int, bot: Bot) -> None:
    """Called by the scheduler when a batch finishes."""
    log.info("process_completed_batch", trip_id=trip_id)
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.status != "processing" or not trip.batch_id:
            return
        batch_id = trip.batch_id
        user_id = trip.user_id

    try:
        outcome = await batch_mod.fetch_results(batch_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("fetch_results_failed", trip_id=trip_id, error=str(exc))
        return

    for receipt_id, ex in outcome.succeeded.items():
        await _persist_extraction(receipt_id, ex, outcome.raw.get(receipt_id, {}))

    await _retry_failed_sync(outcome.failed)
    for receipt_id, reason in outcome.failed.items():
        await _mark_failed(receipt_id, {"batch_failure": reason, **outcome.raw.get(receipt_id, {})})

    try:
        await _build_and_deliver(trip_id, bot)
    except Exception as exc:  # noqa: BLE001
        log.exception("build_and_deliver_failed", trip_id=trip_id, error=str(exc))
        async with session_scope() as session:
            trip = await session.get(Trip, trip_id)
            if trip:
                trip.status = "failed"
        try:
            await bot.send_message(
                chat_id=user_id,
                text="⚠️ Failed to build the report. We'll keep your data — try /end_trip again.",
            )
        except Exception:
            log.exception("notify_failure_failed", trip_id=trip_id)
