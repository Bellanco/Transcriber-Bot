"""Punto de entrada principal del bot de transcripción."""

import logging
import sys

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    TELEGRAM_TOKEN,
    GROQ_API_KEY,
    WEBHOOK_URL,
    WEBHOOK_SECRET,
    PORT,
    TELEGRAM_LOCAL_MODE,
    TELEGRAM_API_BASE_URL,
    TELEGRAM_API_FILE_URL,
)
from utils import (
    validate_env_vars,
    validate_groq_api,
    resolve_webhook_settings,
)
from handlers import (
    cmd_start,
    cmd_help,
    cmd_modo,
    handle_audio,
    handle_text,
    error_handler,
)
from transcriber import _ffmpeg_is_available

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def validate_startup() -> bool:
    """Valida que la configuración sea correcta al iniciar."""
    logger.info("=" * 70)
    logger.info("🚀 Iniciando Bot de Transcripción")
    logger.info("=" * 70)

    # Validar variables de entorno obligatorias
    is_valid, msg = validate_env_vars()
    logger.info(msg)
    if not is_valid:
        logger.error("❌ Validación fallida. Abortar.")
        return False

    # Validar API de Groq
    logger.info("🔐 Validando Groq API...")
    is_valid, msg = await validate_groq_api(GROQ_API_KEY)
    logger.info(msg)
    if not is_valid:
        logger.error("❌ Validación fallida. Abortar.")
        return False

    # Verificar ffmpeg
    if _ffmpeg_is_available():
        logger.info("✅ ffmpeg detectado: procesamiento de audios largos habilitado")
    else:
        logger.warning(
            "⚠️ ffmpeg no detectado: audios largos pueden fallar"
        )

    # Verificar modo local de Bot API
    if TELEGRAM_LOCAL_MODE:
        logger.info(
            "🔗 Modo Bot API local: %s", TELEGRAM_API_BASE_URL
        )
    else:
        logger.info("🔗 Usando Bot API en la nube")

    logger.info("=" * 70)
    logger.info("✅ Todas las validaciones pasaron")
    logger.info("=" * 70)
    return True


def main() -> None:
    """Punto de entrada principal."""
    import asyncio

    # Validar configuración
    is_valid = asyncio.run(validate_startup())
    if not is_valid:
        sys.exit(1)

    # Crear aplicación
    app_builder = Application.builder().token(TELEGRAM_TOKEN)

    # Configurar modo local si está habilitado
    if TELEGRAM_LOCAL_MODE:
        app_builder = (
            app_builder
            .api_base_url(TELEGRAM_API_BASE_URL)
            .api_file_url(TELEGRAM_API_FILE_URL)
        )

    app = (
        app_builder
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )

    # Registrar handlers de comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("modo", cmd_modo))

    # Registrar handlers de mensajes
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_audio)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # Registrar handler global de errores
    app.add_error_handler(error_handler)

    # Configurar webhook
    try:
        webhook_url, webhook_path = resolve_webhook_settings()
    except EnvironmentError as e:
        logger.error("Error al resolver webhook settings: %s", e)
        sys.exit(1)

    logger.info("=" * 70)
    logger.info("🌐 Configuración de Webhook")
    logger.info("=" * 70)
    logger.info("Escuchando en: 0.0.0.0:%d", PORT)
    logger.info("Path: /%s", webhook_path)
    logger.info("Webhook URL: %s", webhook_url)
    logger.info("=" * 70)

    # Iniciar bot con webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
