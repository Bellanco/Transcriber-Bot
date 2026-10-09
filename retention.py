"""Caducidad de mensajes y datos locales del bot."""

import logging
import time
from typing import Any

from telegram.error import TelegramError

from config import DATA_RETENTION_SECONDS

logger = logging.getLogger(__name__)

MESSAGE_EXPIRATIONS_KEY = "message_expirations"
LAST_ACTIVITY_KEY = "_last_activity_at"


def mark_user_activity(user_data: dict[str, Any]) -> None:
    """Registra actividad para aplicar la retención de datos locales."""
    user_data[LAST_ACTIVITY_KEY] = time.time()


async def track_message(application: Any, message: Any) -> None:
    """Registra un mensaje para borrarlo 24 horas después de su creación."""
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    expirations = application.bot_data.setdefault(MESSAGE_EXPIRATIONS_KEY, [])
    if any(
        item.get("chat_id") == chat_id and item.get("message_id") == message_id
        for item in expirations
    ):
        return

    expirations.append(
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "expires_at": time.time() + DATA_RETENTION_SECONDS,
        }
    )
    await application.update_persistence()


async def untrack_message(
    application: Any,
    message: Any = None,
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    """Retira del registro un mensaje que ya se borró antes de vencer."""
    if message is not None:
        chat_id = getattr(message, "chat_id", chat_id)
        message_id = getattr(message, "message_id", message_id)
    if chat_id is None or message_id is None:
        return

    expirations = application.bot_data.get(MESSAGE_EXPIRATIONS_KEY, [])
    remaining = [
        item
        for item in expirations
        if item.get("chat_id") != chat_id or item.get("message_id") != message_id
    ]
    if len(remaining) != len(expirations):
        application.bot_data[MESSAGE_EXPIRATIONS_KEY] = remaining
        await application.update_persistence()


async def reply_with_retention(
    message: Any,
    application: Any,
    user_data: dict[str, Any],
    text: str,
    **kwargs: Any,
) -> Any:
    """Envía y registra un mensaje del bot y actualiza actividad del usuario."""
    mark_user_activity(user_data)
    sent_message = await message.reply_text(text, **kwargs)
    await track_message(application, sent_message)
    return sent_message


async def cleanup_expired_data(application: Any, now: float | None = None) -> None:
    """Borra mensajes vencidos y limpia preferencias y reenvíos inactivos."""
    current_time = time.time() if now is None else now
    changed = False
    expirations = application.bot_data.get(MESSAGE_EXPIRATIONS_KEY, [])
    pending_expirations = []

    for item in expirations:
        if item.get("expires_at", 0) > current_time:
            pending_expirations.append(item)
            continue

        try:
            await application.bot.delete_message(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
            )
        except TelegramError as error:
            logger.warning("No se pudo borrar un mensaje vencido: %s", error)
            pending_expirations.append(item)
            continue
        changed = True

    if len(pending_expirations) != len(expirations):
        application.bot_data[MESSAGE_EXPIRATIONS_KEY] = pending_expirations

    for user_data in application.user_data.values():
        last_activity = user_data.get(LAST_ACTIVITY_KEY)
        if last_activity is None:
            user_data[LAST_ACTIVITY_KEY] = current_time
            changed = True
            continue

        if current_time - last_activity >= DATA_RETENTION_SECONDS:
            user_data.clear()
            changed = True
            continue

        pending_audios = user_data.get("pending_forwarded_audios")
        if pending_audios is None:
            continue

        active_audios = [
            audio
            for audio in pending_audios
            if 0 <= current_time - audio.get("queued_at", 0) < DATA_RETENTION_SECONDS
        ]
        if len(active_audios) != len(pending_audios):
            if active_audios:
                user_data["pending_forwarded_audios"] = active_audios
            else:
                user_data.pop("pending_forwarded_audios", None)
            changed = True

    if changed:
        await application.update_persistence()