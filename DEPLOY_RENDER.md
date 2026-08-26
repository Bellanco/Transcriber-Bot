# 🚀 Guía Rápida de Deploy en Render

## Paso 1: Preparar el repositorio

```bash
# Si aún no lo has hecho, inicializa Git
git init
git add .
git commit -m "Modularización completa + validaciones"

# Pushea a GitHub
git push origin main
```

**Nota:** El archivo `telegram_bot_groq.py` es el código antiguo. Puedes dejarlo como backup o eliminarlo.

---

## Paso 2: Crear servicio en Render

1. Entra a [render.com](https://render.com)
2. Click en **"New → Web Service"**
3. Conecta tu repositorio de GitHub
4. Configura:

```
Name:              transcriber-bot
Environment:       Python 3
Region:            Elige la más cercana
Branch:            main
Root Directory:    (dejar vacío)
Build Command:     pip install -r requirements.txt
Start Command:     python main.py
```

---

## Paso 3: Configurar Variables de Entorno

En Render Dashboard → Environment Variables:

```
TELEGRAM_TOKEN=<tu_token_de_botfather>
GROQ_API_KEY=<tu_clave_api_de_groq>
WEBHOOK_URL=https://transcriber-bot.onrender.com/webhook
WEBHOOK_SECRET=<valor-aleatorio-largo-y-seguro>
PORT=8000
```

**Donde encontrar cada variable:**

- **TELEGRAM_TOKEN**: [@BotFather](https://t.me/botfather) en Telegram
  - `/newbot` → nombre → username → Te da el token
  
- **GROQ_API_KEY**: [console.groq.com/keys](https://console.groq.com/keys)
  - Crea una clave API nueva
  
- **WEBHOOK_URL**: Se auto-genera en Render
  - Format: `https://[nombre-servicio].onrender.com/webhook`
  - Reemplaza `transcriber-bot` con el nombre que uses
  
- **WEBHOOK_SECRET**: Genera un valor aleatorio
  ```bash
  # En tu terminal local
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

---

## Paso 4: Deploy

1. Click en **"Create Web Service"**
2. Render iniciará el build automáticamente
3. Espera a que diga **"Live"** (puede tardar 2-3 minutos)

---

## Paso 5: Verificar Logs

1. En Render Dashboard, click en tu servicio
2. Ve a **"Logs"**
3. Deberías ver:

```
🚀 Iniciando Bot de Transcripción
======================================================================
✅ API de Groq validada correctamente.
✅ ffmpeg detectado: procesamiento de audios largos habilitado
✅ Variables de entorno validadas.
======================================================================
✅ Todas las validaciones pasaron
======================================================================

🌐 Configuración de Webhook
======================================================================
Escuchando en: 0.0.0.0:8000
Path: /webhook
Webhook URL: https://transcriber-bot.onrender.com/webhook
======================================================================
```

**Si ves errores:**
- ❌ "GROQ_API_KEY inválida" → Verifica que copiaste correctamente
- ❌ "TELEGRAM_TOKEN vacío" → Verifica que está en variables de entorno
- ❌ "WEBHOOK_URL no está definido" → Verifica formato y nombre del servicio

---

## Paso 6: Test del Bot

1. Abre Telegram y busca tu bot por username
2. Envía `/start`
3. Deberías recibir:
   ```
   🎙️ Bot de Transcripción de Audios
   
   Envía una nota de voz o archivo de audio y recibirás la transcripción.
   
   Si el audio supera los 40 segundos, también recibirás un resumen.
   
   Comandos:
     /modo — Activar/desactivar resúmenes automáticos
     /ayuda — Ver ayuda
   ```

4. Envía una nota de voz de prueba (10-20 segundos)
5. Deberías recibir la transcripción
6. Si es > 40s, deberías recibir resumen también

---

## 🔧 Troubleshooting

### El bot no responde
- Verifica logs en Render
- Confirma que STATUS es "Live" (no "Building" o "Deploy Failed")
- Verifica WEBHOOK_SECRET es igual en BotFather y Render

### Error "Validación fallida"
- Lee el log específico (GROQ_API_KEY, TELEGRAM_TOKEN, etc.)
- Verifica que copiastes sin espacios ni caracteres extras

### Transcripción muy lenta
- Esto es normal, primeras transcripciones pueden tardar 10-15s
- Groq tiene cold starts

### Transcripción devuelve error
- Si es audio muy largo (> 20 min), podría fallar sin ffmpeg
- Prueba con audios más cortos
- En Render Free, ffmpeg puede no estar disponible

---

## ✅ Checklist Final

- [ ] Repositorio pusheado a GitHub
- [ ] Servicio creado en Render
- [ ] TELEGRAM_TOKEN configurado
- [ ] GROQ_API_KEY configurado
- [ ] WEBHOOK_URL configurado correctamente
- [ ] WEBHOOK_SECRET configurado
- [ ] Deploy en "Live"
- [ ] Logs muestran validaciones OK
- [ ] Bot responde a /start
- [ ] Transcripción funciona
- [ ] Resumen funciona (para audios > 40s)

---

## 🎉 ¡Listo!

Tu bot está en producción. Ahora puedes:
- Compartir el link con usuarios
- Monitorear logs en Render
- Hacer updates: Git push → Render rebuild automático

---

## 📚 Referencias Rápidas

- [Documentación de Render](https://render.com/docs)
- [Render dashboard](https://dashboard.render.com)
- [Groq console](https://console.groq.com)
- [BotFather](https://t.me/botfather)

---

## 💡 Pro Tips

1. **Auto-deploy en cambios:**
   - Render detecta cambios en GitHub automáticamente
   - Solo push → automático rebuild

2. **Ver logs en tiempo real:**
   ```bash
   # CLI de Render (opcional)
   render logs <service-id>
   ```

3. **Redeployar manualmente:**
   - Dashboard → Servicio → Click "Deploy" (flecha)

4. **Rollback:**
   - Dashboard → Servicio → "Deployments" → Click en versión anterior

---

**¿Alguna duda? Revisa los logs en Render o consulta la documentación en ARCHITECTURE.md**
