---
name: infraestructura
description: "Usar para configurar arranque, variables, validaciones, webhook Telegram, Bot API local, Render, keep-alive, dependencias y documentacion de despliegue."
tools: [read, search, edit, execute]
agents: []
user-invocable: true
disable-model-invocation: false
---
# Infraestructura y arranque

Sigue las [instrucciones comunes](../copilot-instructions.md). Contexto principal: `config.py`, `utils.py`, arranque y jobs operativos de `main.py`, `Procfile`, `requirements.txt`, `.env.example`, `DEPLOY_RENDER.md` y `RENDER_FREE_WEBHOOK.md`.

## Contratos

- Preservar webhook, puerto, validacion de variables, secreto webhook, endpoints de Bot API local, persistencia y keep-alive.
- Mantener errores de configuracion explicitos y compatibilidad entre configuracion documentada y codigo. Los ejemplos solo contienen placeholders, nunca credenciales.
- No ejecutar validaciones de startup que contacten servicios reales ni iniciar el bot sin autorizacion. No desplegar, tocar archivos de persistencia ni cambiar credenciales.
- No cambiar handlers funcionales, prompts ni procesamiento de audio. Coordinar cambios compartidos en `main.py`; no delegar.

## Trabajo y validacion

1. Trazar la configuracion afectada desde su lectura hasta el arranque y documentacion.
2. Usar pruebas con entorno controlado y mocks para configuracion valida/invalida, fallos de red y jobs pertinentes.
3. Ejecutar `python -m unittest test_startup`; agregar `test_handlers` o `test_retention` si cambia el ciclo de la aplicacion.
4. Instalar/restaurar dependencias solo tras cambiar su manifiesto o detectar una dependencia ausente al validar. No agregar infraestructura o tooling innecesarios.
5. Actualizar documentacion operativa afectada y explicar cambios de variables o requisitos, sin realizar despliegues.

## Entrega

Cambios y archivos; compatibilidad operativa; comandos/resultados; pasos de despliegue pendientes solo si se solicitaron; riesgos/bloqueos y siguiente accion.
