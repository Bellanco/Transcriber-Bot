import os
import re
import logging
import tempfile
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, List, Tuple

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

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH   = os.environ.get("WEBHOOK_PATH", "webhook")

TRANSCRIPTION_MODEL    = "whisper-large-v3"
SUMMARY_MODEL          = "llama-3.3-70b-versatile"

SUMMARY_MIN_SECONDS    = 40       # Duración mínima para generar resumen
MAX_FILE_SIZE_MB       = 20
MAX_FILE_SIZE_BYTES    = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_LENGTH    = 4096     # Límite de Telegram por mensaje
MAX_SUMMARY_INPUT      = 12000    # Caracteres máximos que se envían al modelo
PROCESSING_CONCURRENCY = 2        # Audios simultáneos permitidos

# Pausa larga: siempre abre párrafo nuevo
PAUSE_THRESHOLD        = 0.92      # segundos
# Pausa corta: abre párrafo solo si el segmento acaba en punto/cierre de frase
SHORT_PAUSE_THRESHOLD  = 0.34      # segundos
# Límite de longitud: abre párrafo solo si el segmento acaba en punto/cierre de frase
MAX_PARAGRAPH_CHARS    = 500

# Retardo entre párrafos en el reveal progresivo
STREAM_DELAY           = 0.5      # segundos

groq_client          = AsyncGroq(api_key=GROQ_API_KEY)
processing_semaphore = asyncio.Semaphore(PROCESSING_CONCURRENCY)


# ── Formateo de transcripción ─────────────────────────────────────────────────

def _seg_attr(seg, key: str, default=None):
    """
    Lee un atributo de un segmento de Whisper de forma segura.
    El SDK de Groq devuelve objetos, no dicts, así que probamos
    ambas formas para ser robustos ante cambios de versión.
    """
    if isinstance(seg, dict):
        return seg.get(key, default)
    return getattr(seg, key, default)


_SENTENCE_END = re.compile(r'[.?!\u2026\u203c\u2049]"?\s*$')


def _ends_sentence(text: str) -> bool:
    """Devuelve True si el texto termina en cierre de frase."""
    return bool(_SENTENCE_END.search(text))


def paragraphs_from_segments(segments: list) -> str:
    """
    Agrupa los segmentos de Whisper en párrafos con tres niveles de corte,
    garantizando que nunca se parte una frase a mitad:

      1. Pausa larga (>= PAUSE_THRESHOLD)
         → siempre abre párrafo nuevo, independientemente de la puntuación.

      2. Pausa corta (>= SHORT_PAUSE_THRESHOLD) + segmento acaba en .?!
         → el hablante hizo una pausa natural tras terminar la idea.

      3. Párrafo acumulado >= MAX_PARAGRAPH_CHARS + segmento acaba en .?!
         → evita bloques enormes sin romper frases a mitad.

    Si ningún criterio se cumple, el segmento se une al párrafo actual.
    """
    if not segments:
        return ""

    paragraphs: List[str] = []
    current: List[str] = []
    current_chars = 0

    for i, seg in enumerate(segments):
        raw  = _seg_attr(seg, "text", "") or ""
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw.strip())
        if not text:
            continue

        current.append(text)
        current_chars += len(text)

        is_last       = (i == len(segments) - 1)
        ends_sentence = _ends_sentence(text)

        # Calcular pausa con el siguiente segmento
        gap = 0.0
        if not is_last:
            end   = _seg_attr(seg,            "end",   0) or 0
            start = _seg_attr(segments[i + 1], "start", 0) or 0
            gap   = max(0.0, start - end)

        # Nivel 1: pausa larga → cortar siempre
        long_pause  = not is_last and gap >= PAUSE_THRESHOLD
        # Nivel 2: pausa corta + fin de frase
        short_pause = not is_last and gap >= SHORT_PAUSE_THRESHOLD and ends_sentence
        # Nivel 3: párrafo demasiado largo + fin de frase
        too_long    = current_chars >= MAX_PARAGRAPH_CHARS and ends_sentence

        if long_pause or short_pause or too_long:
            paragraphs.append(" ".join(current))
            current = []
            current_chars = 0

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def clean_transcription(text: str) -> str:
    """
    Limpieza básica del texto plano:
    - Elimina caracteres de control
    - Colapsa espacios múltiples
    - Asegura que las frases empiecen con mayúscula tras punto
    """
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"(\.) ([a-záéíóúüñ])", lambda m: m.group(1) + " " + m.group(2).upper(), text)
    return text.strip()


