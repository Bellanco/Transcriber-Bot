---
name: telegram
description: "Usar para desarrollar comandos y callbacks de Telegram, modos, mensajes, audios reenviados, persistencia, retencion y entrega de transcripciones o resumenes."
tools: [read, search, edit, execute]
agents: []
user-invocable: true
disable-model-invocation: false
---
# Telegram y retencion

Sigue las [instrucciones comunes](../copilot-instructions.md). Trabaja principalmente en `handlers.py`, `retention.py`, registro de eventos en `main.py` y funciones de entrega de `formatter.py`.

## Contratos

- Mantener comandos, callbacks, modos de transcripcion/resumen/ambos, preferencias persistentes y agrupacion de audios reenviados.
- Preservar validacion de archivos, mensajes de progreso/error, limites de envio, concurrencia y limpieza de temporales incluso al fallar.
- Mantener el seguimiento de mensajes y la limpieza de preferencias y audios inactivos. Verificar aislamiento entre usuarios y chats.
- No cambiar prompts, Whisper, parsing del resumen ni configuracion de despliegue fuera del objetivo. Coordinar cambios de interfaces con el coordinador; no delegar.

## Trabajo y validacion

1. Trazar solo el evento afectado desde el handler hasta la salida y limpieza.
2. Corregir la causa y cubrir con mocks escenarios normales y de error, callbacks repetidos y estado persistido cuando sean relevantes.
3. Ejecutar `python -m unittest test_handlers test_retention`; incluir `test_formatter` si cambia la entrega y `test_startup` si cambia el registro de eventos.
4. Actualizar documentacion de comandos o comportamiento si cambia la UX. No usar el bot real para validar.

## Entrega

Cambios y archivos; contratos preservados o cambios intencionales; comandos y resultados; riesgos/bloqueos y siguiente accion. No editar fuera de los archivos acordados sin comunicar la necesidad.
