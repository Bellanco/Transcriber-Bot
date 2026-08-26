"""Handlers de Telegram para comandos y audios."""

import os
import tempfile
import logging
import asyncio
from pathlib import Path
from typing import Optional

from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from config import (
    MAX_FILE_SIZE_MB,
    MAX_FILE_SIZE_BYTES,
    SUMMARY_MIN_SECONDS,
    LONG_AUDIO_THRESHOLD_SECONDS,
    MAX_SUMMARY_INPUT,
    PROCESSING_CONCURRENCY,
)
from utils import (
    safe_edit,
    safe_delete,
    validate_downloaded_file,
    format_seconds,
)
from transcriber import (
    transcribe,
    transcribe_long_audio,
    _ffmpeg_is_available,
)
from summarizer import summarize
from formatter import stream_text

logger = logging.getLogger(__name__)

processing_semaphore = asyncio.Semaphore(PROCESSING_CONCURRENCY)


# ── Comandos ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler del comando /start."""
    context.user_data.setdefault("summary_enabled", True)
    await update.message.reply_text(
        "🎙️ **Bot de Transcripción de Audios**\n\n"
        "Envía una nota de voz o archivo de audio y recibirás la transcripción.\n\n"
        "Si el audio supera los 40 segundos, también recibirás un resumen.\n\n"
        "Comandos:\n"
        "  /modo — Activar/desactivar resúmenes automáticos\n"
        "  /ayuda — Ver ayuda",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler del comando /ayuda o /help."""
    msg = (
        "📖 **Comandos Disponibles**\n\n"
        "  **/modo** — Activar o desactivar resúmenes automáticos\n"
        "  **/ayuda** — Esta ayuda\n\n"
        "**Formatos aceptados:**\n"
        "  • Notas de voz 🎙️\n"
        "  • MP3, M4A, WAV, OGG, FLAC 🎵\n"
        "  • MP4 con audio 🎬\n\n"
        f"**Límite de tamaño:** {MAX_FILE_SIZE_MB} MB\n\n"
        "**Procesamiento:**\n"
        "  • Audios cortos: transcripción instantánea\n"
        "  • Audios largos (> 20 min): procesamiento automático en trozos\n"
        "  • Resúmenes: disponibles para audios > 40 segundos"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler del comando /modo para alternar resúmenes."""
    current = context.user_data.get("summary_enabled", True)
    context.user_data["summary_enabled"] = not current
    state = "✅ activados" if context.user_data["summary_enabled"] else "❌ desactivados"
    await update.message.reply_text(f"Resúmenes {state}.")


# ── Handler de Audio ──────────────────────────────────────────────────────────


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler principal para audios (voces, audios, video notas)."""
    message = update.message
    if not message:
        return

    # Determinar tipo de audio y extraer metadata
    if message.voice:
        tg_file = await context.bot.get_file(message.voice.file_id)
        ext = "ogg"
        duration = message.voice.duration or 0
        size = message.voice.file_size or 0
        audio_type = "🎙️ Nota de voz"
    elif message.audio:
        tg_file = await context.bot.get_file(message.audio.file_id)
        filename = message.audio.file_name or ""
        ext = Path(filename).suffix.lstrip(".").lower() or "mp3"
        duration = message.audio.duration or 0
        size = message.audio.file_size or 0
        audio_type = "🎵 Audio"
    elif message.video_note:
        tg_file = await context.bot.get_file(message.video_note.file_id)
        ext = "mp4"
        duration = message.video_note.duration or 0
        size = message.video_note.file_size or 0
        audio_type = "🎬 Video Nota"
    else:
        return

    # Validar tamaño
    if size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"❌ El archivo supera el límite de {MAX_FILE_SIZE_MB} MB.\n\n"
            f"Tamaño actual: {size / (1024 * 1024):.1f} MB"
        )
        return

    # Crear mensaje de estado
    duration_str = format_seconds(duration)
    status_msg = await message.reply_text(
        f"⏳ Procesando tu {audio_type.lower()}...\n"
        f"Duración: {duration_str}"
    )
    tmp_path: Optional[str] = None

    try:
        async with processing_semaphore:
            # Descargar archivo
            await safe_edit(status_msg, f"📥 Descargando archivo ({size / (1024 * 1024):.1f} MB)...")

            with tempfile.NamedTemporaryFile(
                suffix=f".{ext}", delete=False
            ) as tmp:
                tmp_path = tmp.name

            await tg_file.download_to_drive(tmp_path)

            # Validar que se descargó correctamente
            is_valid, error_msg = validate_downloaded_file(tmp_path)
            if not is_valid:
                await safe_edit(
                    status_msg,
                    f"❌ Error al descargar: {error_msg}",
                )
                return

            # Transcribir
            if duration >= LONG_AUDIO_THRESHOLD_SECONDS:
                if not _ffmpeg_is_available():
                    await safe_edit(
                        status_msg,
                        "❌ No se puede procesar audios > 20 min ahora.\n"
                        "ffmpeg no está disponible en el servidor.",
                    )
                    return

                await safe_edit(
                    status_msg,
                    f"🔧 Preparando audio largo ({duration_str})...\n"
                    "Dividiendo en trozos...",
                )
                plain, formatted = await transcribe_long_audio(
                    tmp_path, duration, status_msg=status_msg
                )
            else:
                await safe_edit(status_msg, "🎙️ Transcribiendo...")
                plain, formatted = await transcribe(tmp_path)

            # Validar que se obtuvo transcripción
            if not plain:
                await safe_edit(status_msg, "❌ No se detectó voz en el audio.")
                return

            # Eliminar mensaje de estado y mostrar transcripción
            await safe_delete(status_msg)
            status_msg = None

            last_msg = await stream_text(message, formatted)

            # Resumen (si está habilitado y el audio es suficientemente largo)
            summary_enabled = context.user_data.get("summary_enabled", True)
            if summary_enabled and duration >= SUMMARY_MIN_SECONDS:
                summary_status = await last_msg.reply_text(
                    "🤖 Preparando resumen...",
                )

                try:
                    summary = await summarize(plain[: MAX_SUMMARY_INPUT])
                    await summary_status.edit_text(
                        f"📌 **Resumen:**\n\n{summary}",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error("Error al generar resumen: %s", e)
                    await safe_edit(
                        summary_status,
                        "⚠️ No se pudo generar el resumen.\n"
                        "La transcripción está arriba.",
                    )

    except Exception as e:
        logger.exception("Error procesando audio: %s", e)
        error_msg = str(e)

        if "rate_limit" in error_msg.lower():
            await safe_edit(
                status_msg,
                "⏱️ El servicio está saturado.\n"
                "Espera unos segundos e inténtalo de nuevo.",
            )
        elif "timeout" in error_msg.lower():
            await safe_edit(
                status_msg,
                "⏱️ La transcripción tardó demasiado.\n"
                "Prueba con un audio más corto.",
            )
        elif "unauthorized" in error_msg.lower() or "401" in error_msg.lower():
            await safe_edit(
                status_msg,
                "❌ Error de configuración: GROQ_API_KEY inválida.\n"
                "Contacta al administrador.",
            )
        elif "ffmpeg" in error_msg.lower():
            await safe_edit(
                status_msg,
                "❌ Error procesando audio.\n"
                "Intenta con un audio más corto.",
            )
        else:
            await safe_edit(
                status_msg,
                f"❌ Error inesperado:\n`{error_msg[:100]}`",
            )

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler para mensajes de texto que no son comandos."""
    await update.message.reply_text(
        "👋 Envía una nota de voz o archivo de audio para transcribirlo.\n\n"
        "Usa /ayuda para ver más información.",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler global de errores no capturados."""
    logger.exception("Error no controlado", exc_info=context.error)
