"""Bot command list shown in the Telegram menu."""

from __future__ import annotations

from telegram import BotCommand


COMMANDS: list[BotCommand] = [
    BotCommand("start", "Show welcome message"),
    BotCommand("start_trip", "Start a new trip: /start_trip <name>"),
    BotCommand("note", "Attach a note to the most recent receipt"),
    BotCommand("list", "List receipts of the active trip"),
    BotCommand("cancel", "Soft-delete a receipt: /cancel <n>"),
    BotCommand("end_trip", "Process trip and send the report"),
]

