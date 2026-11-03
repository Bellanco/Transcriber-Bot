---
name: transcripcion
description: "Usar para desarrollar transcripcion de audio con Groq Whisper, ffmpeg, fragmentacion de audio largo, solapamiento, deduplicacion, segmentos, limites y reintentos."
tools: [read, search, edit, execute]
agents: []
user-invocable: true
disable-model-invocation: false
---
# Transcripcion de audio

Sigue las [instrucciones comunes](../copilot-instructions.md). Contexto principal: `transcriber.py`, normalizacion de segmentos en `formatter.py`, constantes relacionadas de `config.py` y `test_transcriber.py`.

## Contratos

- Preservar audio corto/largo, idioma y formato de Whisper, orden temporal, texto completo y compatibilidad con handlers.
- Revisar duracion, fragmentacion y solapamiento: no perder palabras ni duplicar segmentos en fronteras. Mantener limpieza de archivos y errores explicitos de ffmpeg.
- Conservar reintentos acotados, timeouts y tratamiento de rate-limit/errores del proveedor. No introducir llamadas sincronas bloqueantes en rutas asincronas.
- No cambiar resumen IA, sus prompts ni el contrato JSON por temas. Coordinar cambios de `handlers.py` o funciones compartidas; no delegar.

## Trabajo y validacion

1. Leer el flujo afectado y reutilizar normalizacion y helpers existentes.
2. Probar con fixtures y mocks fronteras de chunks, segmentos vacios o solapados y fallos relevantes; medir cualquier cambio propuesto de umbrales.
3. Ejecutar `python -m unittest test_transcriber`; agregar `test_formatter` o `test_handlers` si cambia su contrato. No llamar a Groq real.
4. Documentar requisitos ffmpeg o cambios de procesamiento directamente relacionados.

## Entrega

Cambios y archivos; contratos preservados; comandos y resultados; riesgos/bloqueos y siguiente accion. Distinguir tests con mocks de una evaluacion real de precision.
