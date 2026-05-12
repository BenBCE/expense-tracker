"""Portal HTTP routes: Telegram login, dashboard, trips, expenses."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Expense, Receipt, Trip
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.storage import s3 as s3mod
from app.web.auth import (
    TelegramUser,
    current_user,
    store_user_in_session,
    verify_telegram_login,
)

log = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

CurrentUser = Annotated[TelegramUser, Depends(current_user)]

ALLOWED_CATEGORIES = ("meals", "lodging", "transport", "fuel", "office", "other")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    settings = get_settings()
    bot_username = settings.portal_telegram_bot_username or ""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "bot_username": bot_username,
            "public_base_url": settings.public_base_url.rstrip("/"),
        },
    )


@router.get("/auth/telegram")
async def auth_telegram(request: Request) -> Response:
    """Telegram Login Widget redirects here with the signed payload as query string."""
    settings = get_settings()
    data: dict[str, Any] = {k: v for k, v in request.query_params.items()}
    user = verify_telegram_login(
        data, settings.telegram_bot_token, settings.session_max_age_seconds
    )
    if settings.telegram_allowed_user_ids and user.id not in settings.telegram_allowed_user_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
    store_user_in_session(request, user)
    return RedirectResponse(url="/portal/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse(url="/portal/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: CurrentUser) -> Response:
    async with session_scope() as session:
        res = await session.execute(
            select(Trip).where(Trip.user_id == user.id).order_by(Trip.id.desc())
        )
        trips = list(res.scalars().all())

        totals_res = await session.execute(
            select(
                Trip.id,
                func.count(Receipt.id).filter(Receipt.deleted_at.is_(None)).label("receipts"),
                func.coalesce(
                    func.sum(Expense.total).filter(Receipt.deleted_at.is_(None)), 0
                ).label("total"),
            )
            .select_from(Trip)
            .outerjoin(Receipt, Receipt.trip_id == Trip.id)
            .outerjoin(Expense, Expense.receipt_id == Receipt.id)
            .where(Trip.user_id == user.id)
            .group_by(Trip.id)
        )
        totals_by_trip = {row.id: (row.receipts, row.total) for row in totals_res.all()}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "trips": trips,
            "totals_by_trip": totals_by_trip,
        },
    )


@router.post("/trips")
async def create_trip(
    request: Request,
    user: CurrentUser,
    name: Annotated[str, Form()],
) -> Response:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    async with session_scope() as session:
        trip = Trip(user_id=user.id, name=name, status="active")
        session.add(trip)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="You already have an active trip."
            ) from None
        trip_id = trip.id
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


async def _load_trip_for_user(trip_id: int, user_id: int) -> Trip:
    async with session_scope() as session:
        res = await session.execute(
            select(Trip)
            .options(selectinload(Trip.receipts).selectinload(Receipt.expense))
            .where(Trip.id == trip_id, Trip.user_id == user_id)
        )
        trip = res.scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    return trip


@router.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_detail(
    trip_id: int, request: Request, user: CurrentUser
) -> Response:
    trip = await _load_trip_for_user(trip_id, user.id)
    active_receipts = [r for r in trip.receipts if r.deleted_at is None]
    total_by_currency: dict[str, Decimal] = {}
    for r in active_receipts:
        e = r.expense
        if e and e.total is not None and e.currency:
            total_by_currency[e.currency] = total_by_currency.get(e.currency, Decimal(0)) + Decimal(
                str(e.total)
            )
    return templates.TemplateResponse(
        request,
        "trip_detail.html",
        {
            "user": user,
            "trip": trip,
            "receipts": active_receipts,
            "totals": total_by_currency,
            "categories": ALLOWED_CATEGORIES,
        },
    )


@router.post("/trips/{trip_id}/end")
async def end_trip(
    trip_id: int, request: Request, user: CurrentUser
) -> Response:
    from app import end_trip_flow

    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.user_id != user.id:
            raise HTTPException(status_code=404, detail="trip not found")
        if trip.status != "active":
            return RedirectResponse(
                url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
            )

    tg_app = request.app.state.tg_app
    asyncio.create_task(end_trip_flow.kick_off(trip_id, tg_app.bot))
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/trips/{trip_id}/rename")
async def rename_trip(
    trip_id: int,
    user: CurrentUser,
    name: Annotated[str, Form()],
) -> Response:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.user_id != user.id:
            raise HTTPException(status_code=404, detail="trip not found")
        trip.name = name
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"invalid number: {value}") from None


def _validate_date(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from None
    return v


def _validate_currency(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().upper()
    if not v:
        return None
    if len(v) != 3 or not v.isalpha():
        raise HTTPException(status_code=400, detail="currency must be 3 letters")
    return v


def _validate_category(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v and v not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    return v or None


@router.post("/trips/{trip_id}/expenses")
async def create_manual_expense(
    trip_id: int,
    request: Request,
    user: CurrentUser,
    vendor: Annotated[str | None, Form()] = None,
    date: Annotated[str | None, Form()] = None,
    total: Annotated[str | None, Form()] = None,
    subtotal: Annotated[str | None, Form()] = None,
    vat: Annotated[str | None, Form()] = None,
    currency: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> Response:
    date_v = _validate_date(date)
    total_v = _parse_decimal(total)
    subtotal_v = _parse_decimal(subtotal)
    vat_v = _parse_decimal(vat)
    currency_v = _validate_currency(currency)
    category_v = _validate_category(category) or "other"

    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.user_id != user.id:
            raise HTTPException(status_code=404, detail="trip not found")
        next_seq = await _next_seq(session, trip_id)
        receipt = Receipt(
            trip_id=trip_id,
            seq=next_seq,
            telegram_file_id=None,
            s3_key=None,
            source="portal_manual",
            status="manual",
            note=(note or None),
        )
        session.add(receipt)
        await session.flush()
        session.add(
            Expense(
                receipt_id=receipt.id,
                vendor=(vendor or None),
                date=date_v,
                total=total_v,
                subtotal=subtotal_v,
                vat=vat_v,
                currency=currency_v,
                category=category_v,
            )
        )
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


async def _next_seq(session: Any, trip_id: int) -> int:
    res = await session.execute(
        select(Receipt.seq)
        .where(Receipt.trip_id == trip_id)
        .order_by(Receipt.seq.desc())
        .limit(1)
    )
    last = res.scalar_one_or_none()
    return (last or 0) + 1


@router.post("/trips/{trip_id}/upload")
async def upload_receipt(
    trip_id: int,
    request: Request,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    note: Annotated[str | None, Form()] = None,
) -> Response:
    settings = get_settings()
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="unsupported image type")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > settings.portal_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large")

    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.user_id != user.id:
            raise HTTPException(status_code=404, detail="trip not found")
        next_seq = await _next_seq(session, trip_id)
        receipt = Receipt(
            trip_id=trip_id,
            seq=next_seq,
            telegram_file_id=None,
            s3_key="",
            source="portal_upload",
            status="pending",
            note=(note or None),
        )
        session.add(receipt)
        await session.flush()
        s3_key = s3mod.receipt_key(user.id, trip_id, receipt.id)
        receipt.s3_key = s3_key
        receipt_id = receipt.id

    try:
        await s3mod.upload_bytes(
            settings.s3_bucket_receipts,
            s3_key,
            raw,
            content_type=file.content_type or "image/jpeg",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("portal_upload_failed", receipt_id=receipt_id, error=str(exc))
        async with session_scope() as session:
            r = await session.get(Receipt, receipt_id)
            if r:
                r.status = "failed"
        raise HTTPException(status_code=502, detail="upload failed") from None

    asyncio.create_task(_extract_single_receipt(receipt_id, raw, file.content_type or "image/jpeg"))
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


async def _extract_single_receipt(receipt_id: int, image_bytes: bytes, media_type: str) -> None:
    """Run a synchronous Claude extraction for a portal-uploaded receipt."""
    from app.ai import batch as batch_mod

    try:
        ex = await batch_mod.sync_extract_with_retry(
            batch_mod.ReceiptInput(
                receipt_id=receipt_id, image_bytes=image_bytes, media_type=media_type
            ),
            retries=1,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("portal_extract_failed", receipt_id=receipt_id, error=str(exc))
        async with session_scope() as session:
            r = await session.get(Receipt, receipt_id)
            if r:
                r.status = "failed"
            stmt = (
                pg_insert(Expense)
                .values(receipt_id=receipt_id, raw_json={"portal_error": str(exc)})
                .on_conflict_do_update(
                    index_elements=[Expense.receipt_id],
                    set_={"raw_json": {"portal_error": str(exc)}},
                )
            )
            await session.execute(stmt)
        return

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
                raw_json={"portal_sync": True},
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
                    "raw_json": {"portal_sync": True},
                },
            )
        )
        await session.execute(stmt)
        r = await session.get(Receipt, receipt_id)
        if r:
            r.status = "processed"


@router.post("/expenses/{receipt_id}/edit")
async def edit_expense(
    receipt_id: int,
    request: Request,
    user: CurrentUser,
    vendor: Annotated[str | None, Form()] = None,
    date: Annotated[str | None, Form()] = None,
    total: Annotated[str | None, Form()] = None,
    subtotal: Annotated[str | None, Form()] = None,
    vat: Annotated[str | None, Form()] = None,
    currency: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> Response:
    date_v = _validate_date(date)
    total_v = _parse_decimal(total)
    subtotal_v = _parse_decimal(subtotal)
    vat_v = _parse_decimal(vat)
    currency_v = _validate_currency(currency)
    category_v = _validate_category(category)

    async with session_scope() as session:
        res = await session.execute(
            select(Receipt, Trip)
            .join(Trip, Trip.id == Receipt.trip_id)
            .where(Receipt.id == receipt_id, Trip.user_id == user.id)
        )
        row = res.first()
        if not row:
            raise HTTPException(status_code=404, detail="expense not found")
        receipt, trip = row
        receipt.note = note or None
        stmt = (
            pg_insert(Expense)
            .values(
                receipt_id=receipt_id,
                vendor=(vendor or None),
                date=date_v,
                total=total_v,
                subtotal=subtotal_v,
                vat=vat_v,
                currency=currency_v,
                category=category_v,
            )
            .on_conflict_do_update(
                index_elements=[Expense.receipt_id],
                set_={
                    "vendor": (vendor or None),
                    "date": date_v,
                    "total": total_v,
                    "subtotal": subtotal_v,
                    "vat": vat_v,
                    "currency": currency_v,
                    "category": category_v,
                },
            )
        )
        await session.execute(stmt)
        trip_id = trip.id

    if request.headers.get("HX-Request"):
        return await _row_partial(receipt_id, user.id, request)
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/expenses/{receipt_id}/row", response_class=HTMLResponse)
async def expense_row(
    receipt_id: int, request: Request, user: CurrentUser
) -> Response:
    return await _row_partial(receipt_id, user.id, request)


@router.get("/expenses/{receipt_id}/edit", response_class=HTMLResponse)
async def expense_row_editor(
    receipt_id: int, request: Request, user: CurrentUser
) -> Response:
    async with session_scope() as session:
        res = await session.execute(
            select(Receipt)
            .options(selectinload(Receipt.expense))
            .join(Trip, Trip.id == Receipt.trip_id)
            .where(Receipt.id == receipt_id, Trip.user_id == user.id)
        )
        receipt = res.scalar_one_or_none()
    if not receipt:
        raise HTTPException(status_code=404, detail="not found")
    return templates.TemplateResponse(
        request,
        "_expense_row_edit.html",
        {"receipt": receipt, "categories": ALLOWED_CATEGORIES},
    )


async def _row_partial(receipt_id: int, user_id: int, request: Request) -> Response:
    async with session_scope() as session:
        res = await session.execute(
            select(Receipt)
            .options(selectinload(Receipt.expense))
            .join(Trip, Trip.id == Receipt.trip_id)
            .where(Receipt.id == receipt_id, Trip.user_id == user_id)
        )
        receipt = res.scalar_one_or_none()
    if not receipt:
        raise HTTPException(status_code=404, detail="not found")
    return templates.TemplateResponse(
        request,
        "_expense_row.html",
        {"receipt": receipt},
    )


@router.post("/expenses/{receipt_id}/delete")
async def delete_expense(
    receipt_id: int, request: Request, user: CurrentUser
) -> Response:
    settings = get_settings()
    async with session_scope() as session:
        res = await session.execute(
            select(Receipt, Trip)
            .join(Trip, Trip.id == Receipt.trip_id)
            .where(Receipt.id == receipt_id, Trip.user_id == user.id)
        )
        row = res.first()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        receipt, trip = row
        receipt.deleted_at = datetime.now(UTC)
        s3_key = receipt.s3_key
        trip_id = trip.id

    if s3_key:
        try:
            await s3mod.delete_object(settings.s3_bucket_receipts, s3_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("portal_s3_delete_failed", receipt_id=receipt_id, error=str(exc))

    if request.headers.get("HX-Request"):
        return Response(status_code=200)
    return RedirectResponse(
        url=f"/portal/trips/{trip_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/receipts/{receipt_id}/image")
async def receipt_image(
    receipt_id: int, user: CurrentUser
) -> Response:
    settings = get_settings()
    async with session_scope() as session:
        res = await session.execute(
            select(Receipt)
            .join(Trip, Trip.id == Receipt.trip_id)
            .where(Receipt.id == receipt_id, Trip.user_id == user.id)
        )
        receipt = res.scalar_one_or_none()
    if not receipt or not receipt.s3_key:
        raise HTTPException(status_code=404, detail="no image")
    url = await s3mod.presign_url(
        settings.s3_bucket_receipts, receipt.s3_key, settings.presign_ttl_seconds
    )
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/trips/{trip_id}/reports/{filename}")
async def trip_report_file(
    trip_id: int, filename: str, user: CurrentUser
) -> Response:
    settings = get_settings()
    async with session_scope() as session:
        trip = await session.get(Trip, trip_id)
        if not trip or trip.user_id != user.id:
            raise HTTPException(status_code=404, detail="trip not found")
        keys = trip.report_keys or {}
        key = keys.get(filename)
    if not key:
        raise HTTPException(status_code=404, detail="report not found")
    url = await s3mod.presign_url(
        settings.s3_bucket_reports, key, settings.presign_ttl_seconds
    )
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/expenses", response_class=HTMLResponse)
async def expenses_index(
    request: Request,
    user: CurrentUser,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    currency: str | None = None,
) -> Response:
    date_from_v = _validate_date(date_from)
    date_to_v = _validate_date(date_to)
    category_v = _validate_category(category)
    currency_v = _validate_currency(currency)

    async with session_scope() as session:
        stmt = (
            select(Receipt, Expense, Trip)
            .join(Trip, Trip.id == Receipt.trip_id)
            .outerjoin(Expense, Expense.receipt_id == Receipt.id)
            .where(Trip.user_id == user.id, Receipt.deleted_at.is_(None))
            .order_by(Trip.id.desc(), Receipt.seq.desc())
        )
        if date_from_v:
            stmt = stmt.where(Expense.date >= date_from_v)
        if date_to_v:
            stmt = stmt.where(Expense.date <= date_to_v)
        if category_v:
            stmt = stmt.where(Expense.category == category_v)
        if currency_v:
            stmt = stmt.where(Expense.currency == currency_v)
        res = await session.execute(stmt)
        rows = list(res.all())

    totals: dict[str, Decimal] = {}
    for _, expense, _trip in rows:
        if expense and expense.total is not None and expense.currency:
            totals[expense.currency] = totals.get(expense.currency, Decimal(0)) + Decimal(
                str(expense.total)
            )

    return templates.TemplateResponse(
        request,
        "expenses.html",
        {
            "user": user,
            "rows": rows,
            "totals": totals,
            "categories": ALLOWED_CATEGORIES,
            "filters": {
                "date_from": date_from_v or "",
                "date_to": date_to_v or "",
                "category": category_v or "",
                "currency": currency_v or "",
            },
        },
    )
