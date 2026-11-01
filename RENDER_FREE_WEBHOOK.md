# Deploy en Render Free (Webhook)

Esta guia deja el bot funcionando en Render Free sin polling.

## 1) Requisitos

- Repositorio en GitHub con este proyecto.
- Token de Telegram (`TELEGRAM_TOKEN`).
- API key de Groq (`GROQ_API_KEY`).

## 2) Crear servicio en Render

1. Entrar a Render y crear `New > Web Service`.
2. Conectar el repositorio.
3. Plan: `Free`.
4. Build Command:

```bash
pip install -r requirements.txt
```

5. Start Command:

```bash
python telegram_bot_groq.py
```

Nota: `Procfile` ya esta preparado con proceso `web`.

## 3) Variables de entorno

Configura estas variables en Render:

- `TELEGRAM_TOKEN` = token de BotFather
- `GROQ_API_KEY` = clave de Groq
- `WEBHOOK_URL` = URL publica de Render
- `WEBHOOK_SECRET` = cadena aleatoria larga
- `WEBHOOK_PATH` = `webhook` (opcional)

### Formas validas de WEBHOOK_URL

- Con path incluido:
  - `https://tu-servicio.onrender.com/webhook`
- Sin path (el bot anade `WEBHOOK_PATH`):
  - `https://tu-servicio.onrender.com`

## 4) Primer deploy

1. Haz deploy.
2. Revisa logs y confirma mensajes tipo:
   - `Bot iniciado en modo webhook...`
   - `Webhook configurado en ...`

## 5) Verificar webhook en Telegram

Abre en navegador (o usa curl):

```text
https://api.telegram.org/bot<TELEGRAM_TOKEN>/getWebhookInfo
```

Debe mostrar:

- `ok: true`
- `url` apuntando a tu URL de Render
- `last_error_message` vacio (o no presente)

## 6) Pruebas funcionales

1. En Telegram manda `/start`.
2. Manda `/modo`.
3. Envia una nota de voz corta.
4. Envia un audio mas largo para validar transcripcion por trozos y resumen.

## 7) Limitaciones esperadas en Free

- El servicio se apaga tras 15 min sin tráfico HTTP entrante (las tareas internas,
  como `retention-cleanup`, no cuentan). En los logs se ve como
  `Application is stopping`.
- El primer mensaje despues de dormir puede tardar (cold start). Si tarda demasiado,
  Telegram registra `Read timeout expired` en `getWebhookInfo` y reintenta la entrega.
- Para mantenerlo despierto:
  - El bot hace un keep-alive interno: cada `KEEP_ALIVE_INTERVAL_SECONDS` (600 por
    defecto, `0` lo desactiva) visita la raiz de su URL publica, derivada de `WEBHOOK_URL`.
  - Si aun asi se duerme o se reinicia, un monitor externo (UptimeRobot, cron-job.org)
    a `https://tu-servicio.onrender.com/` lo despierta y avisa por email si no responde
    (un 404 tambien cuenta como trafico).
  Las 750 h/mes del plan Free bastan para un servicio 24/7.
- Si Groq falla de forma transitoria al arrancar, el bot arranca igualmente (solo
  aborta si falta la clave o es invalida).
- El disco es efimero: `bot_data.pkl` (preferencias y mensajes pendientes de borrar)
  se pierde en cada deploy o reinicio.
- Hay limites mensuales del plan Free (horas/uso).

## 8) Troubleshooting rapido

- El bot no responde y no hay logs en Render:
  - consulta `getWebhookInfo`: si `pending_update_count` > 0 y `last_error_message`
    es `Read timeout expired`, Render no esta levantando la instancia.
  - haz `Manual Deploy > Deploy latest commit` (o `Restart`) y revisa el estado de
    horas Free en Billing.
- Error de variables faltantes:
  - revisa `TELEGRAM_TOKEN`, `GROQ_API_KEY`, `WEBHOOK_URL`.
- Webhook no llega:
  - valida `getWebhookInfo` y que la URL sea HTTPS publica.
- Timeouts en audio largo:
  - el bot ya divide audios largos automaticamente en trozos usando `ffmpeg`.
  - si sigue habiendo cortes, baja concurrencia (`PROCESSING_CONCURRENCY=1`) y revisa logs.
  - ajusta trozos con variables en `telegram_bot_groq.py`:
    - `AUDIO_CHUNK_SECONDS`
    - `AUDIO_CHUNK_OVERLAP_SECONDS`
    - `TRANSCRIBE_MAX_RETRIES`
