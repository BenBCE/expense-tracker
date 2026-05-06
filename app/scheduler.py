"""APScheduler poller that drives processing trips to completion."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from telegram import Bot

from app import end_trip_flow
from app.ai import batch as batch_mod
from app.config import get_settings
from app.db.models import Trip
from app.db.session import session_scope
from app.logging_setup import get_logger

log = get_logger(__name__)


async def _poll_processing_trips(bot: Bot) -> None:
    async with session_scope() as session:
        res = await session.execute(
            select(Trip.id, Trip.batch_id).where(Trip.status == "processing")
        )
        trips = [(row.id, row.batch_id) for row in res.all() if row.batch_id]

    for trip_id, batch_id in trips:
        try:
            status = await batch_mod.poll_batch(batch_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("poll_batch_failed", trip_id=trip_id, error=str(exc))
            continue
        log.info("batch_status", trip_id=trip_id, batch_id=batch_id, status=status)
        if status == "ended":
            await end_trip_flow.process_completed_batch(trip_id, bot)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _poll_processing_trips,
        "interval",
        seconds=settings.batch_poll_interval_seconds,
        kwargs={"bot": bot},
        id="poll_processing_trips",
        coalesce=True,
        max_instances=1,
    )
    return scheduler
