# 🎙️ Bot de Transcripción - Guía de Arquitectura

## Estructura Modular

La aplicación está organizada en módulos Python siguiendo patrones estándar:

### 📁 Módulos

| Módulo | Responsabilidad |
|--------|-----------------|
| **`config.py`** | Variables de entorno y constantes centralizadas |
| **`utils.py`** | Validación (API/archivos), helpers de Telegram, formateo |
| **`formatter.py`** | Formateo de transcripciones, párrafos, resúmenes |
| **`transcriber.py`** | Lógica de transcripción con Groq Whisper + manejo de audios largos |
| **`summarizer.py`** | Generación de resúmenes con Groq llama |
| **`handlers.py`** | Handlers de comandos y procesamiento de audios |
| **`main.py`** | Punto de entrada: validación al startup + configuración webhook |

### 🔄 Dependencias entre módulos

```
main.py
  ├── config.py (constantes globales)
  ├── utils.py (validación de startup)
  │   └── config.py
  └── handlers.py (lógica de eventos)
      ├── config.py
      ├── utils.py (safe_edit, validate_downloaded_file)
      ├── transcriber.py (transcribe, transcribe_long_audio)
      │   ├── config.py
      │   ├── formatter.py (parse_transcription_result, etc)
      │   │   ├── config.py
      │   │   └── formatter.py helpers
      │   └── utils.py (safe_edit)
      ├── summarizer.py (summarize)
      │   └── config.py
      └── formatter.py (stream_text)
```

---

## ✨ Mejoras Implementadas

### 1. **Modularización**
- Separación de responsabilidades en 7 módulos especializados
- Facilita testing y reutilización de código
- Mejora legibilidad y mantenibilidad

### 2. **Validación Robusta**
- ✅ Validación de `GROQ_API_KEY` al iniciar (test de conexión)
- ✅ Validación de archivo descargado (existe + tamaño > 0)
- ✅ Validación de variables de entorno obligatorias
- ✅ Mensajes de error específicos y útiles

### 3. **Mensajes de Carga Mejorados**
- 🎙️ Tipo de archivo y duración en primer mensaje
- 📥 Progreso de descarga con tamaño del archivo
- 🔧 Estado detallado para audios largos (`X/Y trozos`)
- ⏳ Indicadores visuales con emojis (✅, ❌, ⏳, 🔧)

### 4. **Mejor Manejo de Errores**
- Errores diferenciados con mensajes específicos:
  - Saturación de servicio (rate limiting)
  - Timeout en transcripción
  - API key inválida
  - ffmpeg no disponible
  - Archivo descargado inválido
- Logging detallado para debugging

### 5. **Logging de Startup**
- Validación de Groq API con resultado visible
- Detección automática de ffmpeg
- Información de webhook al iniciar
- Timeline clara de validaciones

---

## 🚀 Ejecución

### Desarrollo Local
```bash
# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno
export TELEGRAM_TOKEN="..."
export GROQ_API_KEY="..."
export WEBHOOK_URL="https://tu-dominio.com"
export WEBHOOK_SECRET="tu-secreto-aleatorio"

# Ejecutar
python main.py
```

### Deploy en Render
- El `Procfile` ya apunta a `python main.py`
- Todas las validaciones se ejecutan al startup
- Si alguna validación falla, el servicio se detiene con código de error

---

## 🔧 Configuración Avanzada

### Modo Bot API Local
Para usar servidor Bot API local en lugar de la nube:

```bash
export TELEGRAM_LOCAL_MODE=true
export TELEGRAM_API_BASE_URL="http://localhost:8081"
export TELEGRAM_API_FILE_URL="http://localhost:8081"
```

### Webhook Flexible
- `WEBHOOK_URL` con path: `https://tu-servicio.com/webhook`
- `WEBHOOK_URL` sin path: se añade automáticamente `WEBHOOK_PATH`

---

## 📊 Constantes Configurables

En `config.py`:

```python
SUMMARY_MIN_SECONDS = 40          # Duración mínima para resumen
MAX_FILE_SIZE_MB = 20             # Límite de tamaño
LONG_AUDIO_THRESHOLD_SECONDS = 1200  # 20 min
AUDIO_CHUNK_SECONDS = 300         # Trozos de 5 min
TRANSCRIBE_MAX_RETRIES = 3        # Reintentos en error
PROCESSING_CONCURRENCY = 2        # Audios simultáneos
```

---

## 🧪 Testing de Módulos

```bash
# Test de sintaxis
python -m py_compile *.py

# Test de imports
python -c "import config, utils, formatter, transcriber, summarizer, handlers; print('✅ OK')"

# Test de validación de Groq API
python -c "
import asyncio
from utils import validate_groq_api
import os
api_key = os.environ.get('GROQ_API_KEY')
is_valid, msg = asyncio.run(validate_groq_api(api_key))
print(msg)
"
```

---

## 📝 Notas de Implementación

- Todos los módulos son compatibles con Python 3.8+
- Type hints incluidos donde es relevante
- Logging con formato coherente
- Manejo de excepciones específico en cada capa
- Compatible con Bot API local y nube
- Soporta audios de cualquier duración (con ffmpeg)
