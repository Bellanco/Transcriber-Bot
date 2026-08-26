"""Configuración centralizada de variables de entorno y constantes."""

import os
from dotenv import load_dotenv

# Cargar .env si existe
try:
    load_dotenv()
except ImportError:
    pass

# ── Variables de entorno ──────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "webhook")
PORT = int(os.environ.get("PORT", "8000"))

# Modo local para Bot API
TELEGRAM_LOCAL_MODE = os.environ.get("TELEGRAM_LOCAL_MODE", "").lower() in ("true", "1", "yes")
TELEGRAM_API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "http://localhost:8081")
TELEGRAM_API_FILE_URL = os.environ.get("TELEGRAM_API_FILE_URL", "http://localhost:8081")

# ── Modelos Groq ──────────────────────────────────────────────────────────────

TRANSCRIPTION_MODEL = "whisper-large-v3"
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen/qwen3.8-27b")

# ── Límites y umbrales ────────────────────────────────────────────────────────

SUMMARY_MIN_SECONDS = 40  # Duración mínima para generar resumen
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TELEGRAM_LENGTH = 4096  # Límite de Telegram por mensaje
MAX_SUMMARY_INPUT = 12000  # Caracteres máximos para resumir
PROCESSING_CONCURRENCY = 2  # Audios simultáneos permitidos

# ── Audio largo: chunking ─────────────────────────────────────────────────────

# Antes: 20 min. Audios de ~10-12 min en una sola petición podían superar
# GROQ_TIMEOUT_SECONDS y agotar todos los reintentos sin dar feedback (~9 min
# de espera silenciosa). Se baja el umbral para trocear antes y mostrar progreso.
LONG_AUDIO_THRESHOLD_SECONDS = 6 * 60  # 6 minutos
AUDIO_CHUNK_SECONDS = 5 * 60  # Trozos de 5 minutos
AUDIO_CHUNK_OVERLAP_SECONDS = 45  # Solapo para evitar cortes

# ── Reintentos y timeouts ─────────────────────────────────────────────────────

GROQ_TIMEOUT_SECONDS = 120
TRANSCRIBE_MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2.0

# Timeout específico para la llamada de resumen (más corta que la transcripción).
SUMMARY_TIMEOUT_SECONDS = 60
SUMMARY_MAX_RETRIES = 2

# Guardarraíl mínimo de timeout para transcripción.
# El handler calcula un timeout dinámico según duración/chunks y aplica al menos
# este mínimo para evitar esperas indefinidas.
PROCESSING_TIMEOUT_SECONDS = 8 * 60

# ── Formateo de párrafos ──────────────────────────────────────────────────────

PAUSE_THRESHOLD = 0.92  # Pausa larga: siempre abre párrafo
SHORT_PAUSE_THRESHOLD = 0.34  # Pausa corta: abre si hay puntuación
MAX_PARAGRAPH_CHARS = 500  # Límite de caracteres por párrafo
STREAM_DELAY = 0.5  # Retardo entre párrafos en reveal progresivo