def format_summary(text: str) -> str:
    """
    Normaliza el resumen:
    - Elimina viñetas o numeraciones si aparecen
    - Mantiene bloques en párrafos simples
    - Limpia líneas vacías duplicadas
    """
    lines = text.splitlines()
    cleaned: List[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            cleaned.append("")
            continue

        # Si el modelo devuelve listas, las convertimos a texto corrido.
        line = re.sub(r"^[\*\-•·]\s+", "", line)
        line = re.sub(r"^\d+[\.)]\s+", "", line)
        cleaned.append(line)

    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ── Partición de mensajes largos ──────────────────────────────────────────────

def split_text(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> List[str]:
    """
    Divide texto largo en trozos respetando párrafos, frases y palabras.
    Nunca corta a mitad de palabra.
    """
    text = text.strip()
    if not text:
        return []

    parts = []
    remaining = text

    while len(remaining) > limit:
        # Intentar cortar en párrafo, luego frase, luego espacio
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(". ", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit

        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)

    return [p for p in parts if p]


# ── Envío progresivo (reveal por párrafos) ────────────────────────────────────

async def stream_text(message: Message, text: str) -> Optional[Message]:
    """
    Revela el texto párrafo a párrafo editando el mismo mensaje.
    Si el mensaje acumulado supera el límite de Telegram, abre uno nuevo.
    Devuelve el último mensaje enviado (para poder encadenar el resumen).
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return await message.reply_text(text or "—")

    sent = await message.reply_text(paragraphs[0])
    accumulated = paragraphs[0]

    for paragraph in paragraphs[1:]:
        await asyncio.sleep(STREAM_DELAY)
        candidate = accumulated + "\n\n" + paragraph

        if len(candidate) > MAX_TELEGRAM_LENGTH:
            # El bloque no cabe: enviar mensaje nuevo
            sent = await message.reply_text(paragraph)
            accumulated = paragraph
        else:
            try:
                await sent.edit_text(candidate)
                accumulated = candidate
            except BadRequest:
                sent = await message.reply_text(paragraph)
                accumulated = paragraph
            except TelegramError:
                pass

    return sent


# ── Helpers Telegram ──────────────────────────────────────────────────────────

async def safe_edit(msg: Optional[Message], text: str) -> None:
    if not msg:
        return
    try:
        await msg.edit_text(text)
    except (BadRequest, TelegramError):
        pass


async def safe_delete(msg: Optional[Message]) -> None:
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error no controlado", exc_info=context.error)


def resolve_webhook_settings() -> Tuple[str, str]:
    """
    Normaliza webhook URL y path.

    - Si WEBHOOK_URL ya incluye path (ej. /webhook), se usa ese path.
    - Si WEBHOOK_URL no incluye path, se añade WEBHOOK_PATH.
    """
    raw_url = WEBHOOK_URL.strip().rstrip("/")
    fallback_path = WEBHOOK_PATH.strip().strip("/") or "webhook"

    if not raw_url:
        raise EnvironmentError("WEBHOOK_URL no está definido")

    parsed = urlparse(raw_url)
    path_from_url = parsed.path.strip("/")

    if path_from_url:
        return raw_url, path_from_url

    return f"{raw_url}/{fallback_path}", fallback_path


# ── Groq: transcripción y resumen ─────────────────────────────────────────────

async def transcribe(file_path: str) -> Tuple[str, str]:
    """
    Transcribe con Whisper en modo verbose_json para obtener segmentos
    con timestamps reales.

    Devuelve (texto_plano, texto_con_párrafos).
    El texto plano se usa para el resumen; el formateado para mostrar.
    """
    with open(file_path, "rb") as f:
        result = await groq_client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=f,
            language="es",
            response_format="verbose_json",
        )

    segments = getattr(result, "segments", None) or []

    if segments:
        # Construir texto plano desde segmentos (más limpio que result.text)
        plain = " ".join(
            (_seg_attr(s, "text") or "").strip()
            for s in segments
            if (_seg_attr(s, "text") or "").strip()
        )
        plain     = clean_transcription(plain)
        formatted = paragraphs_from_segments(segments)
    else:
        # Fallback: Groq no devolvió segmentos
        plain     = clean_transcription(getattr(result, "text", "") or "")
        formatted = plain

    return plain, formatted


async def summarize(text: str) -> str:
    """Genera un resumen breve y natural con lenguaje sencillo."""
    response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que resume audios en español con lenguaje muy sencillo, natural y cercano.\n\n"
                    "Devuelve únicamente texto en párrafos cortos, sin viñetas, sin numeraciones y sin títulos.\n"
                    "Incluye primero la idea principal y después los puntos más importantes, con un párrafo por idea clave.\n"
                    "Usa frases claras para cualquier persona, evita tecnicismos y evita repetir ideas.\n"
                    "No añadas saludos, despedidas ni explicaciones extra."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
    )
    raw = response.choices[0].message.content.strip()
    return format_summary(raw)


# ── Comandos ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.setdefault("summary_enabled", True)
    await update.message.reply_text(
        "Bot de transcripción de audios.\n\n"
        "Envía una nota de voz o archivo de audio y recibirás la transcripción.\n\n"
        "Si el audio supera los 40 segundos, también recibirás un resumen.\n\n"
        "Usa /modo para activar o desactivar los resúmenes."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n\n"
        "/modo  — Activar o desactivar el resumen automático\n"
        "/ayuda — Esta ayuda\n\n"
        "Formatos aceptados:\n"
        "Notas de voz, MP3, M4A, WAV, OGG, FLAC, MP4\n\n"
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB"
    )


async def cmd_modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.user_data.get("summary_enabled", True)
    context.user_data["summary_enabled"] = not current
    state = "activados" if context.user_data["summary_enabled"] else "desactivados"
    await update.message.reply_text(f"Resúmenes {state}.")


# ── Handler de audio ──────────────────────────────────────────────────────────

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await message.reply_text(f"El archivo supera el límite de {MAX_FILE_SIZE_MB} MB.")
        return

    status_msg = await message.reply_text("Procesando tu audio...")
    tmp_path: Optional[str] = None

    try:
        async with processing_semaphore:

            await safe_edit(status_msg, "Transcribiendo...")

            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

            plain, formatted = await transcribe(tmp_path)

            if not plain:
                await safe_edit(status_msg, "No se detectó voz en el audio.")
                return

            await safe_delete(status_msg)
            status_msg = None  # Ya no existe, evitar doble edición en el except

            last_msg = await stream_text(message, formatted)

            summary_enabled = context.user_data.get("summary_enabled", True)
            if summary_enabled and duration >= SUMMARY_MIN_SECONDS:
                summary_status = await last_msg.reply_text("Preparando resumen...")
                summary = await summarize(plain[:MAX_SUMMARY_INPUT])
                await safe_edit(summary_status, summary)

    except RateLimitError:
        await safe_edit(status_msg, "El servicio está saturado. Espera unos segundos e inténtalo de nuevo.")
    except APITimeoutError:
        await safe_edit(status_msg, "La transcripción tardó demasiado. Prueba con un audio más corto.")
    except APIError as e:
        logger.error("Groq API error: %s", e)
        await safe_edit(status_msg, "Error en el servicio de transcripción.")
    except TelegramError as e:
        logger.error("Telegram error: %s", e)
    except Exception as e:
        logger.exception("Error inesperado: %s", e)
        await safe_edit(status_msg, "Ocurrió un error inesperado.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Envía una nota de voz o archivo de audio para transcribirlo."
    )


# ── Arranque ──────────────────────────────────────────────────────────────────

def main() -> None:
    missing = [
        v for v in ("TELEGRAM_TOKEN", "GROQ_API_KEY", "WEBHOOK_URL")
        if not os.environ.get(v)
    ]
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

    port = int(os.environ.get("PORT", "8000"))
    webhook_url, webhook_path = resolve_webhook_settings()
    logger.info("Bot iniciado en modo webhook. Escuchando en puerto %s", port)
    logger.info("Webhook configurado en %s", webhook_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    import asyncio as _asyncio
    _asyncio.set_event_loop(_asyncio.new_event_loop())
    main()