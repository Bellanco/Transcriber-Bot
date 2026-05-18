"""
Bot de Telegram — Transcripción y Resumen de Audios (versión Groq, GRATUITA)
=============================================================================
Dependencias:
    pip install python-telegram-bot groq

Variables de entorno necesarias:
    TELEGRAM_TOKEN  — Token del bot (BotFather)
    GROQ_API_KEY    — Clave de Groq (gratuita en console.groq.com)

Modelos usados (ambos gratuitos en Groq):
    Transcripción : whisper-large-v3
    Resumen       : llama-3.3-70b-versatile
"""

import os
import logging
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError
from groq import Groq, APIError, RateLimitError, APITimeoutError

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")

TRANSCRIPTION_MODEL = "whisper-large-v3"
SUMMARY_MODEL       = "llama-3.3-70b-versatile"

# El resumen solo se genera si el audio supera este umbral (en segundos)
SUMMARY_MIN_SECONDS = 40

# Groq limita el tamaño de audio a 25 MB
MAX_FILE_SIZE_MB = 25
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Modos de respuesta
MODE_TRANSCRIBE = "transcribir"
MODE_BOTH       = "resumir"

# ── Cliente Groq ──────────────────────────────────────────────────────────────

groq_client = Groq(api_key=GROQ_API_KEY)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", MODE_BOTH)


def mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Solo transcripción" + (" ✓" if current_mode == MODE_TRANSCRIBE else ""),
            callback_data=MODE_TRANSCRIBE,
        ),
        InlineKeyboardButton(
            "Resumen" + (" ✓" if current_mode == MODE_BOTH else ""),
            callback_data=MODE_BOTH,
        ),
    ]])


def transcribe(file_path: str) -> str:
    """Transcribe audio con Whisper en Groq (idioma forzado a español)."""
    with open(file_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=f,
            language="es",
            response_format="text",
        )
    # Groq devuelve el texto directamente cuando response_format="text"
    return (result or "").strip()


def summarize(text: str) -> str:
    """Genera un resumen en español con Llama via Groq."""
    response = groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.3,   # Menos creatividad → más fiel al original
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que resume textos en español de forma clara y concisa. "
                    "Responde SOLO con el resumen, sin saludos ni explicaciones adicionales. "
                    "Usa viñetas (•) si hay varios puntos clave."
                ),
            },
            {
                "role": "user",
                "content": f"Resume este texto:\n\n{text}",
            },
        ],
    )
    return response.choices[0].message.content.strip()


def format_duration(seconds: int) -> str:
    """Formatea segundos como '1 min 23 s' o '45 s'."""
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


# ── Handlers de comandos ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    await update.message.reply_text(
        "¡Hola! Soy tu asistente de transcripción de audios.\n\n"
        "Envíame una *nota de voz* o *archivo de audio* y te lo transcribo al instante.\n"
        "Si dura más de 40 segundos, también puedo hacerte un resumen.\n\n"
        "Todo funciona de forma *gratuita* gracias a Groq. 🚀",
        parse_mode="Markdown",
        reply_markup=mode_keyboard(mode),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    await update.message.reply_text(
        "*Comandos*\n\n"
        "/start — Inicio\n"
        "/modo  — Cambiar modo\n"
        "/ayuda — Esta ayuda\n\n"
        "*Formatos aceptados:*\n"
        "Notas de voz · MP3 · M4A · WAV · OGG · FLAC · MP4 (videomensaje)\n\n"
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB",
        parse_mode="Markdown",
        reply_markup=mode_keyboard(mode),
    )


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    await update.message.reply_text(
        "Selecciona el modo de respuesta:",
        reply_markup=mode_keyboard(mode),
    )


# ── Handler de botones inline ─────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    new_mode = query.data
    context.user_data["mode"] = new_mode

    label = "Solo transcripción" if new_mode == MODE_TRANSCRIBE else "Transcripción + resumen"
    await query.edit_message_text(
        f"Modo actualizado: *{label}*\n\n"
        "Listo, envíame un audio cuando quieras.",
        parse_mode="Markdown",
        reply_markup=mode_keyboard(new_mode),
    )


