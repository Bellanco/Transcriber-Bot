import os
import re
import json
import logging
import tempfile
import asyncio
import subprocess
import shutil
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, List, Tuple, Dict, Any

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
SUMMARY_MODEL          = os.environ.get("SUMMARY_MODEL", "qwen/qwen3.8-27b")

SUMMARY_MIN_SECONDS    = 40       # Duración mínima para generar resumen
MAX_FILE_SIZE_MB       = 20
MAX_FILE_SIZE_BYTES    = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_LENGTH    = 4096     # Límite de Telegram por mensaje
MAX_SUMMARY_INPUT      = 12000    # Caracteres máximos que se envían al modelo
PROCESSING_CONCURRENCY = 2        # Audios simultáneos permitidos

# Audios largos: dividir y transcribir en trozos
LONG_AUDIO_THRESHOLD_SECONDS = 20 * 60
AUDIO_CHUNK_SECONDS          = 5 * 60
AUDIO_CHUNK_OVERLAP_SECONDS  = 45

# Resiliencia de peticiones de transcripción
GROQ_TIMEOUT_SECONDS     = 180
TRANSCRIBE_MAX_RETRIES   = 3
RETRY_BASE_SECONDS       = 2.0

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


def _normalize_segment(seg: Any, offset: float = 0.0) -> Optional[Dict[str, Any]]:
    """Convierte un segmento de Groq a dict normalizado con offset opcional."""
    raw_text = _seg_attr(seg, "text", "") or ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_text.strip())
    if not text:
        return None

    start = float(_seg_attr(seg, "start", 0.0) or 0.0) + offset
    end = float(_seg_attr(seg, "end", 0.0) or 0.0) + offset
    if end < start:
        end = start

    return {
        "text": text,
        "start": start,
        "end": end,
    }


def _plain_from_segments(segments: List[Dict[str, Any]]) -> str:
    """Construye texto plano limpio a partir de segmentos normalizados."""
    if not segments:
        return ""
    plain = " ".join(s["text"] for s in segments if s.get("text"))
    return clean_transcription(plain)


