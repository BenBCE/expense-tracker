"""Telegram bot handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import get_settings
from app.db.models import Receipt, Trip
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.storage import s3 as s3mod

log = get_logger(__name__)


async def _active_trip(session: AsyncSession, user_id: int) -> Trip | None:
    res = await session.execute(
        select(Trip).where(Trip.user_id == user_id, Trip.status == "active")
    )
    return res.scalar_one_or_none()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Hi! Send /start_trip <name> to begin a trip, then forward me your receipts.\n"
        "When you're done, /end_trip will produce the report."
    )


async def cmd_start_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    name = " ".join(context.args or []).strip()
    if not name:
        await msg.reply_text("Usage: /start_trip <name>")
        return

    async with session_scope() as session:
        existing = await _active_trip(session, user.id)
        if existing:
            await msg.reply_text(
                f"You already have an active trip #{existing.id} '{existing.name}'. "
                "Run /end_trip first."
            )
            return
        trip = Trip(user_id=user.id, name=name, status="active")
        session.add(trip)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            await msg.reply_text("Could not start trip — you already have one in progress.")
            return
        trip_id = trip.id

    await msg.reply_text(f"✈️ Trip #{trip_id} '{name}' started. Send me receipts.")


async def _next_seq(session: AsyncSession, trip_id: int) -> int:
    res = await session.execute(
        select(Receipt.seq).where(Receipt.trip_id == trip_id).order_by(Receipt.seq.desc()).limit(1)
    )
    last = res.scalar_one_or_none()
    return (last or 0) + 1


async def _download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    file = await context.bot.get_file(file_id)
    buf = BytesIO()
    await file.download_to_memory(out=buf)
    return buf.getvalue()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    file_id: str | None = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id

    if not file_id:
        return

    settings = get_settings()
    async with session_scope() as session:
        trip = await _active_trip(session, user.id)
        if not trip:
            await msg.reply_text("No active trip. Run /start_trip <name> first.")
            return
        seq = await _next_seq(session, trip.id)
        receipt = Receipt(
            trip_id=trip.id,
            seq=seq,
            telegram_file_id=file_id,
            s3_key="",
            status="pending",
        )
        session.add(receipt)
        await session.flush()
        s3_key = s3mod.receipt_key(user.id, trip.id, receipt.id)
        receipt.s3_key = s3_key
        rid = receipt.id
        trip_id = trip.id
        user_id = user.id

    try:
        data = await _download_telegram_file(context, file_id)
        await s3mod.upload_bytes(
            settings.s3_bucket_receipts, s3_key, data, content_type="image/jpeg"
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("receipt_upload_failed", trip_id=trip_id, receipt_id=rid, error=str(exc))
        async with session_scope() as session:
            r = await session.get(Receipt, rid)
            if r:
                r.status = "failed"
        await msg.reply_text("⚠️ Failed to save receipt. Please try again.")
        return

    log.info("receipt_uploaded", user_id=user_id, trip_id=trip_id, receipt_id=rid, seq=seq)
    await msg.reply_text(f"📸 receipt #{seq} saved")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await msg.reply_text("Usage: /note <text>")
        return

    async with session_scope() as session:
        trip = await _active_trip(session, user.id)
        if not trip:
            await msg.reply_text("No active trip.")
            return
        res = await session.execute(
            select(Receipt)
            .where(Receipt.trip_id == trip.id, Receipt.deleted_at.is_(None))
            .order_by(Receipt.seq.desc())
            .limit(1)
        )
        receipt = res.scalar_one_or_none()
        if not receipt:
            await msg.reply_text("No receipts yet.")
            return
        receipt.note = text
        seq = receipt.seq

    await msg.reply_text(f"📝 note attached to receipt #{seq}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    async with session_scope() as session:
        trip = await _active_trip(session, user.id)
        if not trip:
            await msg.reply_text("No active trip.")
            return
        res = await session.execute(
            select(Receipt)
            .where(Receipt.trip_id == trip.id, Receipt.deleted_at.is_(None))
            .order_by(Receipt.seq.desc())
        )
        receipts = list(res.scalars().all())

    total = len(receipts)
    last5 = receipts[:5]
    if not last5:
        await msg.reply_text(f"Trip #{trip.id} '{trip.name}' — 0 receipts.")
        return
    lines = [f"Trip #{trip.id} '{trip.name}' — {total} receipt(s). Last 5:"]
    for r in reversed(last5):
        note = f" — {r.note}" if r.note else ""
        lines.append(f"  #{r.seq} [{r.status}]{note}")
    await msg.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not context.args:
        await msg.reply_text("Usage: /cancel <n>")
        return
    try:
        seq = int(context.args[0])
    except ValueError:
        await msg.reply_text("Receipt number must be an integer.")
        return

    async with session_scope() as session:
        trip = await _active_trip(session, user.id)
        if not trip:
            await msg.reply_text("No active trip.")
            return
        res = await session.execute(
            select(Receipt).where(
                Receipt.trip_id == trip.id,
                Receipt.seq == seq,
                Receipt.deleted_at.is_(None),
            )
        )
        receipt = res.scalar_one_or_none()
        if not receipt:
            await msg.reply_text(f"No active receipt #{seq}.")
            return
        receipt.deleted_at = datetime.now(timezone.utc)

    await msg.reply_text(f"🗑 receipt #{seq} cancelled")


async def cmd_end_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark trip processing and kick off batch submission in the background."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    from app import end_trip_flow  # local import to avoid cycle

    async with session_scope() as session:
        res = await session.execute(
            select(Trip)
            .where(Trip.user_id == user.id)
            .where(Trip.status.in_(["active", "processing", "done"]))
            .order_by(Trip.id.desc())
            .limit(1)
        )
        trip = res.scalar_one_or_none()
        if not trip:
            await msg.reply_text("No trip found.")
            return
        trip_id = trip.id
        status = trip.status

    if status == "done":
        await msg.reply_text("Trip already finished — re-sending the report…")
        asyncio.create_task(end_trip_flow.resend_report(trip_id, context.bot))
        return
    if status == "processing":
        await msg.reply_text("Trip is already processing. I'll send the report when it's ready.")
        return

    await msg.reply_text("⏳ Closing trip and queuing receipts. ETA ~5–60 min.")
    asyncio.create_task(end_trip_flow.kick_off(trip_id, context.bot))


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("start_trip", cmd_start_trip))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("end_trip", cmd_end_trip))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(
        MessageHandler(filters.Document.IMAGE | filters.Document.MimeType("image/jpeg"), handle_photo)
    )
