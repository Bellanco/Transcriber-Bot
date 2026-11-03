# Trabajo en Transcriber-Bot

- Este proyecto es un bot Python asincrono con python-telegram-bot, Groq y ffmpeg; no aplicar patrones de React/MVVM. Consultar [arquitectura](../ARCHITECTURE.md) y [uso](../README.md) antes de cambiar contratos.
- `main.py` configura el runtime; `handlers.py` orquesta Telegram; `transcriber.py` procesa audio; `summarizer.py` resume; `formatter.py` comparte contratos de salida; `retention.py` limpia mensajes y estado; `config.py` y `utils.py` gestionan configuracion y validacion.
- Leer el estado de Git y el codigo relevante antes de editar. Conservar cambios ajenos y hacer cambios completos pero acotados, siguiendo nombres, estilo, mensajes en espanol y patrones existentes. Actualizar solo documentacion relacionada.
- Preservar asincronia, modos de usuario, persistencia, limpieza de temporales, limites de Telegram, reintentos y errores visibles. No introducir bloqueos del event loop, fallos silenciosos ni respuestas de exito ante errores.
- No leer ni mostrar `.env`, tokens, claves, transcripciones privadas o archivos de persistencia. Consultar nombres y placeholders en `.env.example`. No enviar contenido a servicios externos ni iniciar el bot, llamadas de pago, despliegues, commits o pushes sin peticion explicita.
- Ejecutar pruebas focalizadas con `python -m unittest <modulos>`; la suite existente se ejecuta con `python -m unittest discover`. Usar fixtures y mocks, no Groq ni Telegram reales. Revisar `git diff --check`. No agregar linters o dependencias solo para validar customizaciones.
- No confundir tests de formato con evaluacion de calidad del resumen. Informar comandos, resultados y limitaciones reales; nunca declarar comprobado lo que no se ejecuto.
- Usar rutas y sintaxis compatibles con el sistema anfitrion; en PowerShell no asumir operadores de PowerShell 7 ni expansion de comodines para comandos Python.
- Los agentes de `.github/agents` son asistentes de desarrollo de VS Code, no componentes runtime del bot. Heredan el modelo del entorno.
