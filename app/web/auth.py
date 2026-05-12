"""Telegram Login Widget verification + session helpers."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import get_settings


class RedirectToLoginError(Exception):
    """Sentinel raised by current_user when the session is missing."""


@dataclass(slots=True)
class TelegramUser:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


def verify_telegram_login(data: dict[str, Any], bot_token: str, max_age: int) -> TelegramUser:
    """Verify the hash of a Telegram Login Widget callback payload.

    See https://core.telegram.org/widgets/login#checking-authorization.
    """
    if "hash" not in data or "id" not in data or "auth_date" not in data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing fields")

    received_hash = str(data["hash"])
    pairs = sorted(
        f"{k}={v}" for k, v in data.items() if k != "hash" and v is not None and v != ""
    )
    data_check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad hash")

    try:
        auth_date = int(data["auth_date"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad auth_date") from None
    if time.time() - auth_date > max_age:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login expired")

    try:
        user_id = int(data["id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from None

    return TelegramUser(
        id=user_id,
        first_name=str(data.get("first_name") or "") or None,
        last_name=str(data.get("last_name") or "") or None,
        username=str(data.get("username") or "") or None,
        photo_url=str(data.get("photo_url") or "") or None,
    )


def current_user(request: Request) -> TelegramUser:
    sess = request.session
    uid = sess.get("user_id")
    if not uid:
        raise RedirectToLoginError()
    settings = get_settings()
    if settings.telegram_allowed_user_ids and int(uid) not in settings.telegram_allowed_user_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return TelegramUser(
        id=int(uid),
        first_name=sess.get("first_name"),
        last_name=sess.get("last_name"),
        username=sess.get("username"),
        photo_url=sess.get("photo_url"),
    )


def store_user_in_session(request: Request, user: TelegramUser) -> None:
    request.session["user_id"] = user.id
    request.session["first_name"] = user.first_name
    request.session["last_name"] = user.last_name
    request.session["username"] = user.username
    request.session["photo_url"] = user.photo_url
