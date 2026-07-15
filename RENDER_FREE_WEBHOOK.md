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
4. Envia un audio mas largo para validar resumen.

## 7) Limitaciones esperadas en Free

- Puede haber cold start tras inactividad.
- El primer mensaje despues de dormir puede tardar.
- Hay limites mensuales del plan Free (horas/uso).

## 8) Troubleshooting rapido

- Error de variables faltantes:
  - revisa `TELEGRAM_TOKEN`, `GROQ_API_KEY`, `WEBHOOK_URL`.
- Webhook no llega:
  - valida `getWebhookInfo` y que la URL sea HTTPS publica.
- Timeouts en audio largo:
  - prueba audios mas cortos o baja concurrencia (`PROCESSING_CONCURRENCY=1`).
