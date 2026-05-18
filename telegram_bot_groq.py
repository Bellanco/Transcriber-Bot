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
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")

TRANSCRIPTION_MODEL   = "whisper-large-v3"
SUMMARY_MODEL         = "llama-3.3-70b-versatile"

SUMMARY_MIN_SECONDS   = 40
MAX_FILE_SIZE_MB      = 20
MAX_FILE_SIZE_BYTES   = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_LENGTH   = 4096
MAX_SUMMARY_INPUT     = 12000
PROCESSING_CONCURRENCY = 2

# Pausa mínima entre segmentos de Whisper para crear nuevo párrafo (segundos)
PAUSE_THRESHOLD = 0.92

# Retardo entre bloques al revelar el texto (segundos)
STREAM_DELAY = 0.6

groq_client          = AsyncGroq(api_key=GROQ_API_KEY)
processing_semaphore = asyncio.Semaphore(PROCESSING_CONCURRENCY)


# ── Formateo de texto ─────────────────────────────────────────────────────────

def paragraphs_from_segments(segments: list) -> str:
    """
    Recibe los segmentos de Whisper (cada uno con 'start', 'end', 'text')
    y los agrupa en párrafos según las pausas entre ellos.
    Si la pausa entre el final de un segmento y el inicio del siguiente
    supera PAUSE_THRESHOLD, se abre un párrafo nuevo.
    """
    if not segments:
        return ""

    paragraphs = []
    current: List[str] = []

    for i, seg in enumerate(segments):
        text = seg.get("text", "").strip()
        if not text:
            continue

        current.append(text)

        # Comprobar pausa con el segmento siguiente
        if i < len(segments) - 1:
            gap = segments[i + 1].get("start", 0) - seg.get("end", 0)
            if gap >= PAUSE_THRESHOLD:
                paragraphs.append(" ".join(current))
                current = []

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def format_summary(text: str) -> str:
    """Normaliza las viñetas del resumen y añade espaciado entre puntos."""
    import re
    lines = text.splitlines()
    formatted = []

    for line in lines:
        line = line.strip()
        if not line:
            formatted.append("")
            continue
        # Normalizar distintos tipos de viñeta a •
        line = re.sub(r'^[\*\-]\s+', '• ', line)
        formatted.append(line)

    result = "\n".join(formatted)
    # Asegurar línea en blanco antes de cada viñeta
    result = re.sub(r'\n(•)', r'\n\n\1', result)
    return result.strip()


