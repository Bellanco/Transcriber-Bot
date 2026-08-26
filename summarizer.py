"""Lógica de resumen con Groq llama."""

import asyncio
import json
import logging
from typing import List, Dict, Any

from groq import AsyncGroq, APIError, RateLimitError, APITimeoutError

from config import (
    GROQ_API_KEY,
    SUMMARY_MODEL,
    SUMMARY_TIMEOUT_SECONDS,
    SUMMARY_MAX_RETRIES,
    RETRY_BASE_SECONDS,
)
from formatter import _extract_json_payload, _format_summary_from_topics, format_summary

logger = logging.getLogger(__name__)

groq_client = AsyncGroq(api_key=GROQ_API_KEY)


async def _summarize_request(text: str) -> str:
    """Hace una petición de resumen a Groq con timeout explícito."""
    response = await groq_client.chat.completions.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        temperature=0.2,
        timeout=SUMMARY_TIMEOUT_SECONDS,
        messages=[
            {
                "role": "system",
                "content": (
                    "Eres un asistente que resume audios en español (castellano) de forma clara y directa.\n\n"
                    "Analiza el texto y detecta sus temas en orden cronológico de aparición.\n"
                    "Devuelve SOLO JSON válido (sin texto extra) con este esquema exacto:\n"
                    "[\n"
                    '  {"tema":"...","resumen":"...","posicion_inicial":123}\n'
                    "]\n\n"
                    "Reglas obligatorias:\n"
                    "1) Detecta todos los temas del audio (sin límite fijo de puntos).\n"
                    "2) Respeta estrictamente el orden cronológico de aparición de temas.\n"
                    "3) Cada tema debe tener una sola frase breve (10 a 22 palabras), clara y sin rodeos.\n"
                    "4) posicion_inicial debe ser el índice aproximado (en caracteres) donde ese tema aparece por primera vez.\n"
                    "5) No uses markdown, no uses bloques de código, no agregues explicaciones.\n"
                    "6) Si no hay temas claros, devuelve un único objeto con tema='Tema general'.\n"
                    "7) Responde siempre en castellano (español), aunque el audio mezcle otros idiomas."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
    )
    return response.choices[0].message.content.strip()


async def summarize(text: str) -> str:
    """
    Genera un resumen por temas en formato de puntos breves y escaneables.
    Reintenta en caso de errores transitorios (rate limit, timeout).

    Returns:
        Resumen formateado como bullets.
    """
    raw: str = ""
    for attempt in range(1, SUMMARY_MAX_RETRIES + 1):
        try:
            raw = await _summarize_request(text)
            break
        except (RateLimitError, APITimeoutError) as e:
            if attempt == SUMMARY_MAX_RETRIES:
                raise
            logger.warning(
                "Reintento de resumen %s/%s por error transitorio: %s",
                attempt,
                SUMMARY_MAX_RETRIES,
                e,
            )
            await asyncio.sleep(RETRY_BASE_SECONDS * attempt)
        except APIError as e:
            status_code = getattr(e, "status_code", None)
            if attempt == SUMMARY_MAX_RETRIES or not (status_code and int(status_code) >= 500):
                raise
            logger.warning(
                "Reintento de resumen %s/%s por APIError recuperable: %s",
                attempt,
                SUMMARY_MAX_RETRIES,
                e,
            )
            await asyncio.sleep(RETRY_BASE_SECONDS * attempt)

    # Intenta extraer y parsear JSON
    payload = _extract_json_payload(raw)
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                rendered = _format_summary_from_topics(parsed)
                if rendered:
                    return rendered
        except json.JSONDecodeError:
            logger.warning(
                "Respuesta de resumen no vino en JSON válido; usando fallback."
            )

    # Fallback: formatear como texto plano
    return format_summary(raw)
