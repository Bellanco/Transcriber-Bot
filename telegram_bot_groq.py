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


def split_text(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> List[str]:
    text = text.strip()
    if not text:
        return [""]

    parts = []
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
        parts.append(part)
        remaining = remaining[cut:].strip()

    parts.append(remaining)
    return parts


async def send_long_reply(message: Message, text: str) -> Message:
    """
    Envía texto largo y devuelve el primer mensaje enviado
    (para poder responder a él después).
    """
    chunks = split_text(text)
    first_msg = None

    for chunk in chunks:
        sent = await message.reply_text(chunk)
        if first_msg is None:
            first_msg = sent

    return first_msg


def format_summary(text: str) -> str:
    lines = text.splitlines()
    formatted = []

    for line in lines:
        line = line.strip()

        if not line:
            formatted.append("")
            continue

        if line.startswith("* ") or line.startswith("- "):
            line = "• " + line[2:]

        formatted.append(line)

    result = "\n".join(formatted)
    result = result.replace("\n•", "\n\n•")

    return result.strip()


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
                    "Resume textos en español de forma clara.\n\n"
                    "Primero escribe un breve párrafo explicando la idea general.\n\n"
                    "Después escribe una lista de puntos clave usando el carácter •.\n"
                    "No uses * ni -."
                ),
            },
            {"role": "user", "content": text},
        ],
    )

    raw = response.choices[0].message.content.strip()
    return format_summary(raw)


async def safe_edit_text(message: Optional[Message], text: str):
    if not message:
        return
    try:
        await message.edit_text(text)
    except (BadRequest, TelegramError):
        pass


async def safe_delete(message: Optional[Message]):
    if not message:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Error no controlado", exc_info=context.error)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("summary_enabled", True)

    await update.message.reply_text(
        "Bot de transcripción.\n\n"
        "Envía una nota de voz o archivo de audio y recibirás la transcripción.\n"
        "Si dura más de 40 segundos, también recibirás un resumen."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos:\n"
        "/start — Inicio\n"
        "/modo — Activar/desactivar resumen\n"
        "/ayuda — Ayuda\n\n"
        "Formatos: notas de voz, MP3, M4A, WAV, OGG, FLAC, MP4\n"
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB"
    )


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("summary_enabled", True)
    context.user_data["summary_enabled"] = not current

    state = "activados" if context.user_data["summary_enabled"] else "desactivados"

    await update.message.reply_text(f"Resúmenes {state}.")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await message.reply_text(f"El archivo supera {MAX_FILE_SIZE_MB} MB.")
        return

    status_msg = await message.reply_text("Procesando tu audio...")
    tmp_path = None

    try:
        async with processing_semaphore:

            await safe_edit_text(status_msg, "Transcribiendo tu audio...")

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name

            await tg_file.download_to_drive(tmp_path)

            transcription = await transcribe(tmp_path)

            if not transcription:
                await safe_edit_text(status_msg, "No se detectó voz en el audio.")
                return

            await safe_delete(status_msg)

            transcription_msg = await send_long_reply(message, transcription)

            summary_enabled = context.user_data.get("summary_enabled", True)

            if summary_enabled and duration >= SUMMARY_MIN_SECONDS:

                summary_msg = await transcription_msg.reply_text(
                    "Preparando un resumen del audio..."
                )

                summary_source = transcription[:MAX_SUMMARY_INPUT_CHARS]

                summary = await summarize(summary_source)

                await safe_edit_text(summary_msg, summary)

    except RateLimitError:
        await safe_edit_text(status_msg, "El servicio está ocupado. Intenta más tarde.")

    except APITimeoutError:
        await safe_edit_text(status_msg, "La transcripción tardó demasiado.")

    except APIError:
        await safe_edit_text(status_msg, "Error en el servicio de transcripción.")

    except TelegramError:
        await safe_edit_text(status_msg, "Error al procesar el audio.")

    except Exception as e:
        logger.exception("Error inesperado: %s", e)
        await safe_edit_text(status_msg, "Ocurrió un error inesperado.")

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Envía una nota de voz o archivo de audio para transcribirlo."
    )


def main():

    missing = [v for v in ("TELEGRAM_TOKEN", "GROQ_API_KEY") if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Faltan variables: {', '.join(missing)}")

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

    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_audio)
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Esperando mensajes...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()