def split_text(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> List[str]:
    """Divide texto largo respetando párrafos."""
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

        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    parts.append(remaining)
    return [p for p in parts if p]


# ── Envío progresivo ──────────────────────────────────────────────────────────

async def stream_text(message: Message, text: str) -> Optional[Message]:
    """
    Simula streaming revelando el texto párrafo a párrafo.
    Edita el mismo mensaje en cada paso para dar sensación de escritura progresiva.
    Devuelve el mensaje enviado.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return await message.reply_text(text)

    # Enviar el primer párrafo
    sent = await message.reply_text(paragraphs[0])

    # Ir añadiendo párrafos con un pequeño retardo
    accumulated = paragraphs[0]
    for paragraph in paragraphs[1:]:
        await asyncio.sleep(STREAM_DELAY)
        accumulated += f"\n\n{paragraph}"
        try:
            await sent.edit_text(accumulated)
        except (BadRequest, TelegramError):
            # Si el mensaje supera el límite, enviar uno nuevo
            sent = await message.reply_text(paragraph)
            accumulated = paragraph

    return sent


async def send_long_text(message: Message, text: str) -> Optional[Message]:
    """Envía texto largo dividido en mensajes y devuelve el primero."""
    chunks = split_text(text)
    first = None
    for chunk in chunks:
        sent = await message.reply_text(chunk)
        if first is None:
            first = sent
    return first


# ── Helpers Telegram ──────────────────────────────────────────────────────────

async def safe_edit(msg: Optional[Message], text: str):
    if not msg:
        return
    try:
        await msg.edit_text(text)
    except (BadRequest, TelegramError):
        pass


async def safe_delete(msg: Optional[Message]):
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Error no controlado", exc_info=context.error)


# ── Groq ──────────────────────────────────────────────────────────────────────

async def transcribe(file_path: str) -> tuple[str, str]:
    """
    Transcribe el audio con Whisper en modo verbose_json para obtener
    los segmentos con timestamps y detectar pausas reales.

    Devuelve una tupla (texto_plano, texto_formateado_con_párrafos).
    """
    with open(file_path, "rb") as f:
        result = await groq_client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=f,
            language="es",
            response_format="verbose_json",
        )

    segments = getattr(result, "segments", None) or []

    # Texto plano (para el resumen)
    plain = " ".join(
        seg.get("text", "").strip()
        for seg in segments
        if seg.get("text", "").strip()
    ).strip()

    # Si Groq no devuelve segmentos, usar el texto directo sin pausas
    if not plain:
        plain = getattr(result, "text", "").strip()

    # Texto con párrafos basados en pausas reales
    formatted = paragraphs_from_segments(segments) if segments else plain

    return plain, formatted


async def summarize(text: str) -> str:
    response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.3,
        messages=[
            {
                "role": "system",
                "content": (
                    "Resume el texto en español de forma clara.\n\n"
                    "Escribe primero un párrafo corto con la idea general.\n\n"
                    "Después, una lista de puntos clave usando el carácter •.\n"
                    "No uses * ni - como viñetas."
                ),
            },
            {"role": "user", "content": text},
        ],
    )
    raw = response.choices[0].message.content.strip()
    return format_summary(raw)


# ── Comandos ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("summary_enabled", True)
    await update.message.reply_text(
        "Bot de transcripción de audios.\n\n"
        "Envía una nota de voz o archivo de audio y recibirás la transcripción.\n\n"
        "Si el audio dura más de 40 segundos, también recibirás un resumen.\n\n"
        "Usa /modo para activar o desactivar los resúmenes."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos disponibles:\n\n"
        "/modo  — Activar o desactivar el resumen\n"
        "/ayuda — Esta ayuda\n\n"
        "Formatos aceptados:\n"
        "Notas de voz, MP3, M4A, WAV, OGG, FLAC, MP4\n\n"
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB"
    )


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = context.user_data.get("summary_enabled", True)
    context.user_data["summary_enabled"] = not current
    state = "activados" if context.user_data["summary_enabled"] else "desactivados"
    await update.message.reply_text(f"Resúmenes {state}.")


# ── Handler de audio ──────────────────────────────────────────────────────────

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    if message.voice:
        tg_file  = await context.bot.get_file(message.voice.file_id)
        ext      = "ogg"
        duration = message.voice.duration or 0
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
        duration = message.video_note.duration or 0
        size     = message.video_note.file_size or 0
    else:
        return

    if size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(f"El archivo supera {MAX_FILE_SIZE_MB} MB.")
        return

    status_msg = await message.reply_text("Procesando tu audio...")
    tmp_path   = None

    try:
        async with processing_semaphore:

            await safe_edit(status_msg, "Transcribiendo...")

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

            raw_transcription, formatted = await transcribe(tmp_path)

            if not raw_transcription:
                await safe_edit(status_msg, "No se detectó voz en el audio.")
                return

            await safe_delete(status_msg)
            transcription_msg = await stream_text(message, formatted)

            # Resumen si procede
            summary_enabled = context.user_data.get("summary_enabled", True)
            if summary_enabled and duration >= SUMMARY_MIN_SECONDS:
                summary_status = await transcription_msg.reply_text("Preparando resumen...")
                summary = await summarize(raw_transcription[:MAX_SUMMARY_INPUT])
                await safe_edit(summary_status, summary)

    except RateLimitError:
        await safe_edit(status_msg, "El servicio está saturado. Intenta en unos segundos.")
    except APITimeoutError:
        await safe_edit(status_msg, "La transcripción tardó demasiado. Prueba con un audio más corto.")
    except APIError:
        await safe_edit(status_msg, "Error en el servicio de transcripción.")
    except TelegramError as e:
        logger.error("Telegram error: %s", e)
    except Exception as e:
        logger.exception("Error inesperado: %s", e)
        await safe_edit(status_msg, "Ocurrió un error inesperado.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Envía una nota de voz o archivo de audio para transcribirlo."
    )


# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    missing = [v for v in ("TELEGRAM_TOKEN", "GROQ_API_KEY") if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Faltan variables de entorno: {', '.join(missing)}")

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
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("modo",  cmd_modo))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_audio)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("Bot iniciado. Esperando mensajes...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import asyncio as _asyncio
    _asyncio.set_event_loop(_asyncio.new_event_loop())
    main()