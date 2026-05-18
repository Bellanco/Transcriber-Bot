import os
import logging
import tempfile
import asyncio
from pathlib import Path
from typing import Optional, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest
from groq import AsyncGroq, APIError, RateLimitError, APITimeoutError

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

TRANSCRIPTION_MODEL = "whisper-large-v3"
SUMMARY_MODEL = "llama-3.3-70b-versatile"

SUMMARY_MIN_SECONDS = 40
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

MAX_TELEGRAM_LENGTH = 4096
MAX_SUMMARY_INPUT_CHARS = 12000
PROCESSING_CONCURRENCY = 2

groq_client = AsyncGroq(api_key=GROQ_API_KEY)
processing_semaphore = asyncio.Semaphore(PROCESSING_CONCURRENCY)


def format_duration(seconds: int) -> str:
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


def split_text(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> List[str]:
    """
    Parte el texto en bloques aptos para Telegram.
    Intenta cortar por saltos de línea y espacios antes de hacer un corte duro.
    """
    text = text.strip()
    if not text:
        return [""]

    parts: List[str] = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit

        part = remaining[:cut].strip()
        if part:
            parts.append(part)

        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)

    return parts


async def send_long_reply(message: Message, text: str, **kwargs) -> None:
    """
    Envía un texto largo como varias respuestas.
    """
    chunks = split_text(text)
    for chunk in chunks:
        await message.reply_text(chunk, **kwargs)


async def safe_edit_text(message: Optional[Message], text: str) -> None:
    if not message:
        return
    try:
        await message.edit_text(text)
    except BadRequest:
        # Por ejemplo: mensaje ya fue borrado o no cambió el contenido
        pass
    except TelegramError:
        pass


async def safe_delete(message: Optional[Message]) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


async def transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        result = await groq_client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=f,
            language="es",
            response_format="text",
        )
    return (result or "").strip()


async def summarize(text: str) -> str:
    response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.3,
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que resume textos en español de forma clara y concisa. "
                    "Responde SOLO con el resumen, sin saludos ni explicaciones adicionales. "
                    "Usa viñetas si hay varios puntos clave."
                ),
            },
            {"role": "user", "content": f"Resume este texto:\n\n{text}"},
        ],
    )
    return response.choices[0].message.content.strip()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error no controlado en el bot", exc_info=context.error)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.setdefault("summary_enabled", True)
    await update.message.reply_text(
        "Bot de transcripción.\n\n"
        "Envía una nota de voz o un archivo de audio y recibirás la transcripción.\n"
        "Si el audio dura al menos 40 segundos y los resúmenes están activados, "
        "se enviará un resumen en un mensaje separado."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos:\n"
        "/start — Inicio\n"
        "/modo — Activar/desactivar resúmenes\n"
        "/ayuda — Esta ayuda\n\n"
        "Formatos aceptados: notas de voz, MP3, M4A, WAV, OGG, FLAC, MP4 (videomensaje)\n"
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB"
    )


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.user_data.get("summary_enabled", True)
    context.user_data["summary_enabled"] = not current
    state = "activados" if context.user_data["summary_enabled"] else "desactivados"
    await update.message.reply_text(f"Resúmenes {state}.")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    if message.voice:
        tg_file = await context.bot.get_file(message.voice.file_id)
        ext = "ogg"
        duration = message.voice.duration or 0
        size = message.voice.file_size or 0
    elif message.audio:
        tg_file = await context.bot.get_file(message.audio.file_id)
        filename = message.audio.file_name or ""
        ext = Path(filename).suffix.lstrip(".").lower() or "mp3"
        duration = message.audio.duration or 0
        size = message.audio.file_size or 0
    elif message.video_note:
        tg_file = await context.bot.get_file(message.video_note.file_id)
        ext = "mp4"
        duration = message.video_note.duration or 0
        size = message.video_note.file_size or 0
    else:
        return

    if size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"El archivo supera {MAX_FILE_SIZE_MB} MB. Envía un audio más corto.")
        return

    status_msg = await message.reply_text("Procesando tu audio...")
    tmp_path = None

    try:
        async with processing_semaphore:
            await safe_edit_text(status_msg, "Descargando audio...")
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name

            await tg_file.download_to_drive(tmp_path, read_timeout=60)

            await safe_edit_text(status_msg, "Transcribiendo...")
            transcription = await transcribe(tmp_path)

            if not transcription:
                await safe_edit_text(
                    status_msg,
                    "No se detectó voz en el audio. Comprueba el volumen o prueba otro archivo."
                )
                return

            await safe_delete(status_msg)

            # La transcripción sale como respuesta directa al audio, sin literal adicional.
            await send_long_reply(message, transcription)

            summary_enabled = context.user_data.get("summary_enabled", True)
            if summary_enabled and duration >= SUMMARY_MIN_SECONDS:
                try:
                    summary_msg = await message.reply_text("Generando resumen...")
                    summary_source = transcription[:MAX_SUMMARY_INPUT_CHARS]
                    summary = await summarize(summary_source)
                    await safe_edit_text(summary_msg, summary)
                except RateLimitError:
                    logger.warning("Límite alcanzado al generar resumen")
                    await message.reply_text("No se pudo generar el resumen en este momento. Intenta más tarde.")
                except APITimeoutError:
                    logger.warning("Timeout al generar resumen")
                    await message.reply_text("La generación del resumen tardó demasiado. Intenta con un audio más corto.")
                except APIError:
                    logger.exception("Error en la API al generar resumen")
                    await message.reply_text("Error al generar el resumen. Intenta de nuevo más tarde.")

    except RateLimitError:
        logger.warning("Límite alcanzado en transcripción")
        await safe_edit_text(status_msg, "No se puede procesar ahora. Intenta de nuevo en unos segundos.")
    except APITimeoutError:
        logger.warning("Timeout en transcripción")
        await safe_edit_text(status_msg, "La transcripción tardó demasiado. Prueba con un fragmento más corto.")
    except APIError as e:
        logger.error("Error en la API de transcripción: %s", e)
        await safe_edit_text(status_msg, "Error en el servicio de transcripción. Intenta de nuevo más tarde.")
    except TelegramError as e:
        logger.error("Error de Telegram: %s", e)
        await safe_edit_text(status_msg, "Ocurrió un error al descargar o procesar el archivo de audio.")
    except Exception as e:
        logger.exception("Error inesperado: %s", e)
        await safe_edit_text(status_msg, "Ocurrió un error inesperado. Intenta más tarde.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Envía una nota de voz o un archivo de audio para transcribirlo. "
        "Usa /ayuda para más información."
    )


def main() -> None:
    missing = [v for v in ("TELEGRAM_TOKEN", "GROQ_API_KEY") if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Faltan las variables de entorno: {', '.join(missing)}")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("modo", cmd_modo))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()