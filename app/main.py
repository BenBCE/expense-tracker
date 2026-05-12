"""FastAPI app: Telegram webhook + healthcheck + APScheduler lifespan."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from telegram import MenuButtonCommands, Update
from telegram.ext import Application, ApplicationBuilder

from app.bot.handlers import register_handlers
from app.bot.keyboard import COMMANDS
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging_setup import configure_logging, get_logger
from app.scheduler import build_scheduler
from app.web.auth import RedirectToLoginError
from app.web.routes import router as portal_router


log = get_logger(__name__)


def _build_application() -> Application:
    settings = get_settings()
    app = ApplicationBuilder().token(settings.telegram_bot_token).updater(None).build()
    register_handlers(app)
    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    tg_app = _build_application()
    await tg_app.initialize()
    await tg_app.start()
    try:
        await tg_app.bot.set_my_commands(COMMANDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("set_my_commands_failed", error=str(exc))
    try:
        await tg_app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as exc:  # noqa: BLE001
        log.warning("set_chat_menu_button_failed", error=str(exc))
    try:
        await tg_app.bot.set_webhook(
            url=f"{settings.public_base_url.rstrip('/')}/tg",
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("set_webhook_failed", error=str(exc))

    scheduler = build_scheduler(tg_app.bot)
    scheduler.start()

    app.state.tg_app = tg_app
    app.state.scheduler = scheduler

    log.info("app_started")
    try:
        yield
    finally:
        log.info("app_stopping")
        scheduler.shutdown(wait=False)
        await tg_app.stop()
        await tg_app.shutdown()
        await dispose_engine()


app = FastAPI(title="expense-tracker", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret,
    max_age=_settings.session_max_age_seconds,
    same_site="lax",
    https_only=False,
    session_cookie="portal_session",
)
app.include_router(portal_router, prefix="/portal")


@app.exception_handler(RedirectToLoginError)
async def _redirect_to_login(_: Request, __: RedirectToLoginError) -> Response:
    return RedirectResponse(url="/portal/login", status_code=303)


@app.middleware("http")
async def _request_id_mw(request: Request, call_next):  # type: ignore[no-untyped-def]
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=rid, path=request.url.path)
    response: Response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tg")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="invalid secret token")
    payload = await request.json()
    tg_app: Application = request.app.state.tg_app
    update = Update.de_json(payload, tg_app.bot)
    await tg_app.update_queue.put(update)
    return Response(status_code=204)
