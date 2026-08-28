"""Alias de compatibilidad: el código real vive en main.py.

Existe para que un Start Command desactualizado en el proveedor de hosting
(p. ej. Render con "python telegram_bot_groq.py") siga ejecutando la app modular.
"""

from main import main

if __name__ == "__main__":
    main()
