"""Utilidades: validación, helpers de Telegram y funciones generales."""

import os
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

from telegram import Message
from telegram.error import TelegramError, BadRequest
from groq import AsyncGroq, APIError
from config import SUMMARY_MODEL

logger = logging.getLogger(__name__)


# ── Validación de configuración ───────────────────────────────────────────────

async def validate_groq_api(api_key: str) -> Tuple[bool, str]:
    """
    Valida que la clave API de Groq sea válida haciendo un test mínimo.
    
    Returns:
        (es_válida, mensaje_usuario)
    """
    if not api_key or not api_key.strip():
        return False, "❌ GROQ_API_KEY está vacío o no configurado."

    try:
        client = AsyncGroq(api_key=api_key)
        # Hacer un llamado muy rápido para validar que la key funciona
        await client.chat.completions.create(
            model=SUMMARY_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
            timeout=10,
        )
        return True, "✅ API de Groq validada correctamente."
    except APIError as e:
        status_code = getattr(e, "status_code", None)
        if status_code == 401:
            return False, f"❌ GROQ_API_KEY inválida (error 401 Unauthorized)."
        return False, f"❌ Error al validar Groq API: {str(e)}"
    except Exception as e:
        return False, f"❌ Error inesperado al validar Groq API: {str(e)}"


def validate_env_vars() -> Tuple[bool, str]:
    """
    Valida que todas las variables de entorno obligatorias estén configuradas.
    
    Returns:
        (es_válido, mensaje)
    """
    missing = []
    for var in ("TELEGRAM_TOKEN", "GROQ_API_KEY", "WEBHOOK_URL"):
        if not os.environ.get(var):
            missing.append(var)

    if missing:
        return False, f"❌ Faltan variables de entorno obligatorias: {', '.join(missing)}"

    return True, "✅ Variables de entorno validadas."


def resolve_webhook_settings() -> Tuple[str, str]:
    """
    Normaliza webhook URL y path.

    - Si WEBHOOK_URL ya incluye path (ej. /webhook), se usa ese path.
    - Si WEBHOOK_URL no incluye path, se añade WEBHOOK_PATH.
    
    Returns:
        (webhook_url_completa, webhook_path)
    """
    from config import WEBHOOK_URL, WEBHOOK_PATH

    raw_url = WEBHOOK_URL.strip().rstrip("/")
    fallback_path = WEBHOOK_PATH.strip().strip("/") or "webhook"

    if not raw_url:
        raise EnvironmentError("WEBHOOK_URL no está definido")

    parsed = urlparse(raw_url)
    path_from_url = parsed.path.strip("/")

    if path_from_url:
        return raw_url, path_from_url

    return f"{raw_url}/{fallback_path}", fallback_path


# ── Helpers de Telegram ───────────────────────────────────────────────────────

async def safe_edit(msg: Optional[Message], text: str) -> None:
    """Edita un mensaje de forma segura (ignora errores si no puede)."""
    if not msg:
        return
    try:
        await msg.edit_text(text)
    except (BadRequest, TelegramError):
        pass


async def safe_delete(msg: Optional[Message]) -> None:
    """Elimina un mensaje de forma segura (ignora errores si no puede)."""
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass


# ── Validación de archivos ───────────────────────────────────────────────────

def validate_downloaded_file(file_path: str) -> Tuple[bool, str]:
    """
    Valida que un archivo descargado exista y tenga contenido.
    
    Returns:
        (es_válido, mensaje_error_si_no_es_válido)
    """
    if not os.path.exists(file_path):
        return False, "Archivo no se descargó correctamente (no existe)."

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return False, "Archivo descargado está vacío."

    return True, ""


# ── Formateo de duración ──────────────────────────────────────────────────────

def format_seconds(seconds: int) -> str:
    """Convierte segundos a formato MM:SS o HH:MM:SS."""
    if seconds < 0:
        return "0:00"
    if seconds < 3600:
        mins, secs = divmod(seconds, 60)
        return f"{mins}:{secs:02d}"
    hours, remainder = divmod(seconds, 3600)
    mins, secs = divmod(remainder, 60)
    return f"{hours}:{mins:02d}:{secs:02d}"
