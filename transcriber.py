"""Lógica de transcripción con Groq Whisper."""

import os
import shutil
import subprocess
import tempfile
import logging
import asyncio
from typing import Optional, List, Tuple, Dict, Any

from groq import AsyncGroq, APIError, RateLimitError, APITimeoutError
from telegram import Message

from config import (
    GROQ_API_KEY,
    TRANSCRIPTION_MODEL,
    GROQ_TIMEOUT_SECONDS,
    TRANSCRIBE_MAX_RETRIES,
    RETRY_BASE_SECONDS,
    LONG_AUDIO_THRESHOLD_SECONDS,
    AUDIO_CHUNK_SECONDS,
    AUDIO_CHUNK_OVERLAP_SECONDS,
)
from formatter import (
    parse_transcription_result,
    paragraphs_from_segments,
    _merge_segments_with_overlap,
    _plain_from_segments,
    clean_transcription,
)
from utils import safe_edit

logger = logging.getLogger(__name__)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)


def _ffmpeg_is_available() -> bool:
    """Comprueba si ffmpeg está disponible en el sistema."""
    return shutil.which("ffmpeg") is not None


def _is_retryable_api_error(exc: APIError) -> bool:
    """Determina si un error de API es recuperable."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        return False
    return int(status_code) >= 500


async def _transcribe_request(file_path: str) -> Any:
    """Hace una petición de transcripción a Groq Whisper."""
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
            # Compatibilidad con versiones del SDK que no aceptan timeout.
            f.seek(0)
            return await groq_client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=f,
                language="es",
                response_format="verbose_json",
            )


async def _transcribe_with_retry(file_path: str) -> Any:
    """Transcribe con reintentos automáticos en caso de errores transitorios."""
    last_error: Optional[Exception] = None

    for attempt in range(1, TRANSCRIBE_MAX_RETRIES + 1):
        try:
            return await _transcribe_request(file_path)
        except (RateLimitError, APITimeoutError) as e:
            last_error = e
            if attempt == TRANSCRIBE_MAX_RETRIES:
                raise
            wait_s = RETRY_BASE_SECONDS * attempt
            logger.warning(
                "Reintento %s/%s por error transitorio: %s",
                attempt,
                TRANSCRIBE_MAX_RETRIES,
                e,
            )
            await asyncio.sleep(wait_s)
        except APIError as e:
            last_error = e
            if attempt == TRANSCRIBE_MAX_RETRIES or not _is_retryable_api_error(e):
                raise
            wait_s = RETRY_BASE_SECONDS * attempt
            logger.warning(
                "Reintento %s/%s por APIError recuperable: %s",
                attempt,
                TRANSCRIBE_MAX_RETRIES,
                e,
            )
            await asyncio.sleep(wait_s)

    if last_error:
        raise last_error
    raise RuntimeError("Fallo inesperado al transcribir")


def _build_audio_chunks_with_ffmpeg(
    file_path: str, duration_seconds: int
) -> Tuple[List[Tuple[str, float]], str]:
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
            "-v",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{chunk_len:.3f}",
            "-i",
            file_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            chunk_path,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg falló al crear chunks: {proc.stderr.strip() or 'error desconocido'}"
            )

        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunk_paths.append((chunk_path, start))

        start += float(step)
        index += 1

    if not chunk_paths:
        raise RuntimeError("No se pudieron generar chunks de audio")

    return chunk_paths, chunk_dir


async def transcribe(file_path: str) -> Tuple[str, str]:
    """
    Transcribe un audio con Whisper.

    Returns:
        (texto_plano, texto_con_párrafos)
    """
    result = await _transcribe_with_retry(file_path)
    plain, segments = parse_transcription_result(result)

    if segments:
        formatted = paragraphs_from_segments(segments)
    else:
        formatted = plain

    return plain, formatted


async def transcribe_long_audio(
    file_path: str, duration: int, status_msg: Optional[Message] = None
) -> Tuple[str, str]:
    """
    Transcribe audios largos por chunks con ffmpeg y une resultados.

    Returns:
        (texto_plano, texto_con_párrafos)
    """
    chunks, chunk_dir = _build_audio_chunks_with_ffmpeg(file_path, duration)
    total_chunks = len(chunks)
    logger.info("Audio largo detectado: %ss, %s chunks", duration, total_chunks)

    all_segments: List[Dict[str, Any]] = []
    fallback_plain_parts: List[str] = []

    try:
        for idx, (chunk_path, start_offset) in enumerate(chunks, start=1):
            if status_msg:
                await safe_edit(
                    status_msg,
                    f"⏳ Transcribiendo audio largo...\n`{idx}/{total_chunks}` trozos",
                )

            result = await _transcribe_with_retry(chunk_path)
            chunk_plain, chunk_segments = parse_transcription_result(result, offset=0.0)

            if chunk_segments:
                # Evita duplicar la zona de solapo.
                for seg in chunk_segments:
                    local_end = float(seg.get("end", 0.0))
                    if idx > 1 and local_end <= AUDIO_CHUNK_OVERLAP_SECONDS:
                        continue
                    all_segments.append(
                        {
                            "text": seg["text"],
                            "start": float(seg.get("start", 0.0)) + start_offset,
                            "end": float(seg.get("end", 0.0)) + start_offset,
                        }
                    )
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
