"""Handlers de Telegram para comandos y audios."""

import os
import tempfile
import logging
import asyncio
import math
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from config import (
    MAX_FILE_SIZE_MB,
    MAX_FILE_SIZE_BYTES,
    LONG_AUDIO_THRESHOLD_SECONDS,
    AUDIO_CHUNK_SECONDS,
    AUDIO_CHUNK_OVERLAP_SECONDS,
    PROCESSING_CONCURRENCY,
    PROCESSING_TIMEOUT_SECONDS,
    GROQ_TIMEOUT_SECONDS,
    TRANSCRIBE_MAX_RETRIES,
    RETRY_BASE_SECONDS,
    SUMMARY_TIMEOUT_SECONDS,
    SUMMARY_MAX_RETRIES,
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

OUTPUT_MODE_LABELS = {
    "transcription": "solo transcripción",
    "summary": "solo resumen",
    "both": "transcripción y resumen",
}
OUTPUT_MODE_BUTTON_TEXT = {
    "transcription": "Solo transcripción",
    "summary": "Solo resumen",
    "both": "Transcripción y resumen",
}


def _output_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Obtiene el modo de salida, migrando la preferencia anterior si existe."""
    mode = context.user_data.get("output_mode")
    if mode in OUTPUT_MODE_LABELS:
        return mode
    return "both" if context.user_data.get("summary_enabled", True) else "transcription"


def _mode_inline_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """Construye botones en línea marcando con ✅ el modo activo."""
    rows = []
    for mode in ("transcription", "summary", "both"):
        text = OUTPUT_MODE_BUTTON_TEXT[mode]
        if mode == current_mode:
            text = f"✅ {text}"
        rows.append([InlineKeyboardButton(text, callback_data=f"mode:{mode}")])
    return InlineKeyboardMarkup(rows)


def _mode_selection_text(current_mode: str) -> str:
    """Texto que acompaña a los botones, indicando el modo activo."""
    return (
        "Elige el resultado que quieres recibir para tus próximos audios.\n"
        f"Modo actual: *{OUTPUT_MODE_LABELS[current_mode]}*."
    )


def _retry_backoff_total(retries: int) -> float:
    """Suma del backoff lineal de reintentos (sin contar el último intento)."""
    if retries <= 1:
        return 0.0
    return RETRY_BASE_SECONDS * sum(range(1, retries))


def _estimate_transcription_timeout(duration_seconds: int) -> float:
    """
    Estima timeout de transcripción según duración y estrategia (single/chunks).
    Evita cortar audios grandes válidos y mantiene un guardarraíl mínimo global.
    """
    per_attempt_budget = float(GROQ_TIMEOUT_SECONDS)
    retries_budget = per_attempt_budget * float(TRANSCRIBE_MAX_RETRIES)
    backoff_budget = _retry_backoff_total(TRANSCRIBE_MAX_RETRIES)

    if duration_seconds < LONG_AUDIO_THRESHOLD_SECONDS:
        estimated = retries_budget + backoff_budget + 30.0
        return max(float(PROCESSING_TIMEOUT_SECONDS), estimated)

    chunk_step = max(1, AUDIO_CHUNK_SECONDS - AUDIO_CHUNK_OVERLAP_SECONDS)
    chunk_count = max(1, math.ceil(float(duration_seconds) / float(chunk_step)))
    per_chunk_budget = retries_budget + backoff_budget + 5.0
    estimated = (chunk_count * per_chunk_budget) + 45.0  # margen de preparación ffmpeg
    return max(float(PROCESSING_TIMEOUT_SECONDS), estimated)


def _estimate_summary_timeout() -> float:
    """Timeout máximo esperable del resumen con su propio retry policy."""
    retries_budget = float(SUMMARY_TIMEOUT_SECONDS) * float(SUMMARY_MAX_RETRIES)
    backoff_budget = _retry_backoff_total(SUMMARY_MAX_RETRIES)
    return retries_budget + backoff_budget + 10.0


# ── Comandos ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler del comando /start."""
    context.user_data.setdefault("output_mode", "both")
    current_mode = _output_mode(context)
    await update.message.reply_text(
        "🎙️ **Bot de Transcripción de Audios**\n\n"
        "Envía una nota de voz o archivo de audio y recibirás la transcripción.\n\n"
        f"{_mode_selection_text(current_mode)}\n\n"
        "Comandos:\n"
        "  /modo — Cambiar el resultado que quieres recibir\n"
        "  /ayuda — Ver ayuda",
        parse_mode="Markdown",
        reply_markup=_mode_inline_keyboard(current_mode),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler del comando /ayuda o /help."""
    msg = (
        "📖 **Comandos Disponibles**\n\n"
        "  **/modo** — Elegir transcripción, resumen o ambos\n"
        "  **/ayuda** — Esta ayuda\n\n"
        "**Formatos aceptados:**\n"
        "  • Notas de voz 🎙️\n"
        "  • MP3, M4A, WAV, OGG, FLAC 🎵\n"
        "  • MP4 con audio 🎬\n\n"
        f"**Límite de tamaño:** {MAX_FILE_SIZE_MB} MB\n\n"
        "**Procesamiento:**\n"
        "  • Audios cortos: transcripción instantánea\n"
        "  • Audios largos (> 6 min): procesamiento automático en trozos\n"
        "  • Resúmenes: según el modo seleccionado"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el selector de resultado del audio con el modo activo marcado."""
    current_mode = _output_mode(context)
    await update.message.reply_text(
        _mode_selection_text(current_mode),
        parse_mode="Markdown",
        reply_markup=_mode_inline_keyboard(current_mode),
    )


async def handle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Aplica el modo elegido desde los botones en línea y refresca el mensaje."""
    query = update.callback_query
    if not query or not query.data:
        return

    mode = query.data.split("mode:", 1)[-1]
    if mode not in OUTPUT_MODE_LABELS:
        await query.answer()
        return

    context.user_data["output_mode"] = mode
    await query.answer(f"Modo: {OUTPUT_MODE_LABELS[mode]}")

    try:
        await query.edit_message_text(
            _mode_selection_text(mode),
            parse_mode="Markdown",
            reply_markup=_mode_inline_keyboard(mode),
        )
    except TelegramError:
        pass


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

            # Transcribir (con timeout dinámico por duración/chunks para no
            # cortar audios grandes válidos ni dejar esperas indefinidas).
            async def _do_transcribe():
                if duration >= LONG_AUDIO_THRESHOLD_SECONDS:
                    if not _ffmpeg_is_available():
                        return None, None, "no_ffmpeg"

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
                    plain, formatted = await transcribe(tmp_path, status_msg=status_msg)
                return plain, formatted, None

            try:
                transcription_timeout = _estimate_transcription_timeout(duration)
                plain, formatted, special = await asyncio.wait_for(
                    _do_transcribe(), timeout=transcription_timeout
                )
            except asyncio.TimeoutError:
                await safe_edit(
                    status_msg,
                    "⏱️ La transcripción tardó demasiado y se canceló.\n"
                    "Prueba con un audio más corto o inténtalo de nuevo.",
                )
                return

            if special == "no_ffmpeg":
                await safe_edit(
                    status_msg,
                    f"❌ No se puede procesar audios largos (> {LONG_AUDIO_THRESHOLD_SECONDS // 60} min) ahora.\n"
                    "ffmpeg no está disponible en el servidor.",
                )
                return

            # Validar que se obtuvo transcripción
            if not plain:
                await safe_edit(status_msg, "❌ No se detectó voz en el audio.")
                return

            output_mode = _output_mode(context)

            # Eliminar mensaje de estado y mostrar transcripción si corresponde.
            await safe_delete(status_msg)
            status_msg = None

            last_msg: Message = message
            if output_mode in ("transcription", "both"):
                streamed_message = await stream_text(message, formatted)
                if streamed_message:
                    last_msg = streamed_message

            # Generar resumen cuando el modo seleccionado lo requiere.
            if output_mode in ("summary", "both"):
                summary_status = await last_msg.reply_text(
                    "🤖 Preparando resumen...",
                )

                try:
                    summary_timeout = _estimate_summary_timeout()
                    summary = await asyncio.wait_for(
                        summarize(formatted),
                        timeout=summary_timeout,
                    )
                    await summary_status.edit_text(
                        f"📌 **Resumen:**\n\n{summary}",
                        parse_mode="Markdown",
                    )
                except asyncio.TimeoutError:
                    logger.error("Timeout al generar resumen")
                    await safe_delete(summary_status)
                    await last_msg.reply_text(
                        "⚠️ No se pudo generar el resumen porque la solicitud tardó demasiado.\n"
                        "La transcripción está arriba."
                    )
                except Exception as e:
                    logger.error("Error al generar resumen: %s", e)
                    await safe_delete(summary_status)
                    await last_msg.reply_text(
                        "⚠️ No se pudo generar el resumen por un error del servicio.\n"
                        "La transcripción está arriba."
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