def _merge_segments_with_overlap(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordena y deduplica segmentos por ventana temporal para evitar duplicados."""
    if not segments:
        return []

    ordered = sorted(segments, key=lambda s: (float(s.get("start", 0.0)), float(s.get("end", 0.0))))
    merged: List[Dict[str, Any]] = []
    for seg in ordered:
        if not merged:
            merged.append(seg)
            continue

        prev = merged[-1]
        same_text = (seg.get("text", "").strip().lower() == prev.get("text", "").strip().lower())
        close_time = abs(float(seg.get("start", 0.0)) - float(prev.get("start", 0.0))) <= 1.2
        if same_text and close_time:
            continue

        merged.append(seg)

    return merged


def _ffmpeg_is_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _build_audio_chunks_with_ffmpeg(file_path: str, duration_seconds: int) -> Tuple[List[Tuple[str, float]], str]:
    """
    Divide audio en trozos temporales usando ffmpeg.
    Devuelve lista de (path_chunk, offset_inicio_segundos) y carpeta temporal.
    """
    if duration_seconds <= 0:
        raise ValueError("Duración inválida para chunking")

    if not _ffmpeg_is_available():
        raise RuntimeError("ffmpeg no está disponible en el entorno")

    chunk_dir = tempfile.mkdtemp(prefix="tg_chunks_")
    chunk_paths: List[Tuple[str, float]] = []

    step = max(1, AUDIO_CHUNK_SECONDS - AUDIO_CHUNK_OVERLAP_SECONDS)
    start = 0.0
    index = 0

    while start < duration_seconds:
        remaining = max(0.0, float(duration_seconds) - start)
        chunk_len = min(float(AUDIO_CHUNK_SECONDS), remaining)
        if chunk_len <= 0:
            break

        chunk_path = os.path.join(chunk_dir, f"chunk_{index:03d}.flac")
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-y",
            "-ss", f"{start:.3f}",
            "-t", f"{chunk_len:.3f}",
            "-i", file_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "flac",
            chunk_path,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg falló al crear chunks: {proc.stderr.strip() or 'error desconocido'}")

        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunk_paths.append((chunk_path, start))

        start += float(step)
        index += 1

    if not chunk_paths:
        raise RuntimeError("No se pudieron generar chunks de audio")

    return chunk_paths, chunk_dir


def _is_retryable_api_error(exc: APIError) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return False
    return int(status_code) >= 500


async def _transcribe_request(file_path: str):
    with open(file_path, "rb") as f:
        try:
            return await groq_client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=f,
                language="es",
                response_format="verbose_json",
                timeout=GROQ_TIMEOUT_SECONDS,
            )
        except TypeError:
            # Compatibilidad con versiones del SDK que no aceptan timeout por petición.
            f.seek(0)
            return await groq_client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=f,
                language="es",
                response_format="verbose_json",
            )


async def _transcribe_with_retry(file_path: str):
    last_error: Optional[Exception] = None
    for attempt in range(1, TRANSCRIBE_MAX_RETRIES + 1):
        try:
            return await _transcribe_request(file_path)
        except (RateLimitError, APITimeoutError) as e:
            last_error = e
            if attempt == TRANSCRIBE_MAX_RETRIES:
                raise
            wait_s = RETRY_BASE_SECONDS * attempt
            logger.warning("Reintento %s/%s por error transitorio: %s", attempt, TRANSCRIBE_MAX_RETRIES, e)
            await asyncio.sleep(wait_s)
        except APIError as e:
            last_error = e
            if attempt == TRANSCRIBE_MAX_RETRIES or not _is_retryable_api_error(e):
                raise
            wait_s = RETRY_BASE_SECONDS * attempt
            logger.warning("Reintento %s/%s por APIError recuperable: %s", attempt, TRANSCRIBE_MAX_RETRIES, e)
            await asyncio.sleep(wait_s)

    if last_error:
        raise last_error
    raise RuntimeError("Fallo inesperado al transcribir")


def _parse_transcription_result(result: Any, offset: float = 0.0) -> Tuple[str, List[Dict[str, Any]]]:
    """Extrae texto plano y segmentos normalizados desde la respuesta de Groq."""
    raw_segments = getattr(result, "segments", None) or []
    segments: List[Dict[str, Any]] = []

    for seg in raw_segments:
        normalized = _normalize_segment(seg, offset=offset)
        if normalized:
            segments.append(normalized)

    if segments:
        plain = _plain_from_segments(segments)
    else:
        plain = clean_transcription(getattr(result, "text", "") or "")

    return plain, segments


async def transcribe_long_audio(file_path: str, duration: int, status_msg: Optional[Message] = None) -> Tuple[str, str]:
    """
    Transcribe audios largos por chunks con ffmpeg y une resultados.
    """
    chunks, chunk_dir = _build_audio_chunks_with_ffmpeg(file_path, duration)
    total_chunks = len(chunks)
    logger.info("Audio largo detectado: %ss, %s chunks", duration, total_chunks)

    all_segments: List[Dict[str, Any]] = []
    fallback_plain_parts: List[str] = []

    try:
        for idx, (chunk_path, start_offset) in enumerate(chunks, start=1):
            if status_msg:
                await safe_edit(status_msg, f"Transcribiendo audio largo... {idx}/{total_chunks}")

            result = await _transcribe_with_retry(chunk_path)
            chunk_plain, chunk_segments = _parse_transcription_result(result, offset=0.0)

            if chunk_segments:
                # Evita duplicar la zona de solape al incorporar chunks no iniciales.
                for seg in chunk_segments:
                    local_end = float(seg.get("end", 0.0))
                    if idx > 1 and local_end <= AUDIO_CHUNK_OVERLAP_SECONDS:
                        continue
                    all_segments.append({
                        "text": seg["text"],
                        "start": float(seg.get("start", 0.0)) + start_offset,
                        "end": float(seg.get("end", 0.0)) + start_offset,
                    })
            elif chunk_plain:
                fallback_plain_parts.append(chunk_plain)

        merged_segments = _merge_segments_with_overlap(all_segments)
        if merged_segments:
            plain = _plain_from_segments(merged_segments)
            formatted = paragraphs_from_segments(merged_segments)
            return plain, formatted

        plain = clean_transcription(" ".join(fallback_plain_parts))
        return plain, plain
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)


def _truncate_words(text: str, max_words: int = 22) -> str:
    """Recorta una frase a un máximo de palabras para mantenerla escaneable."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip(" ,;:") + "..."


def _extract_json_payload(raw_text: str) -> Optional[str]:
    """Extrae un bloque JSON desde texto libre o dentro de ```json ... ```."""
    if not raw_text:
        return None

    fenced = re.search(r"```json\s*(.*?)\s*```", raw_text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()

    # Fallback: intenta tomar desde el primer '[' hasta el ultimo ']'.
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return raw_text[start:end + 1].strip()

    return None


def _format_summary_from_topics(topics: List[Dict[str, Any]]) -> str:
    """Renderiza temas en formato fijo y en orden cronologico."""
    if not topics:
        return ""

    normalized: List[Tuple[float, int, str, str]] = []
    for idx, item in enumerate(topics):
        if not isinstance(item, dict):
            continue

        topic = str(item.get("tema", "")).strip(" .,-") or "Tema"
        summary = str(item.get("resumen", "")).strip(" .,-")
        if not summary:
            continue

        order_raw = item.get("posicion_inicial", None)
        try:
            order_value = float(order_raw)
        except (TypeError, ValueError):
            order_value = float("inf")

        normalized.append((order_value, idx, topic, _truncate_words(summary)))

    if not normalized:
        return ""

    normalized.sort(key=lambda t: (t[0], t[1]))
    bullets = [f"• {topic}: {summary}" for _, _, topic, summary in normalized]
    return "\n".join(bullets)


def format_summary(text: str) -> str:
    """
    Normaliza el resumen para formato de vistazo:
    - Una línea por tema
    - Formato fijo: "• Tema: frase breve"
    - Frases compactas para lectura rápida
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets: List[str] = []

    for raw_line in lines:
        line = re.sub(r"^[\*\-•·]\s+", "", raw_line)
        line = re.sub(r"^\d+[\.)]\s+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        if ":" in line:
            topic, summary = line.split(":", 1)
            topic = topic.strip(" .,-") or "Tema"
            summary = summary.strip(" .,-")
            if not summary:
                continue
            summary = _truncate_words(summary)
            bullets.append(f"• {topic}: {summary}")
        else:
            summary = _truncate_words(line.strip(" .,-"))
            if summary:
                bullets.append(f"• Tema: {summary}")

    if bullets:
        return "\n".join(bullets)

    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return "• Tema general: No se detectaron ideas suficientes para resumir."
    return f"• Tema general: {_truncate_words(compact)}"


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
    result = await _transcribe_with_retry(file_path)
    plain, segments = _parse_transcription_result(result)

    if segments:
        formatted = paragraphs_from_segments(segments)
    else:
        formatted = plain

    return plain, formatted


async def summarize(text: str) -> str:
    """Genera un resumen por temas en formato de puntos breves y escaneables."""
    response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que resume audios en español (castellano) de forma clara y directa.\n\n"
                    "Analiza el texto y detecta sus temas en orden cronologico de aparicion.\n"
                    "Devuelve SOLO JSON valido (sin texto extra) con este esquema exacto:\n"
                    "[\n"
                    "  {\"tema\":\"...\",\"resumen\":\"...\",\"posicion_inicial\":123}\n"
                    "]\n\n"
                    "Reglas obligatorias:\n"
                    "1) Detecta todos los temas del audio (sin limite fijo de puntos).\n"
                    "2) Respeta estrictamente el orden cronologico de aparicion de temas.\n"
                    "3) Cada tema debe tener una sola frase breve (10 a 22 palabras), clara y sin rodeos.\n"
                    "4) posicion_inicial debe ser el indice aproximado (en caracteres) donde ese tema aparece por primera vez en el texto.\n"
                    "5) No uses markdown, no uses bloques de codigo, no agregues explicaciones.\n"
                    "6) Si no hay temas claros, devuelve un unico objeto con tema='Tema general'.\n"
                    "7) Responde siempre en castellano (español), aunque el audio mezcle otros idiomas."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
    )
    raw = response.choices[0].message.content.strip()

    payload = _extract_json_payload(raw)
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                rendered = _format_summary_from_topics(parsed)
                if rendered:
                    return rendered
        except json.JSONDecodeError:
            logger.warning("Respuesta de resumen no vino en JSON valido; usando fallback.")

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
        f"Tamaño máximo: {MAX_FILE_SIZE_MB} MB\n"
        "Audios largos: se procesan automáticamente en trozos."
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

            if duration >= LONG_AUDIO_THRESHOLD_SECONDS:
                if not _ffmpeg_is_available():
                    await safe_edit(status_msg, "No se puede procesar audio largo ahora. Falta ffmpeg en el servidor.")
                    return
                await safe_edit(status_msg, "Preparando audio largo...")
                plain, formatted = await transcribe_long_audio(tmp_path, duration, status_msg=status_msg)
            else:
                await safe_edit(status_msg, "Transcribiendo...")
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

    if _ffmpeg_is_available():
        logger.info("ffmpeg detectado: procesamiento de audios largos habilitado")
    else:
        logger.warning("ffmpeg no detectado: audios largos pueden fallar")

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