# ── Handler principal de audio ────────────────────────────────────────────────

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    mode    = get_mode(context)

    # ── 1. Identificar tipo de audio y metadatos ──────────────────────────────
    if message.voice:
        tg_file  = await context.bot.get_file(message.voice.file_id)
        ext      = "ogg"
        duration = message.voice.duration
        size     = message.voice.file_size or 0
    elif message.audio:
        tg_file  = await context.bot.get_file(message.audio.file_id)
        filename = message.audio.file_name or ""
        ext      = Path(filename).suffix.lstrip(".").lower() or "mp3"
        duration = message.audio.duration or 0
        size     = message.audio.file_size or 0
    elif message.video_note:
        tg_file  = await context.bot.get_file(message.video_note.file_id)
        ext      = "mp4"
        duration = message.video_note.duration
        size     = message.video_note.file_size or 0
    else:
        return

    # ── 2. Validar tamaño antes de descargar ──────────────────────────────────
    if size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            f"El archivo pesa más de {MAX_FILE_SIZE_MB} MB, que es el límite de Groq.\n"
            "Por favor envía un audio más corto."
        )
        return

    status_msg = await message.reply_text("Descargando audio…")
    tmp_path = None

    try:
        # ── 3. Descargar a fichero temporal ───────────────────────────────────
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        # ── 4. Transcribir ────────────────────────────────────────────────────
        await status_msg.edit_text("Transcribiendo…")
        transcription = transcribe(tmp_path)

        if not transcription:
            await status_msg.edit_text(
                "No detecté voz en el audio. "
                "Comprueba que el volumen sea suficiente o que no esté en silencio."
            )
            return

        # ── 5. Decidir si resumir ─────────────────────────────────────────────
        needs_summary = (mode == MODE_BOTH) and (duration >= SUMMARY_MIN_SECONDS)

        parts = [f"*Transcripción:*\n\n{transcription}"]

        if needs_summary:
            await status_msg.edit_text("📌 Generando resumen…")
            summary = summarize(transcription)
            parts.append(f"*Resumen:*\n\n{summary}")
        elif mode == MODE_BOTH and duration < SUMMARY_MIN_SECONDS:
            parts.append(
                f"_ℹEl audio dura {format_duration(duration)}, "
                f"se necesitan al menos {SUMMARY_MIN_SECONDS} s para generar resumen._"
            )

        # ── 6. Enviar respuesta ───────────────────────────────────────────────
        await status_msg.delete()
        await message.reply_text(
            "\n\n".join(parts),
            parse_mode="Markdown",
            reply_markup=mode_keyboard(mode),
        )

    # ── Gestión de errores específicos de Groq ─────────────────────────────────
    except RateLimitError:
        logger.warning("Groq rate limit alcanzado")
        await status_msg.edit_text(
            "⏱Se ha alcanzado el límite de uso de Groq por ahora.\n"
            "Espera unos segundos y vuelve a intentarlo."
        )
    except APITimeoutError:
        logger.warning("Groq API timeout")
        await status_msg.edit_text(
            "La transcripción tardó demasiado. El audio puede ser muy largo.\n"
            "Prueba con un fragmento más corto."
        )
    except APIError as e:
        logger.error("Groq API error: %s", e)
        await status_msg.edit_text(
            "Error en el servicio de transcripción. Inténtalo de nuevo en unos segundos."
        )
    except TelegramError as e:
        logger.error("Telegram error: %s", e)
        # Si falla al editar el mensaje de estado, no hacer nada más
    except Exception as e:
        logger.exception("Error inesperado procesando audio: %s", e)
        try:
            await status_msg.edit_text(
                "Ocurrió un error inesperado. Por favor inténtalo más tarde."
            )
        except TelegramError:
            pass
    finally:
        # Siempre limpiar el fichero temporal
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Fallback para mensajes de texto ──────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = get_mode(context)
    await update.message.reply_text(
        "Envíame una *nota de voz* o *archivo de audio* para transcribirlo.\n"
        "Usa /ayuda si necesitas más información.",
        parse_mode="Markdown",
        reply_markup=mode_keyboard(mode),
    )


# ── Punto de entrada ──────────────────────────────────────────────────────────

def main() -> None:
    # Validar variables de entorno al arrancar
    missing = [v for v in ("TELEGRAM_TOKEN", "GROQ_API_KEY") if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"Faltan las siguientes variables de entorno: {', '.join(missing)}\n"
            "Consigue tu clave gratuita de Groq en https://console.groq.com"
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ayuda",  cmd_help))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("modo",   cmd_modo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
            handle_audio,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot arrancado. Esperando mensajes…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
