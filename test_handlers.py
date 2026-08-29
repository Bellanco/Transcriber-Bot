"""Pruebas del flujo de audio sin depender de Telegram ni Groq."""

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import handlers
from config import LONG_AUDIO_THRESHOLD_SECONDS, MAX_FILE_SIZE_BYTES


class FakeStatusMessage:
    """Mensaje mínimo para observar las respuestas del handler."""

    def __init__(self) -> None:
        self.texts = []

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)

    async def delete(self) -> None:
        return None

    async def reply_text(self, text: str, **kwargs: object) -> "FakeStatusMessage":
        self.texts.append(text)
        return self


class FakeIncomingMessage(FakeStatusMessage):
    """Mensaje de voz de prueba con metadatos configurables."""

    def __init__(self, size: int, duration: int) -> None:
        super().__init__()
        self.voice = SimpleNamespace(file_id="file-id", file_size=size, duration=duration)
        self.audio = None
        self.video_note = None
        self.status = FakeStatusMessage()

    async def reply_text(self, text: str, **kwargs: object) -> FakeStatusMessage:
        self.texts.append(text)
        if text.startswith("⏳ Procesando"):
            return self.status
        return self


class FakeTelegramFile:
    """Descarga un archivo temporal no vacío para las pruebas."""

    async def download_to_drive(self, path: str) -> None:
        Path(path).write_bytes(b"audio")


def make_context() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(get_file=AsyncMock(return_value=FakeTelegramFile())),
        user_data={"output_mode": "transcription"},
    )


class HandleAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unknown_file_size(self) -> None:
        message = FakeIncomingMessage(size=0, duration=10)
        context = make_context()

        await handlers.handle_audio(SimpleNamespace(message=message), context)

        self.assertIn("No se pudo validar el tamaño", message.texts[0])
        context.bot.get_file.assert_not_awaited()

    async def test_rejects_file_over_limit_before_download(self) -> None:
        message = FakeIncomingMessage(size=MAX_FILE_SIZE_BYTES + 1, duration=10)
        context = make_context()

        await handlers.handle_audio(SimpleNamespace(message=message), context)

        self.assertIn("supera el límite", message.texts[0])
        context.bot.get_file.assert_not_awaited()

    async def test_processes_short_audio(self) -> None:
        message = FakeIncomingMessage(size=1024, duration=10)
        context = make_context()

        with patch("handlers.transcribe", new=AsyncMock(return_value=("texto", "texto"))) as transcribe:
            with patch("handlers.stream_text", new=AsyncMock(return_value=message)):
                await handlers.handle_audio(SimpleNamespace(message=message), context)

        transcribe.assert_awaited_once()

    async def test_accepts_file_at_size_limit(self) -> None:
        message = FakeIncomingMessage(size=MAX_FILE_SIZE_BYTES, duration=10)
        context = make_context()

        with patch("handlers.transcribe", new=AsyncMock(return_value=("texto", "texto"))) as transcribe:
            with patch("handlers.stream_text", new=AsyncMock(return_value=message)):
                await handlers.handle_audio(SimpleNamespace(message=message), context)

        transcribe.assert_awaited_once()

    async def test_processes_long_audio_with_chunking(self) -> None:
        message = FakeIncomingMessage(size=1024, duration=LONG_AUDIO_THRESHOLD_SECONDS)
        context = make_context()

        with patch("handlers._ffmpeg_is_available", return_value=True):
            with patch(
                "handlers.transcribe_long_audio",
                new=AsyncMock(return_value=("texto", "texto")),
            ) as transcribe_long_audio:
                with patch("handlers.stream_text", new=AsyncMock(return_value=message)):
                    await handlers.handle_audio(SimpleNamespace(message=message), context)

        transcribe_long_audio.assert_awaited_once()

    async def test_cancels_transcription_after_timeout(self) -> None:
        message = FakeIncomingMessage(size=1024, duration=10)
        context = make_context()

        async def never_finishes(*args: object, **kwargs: object) -> tuple[str, str]:
            await asyncio.Event().wait()
            return "", ""

        with patch("handlers._estimate_transcription_timeout", return_value=0.01):
            with patch("handlers.transcribe", new=never_finishes):
                await handlers.handle_audio(SimpleNamespace(message=message), context)

        self.assertIn("tardó demasiado y se canceló", message.status.texts[-1])


if __name__ == "__main__":
    unittest.main()