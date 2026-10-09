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
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)

    async def delete(self) -> None:
        self.deleted = True

    async def reply_text(self, text: str, **kwargs: object) -> "FakeStatusMessage":
        self.texts.append(text)
        return self


class FakeIncomingMessage(FakeStatusMessage):
    """Mensaje de voz de prueba con metadatos configurables."""

    def __init__(self, size: int, duration: int) -> None:
        super().__init__()
        self.message_id = 123
        self.chat_id = 456
        self.voice = SimpleNamespace(file_id="file-id", file_size=size, duration=duration)
        self.audio = None
        self.video_note = None
        self.status = FakeStatusMessage()

    async def reply_text(self, text: str, **kwargs: object) -> FakeStatusMessage:
        self.texts.append(text)
        if text.startswith("Procesando"):
            return self.status
        return self


class FakeTelegramFile:
    """Descarga un archivo temporal no vacío para las pruebas."""

    async def download_to_drive(self, path: str) -> None:
        Path(path).write_bytes(b"audio")


def make_context() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            get_file=AsyncMock(return_value=FakeTelegramFile()),
            delete_message=AsyncMock(),
        ),
        user_data={"output_mode": "transcription"},
        application=SimpleNamespace(update_persistence=AsyncMock()),
    )


class HandleAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_selection_persists_immediately(self) -> None:
        query = SimpleNamespace(
            data="mode:summary",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        context = make_context()

        await handlers.handle_mode_callback(SimpleNamespace(callback_query=query), context)

        self.assertEqual(context.user_data["output_mode"], "summary")
        context.application.update_persistence.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()

    async def test_queues_forwarded_audio_until_mode_is_selected(self) -> None:
        message = FakeIncomingMessage(size=1024, duration=10)
        message.forward_origin = SimpleNamespace(
            sender_user=SimpleNamespace(first_name="Lucía", last_name="Martín")
        )
        context = make_context()

        await handlers.handle_audio(SimpleNamespace(message=message), context)

        self.assertEqual(len(context.user_data["pending_forwarded_audios"]), 1)
        self.assertEqual(context.user_data["pending_forwarded_audios"][0]["message_id"], 123)
        self.assertEqual(
            context.user_data["pending_forwarded_audios"][0]["summary_author"],
            "Lucía Martín",
        )
        self.assertIn("Elige el resultado", message.texts[0])
        context.bot.get_file.assert_not_awaited()

    async def test_selected_forwarded_batch_is_removed_before_processing(self) -> None:
        message = FakeIncomingMessage(size=1024, duration=10)
        query = SimpleNamespace(
            data="batch_mode:summary",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=message,
        )
        context = make_context()
        audio_data = {
            "file_id": "file-id",
            "ext": "ogg",
            "duration": 10,
            "size": 1024,
            "audio_type": "Nota de voz",
        }
        context.user_data["pending_forwarded_audios"] = [audio_data]

        with patch("handlers._process_audio", new=AsyncMock()) as process_audio:
            await handlers.handle_mode_callback(
                SimpleNamespace(callback_query=query), context
            )

        self.assertNotIn("pending_forwarded_audios", context.user_data)
        context.application.update_persistence.assert_awaited_once()
        process_audio.assert_awaited_once_with(audio_data, message, context)
        self.assertTrue(message.deleted)

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
        self.assertTrue(message.deleted)

    async def test_deletes_forwarded_audio_after_processing(self) -> None:
        message = FakeStatusMessage()
        context = make_context()
        audio_data = {
            "file_id": "file-id",
            "ext": "ogg",
            "duration": 10,
            "size": 1024,
            "audio_type": "Nota de voz",
            "message_id": 789,
            "chat_id": 456,
        }

        with patch("handlers.transcribe", new=AsyncMock(return_value=("texto", "texto"))):
            with patch("handlers.stream_text", new=AsyncMock(return_value=message)):
                await handlers._process_audio(audio_data, message, context)

        context.bot.delete_message.assert_awaited_once_with(chat_id=456, message_id=789)

    async def test_summary_heading_uses_forwarded_sender_name(self) -> None:
        message = FakeStatusMessage()
        context = make_context()
        context.user_data["output_mode"] = "summary"
        audio_data = {
            "file_id": "file-id",
            "ext": "ogg",
            "duration": 10,
            "size": 1024,
            "audio_type": "Nota de voz",
            "summary_author": "Lucía Martín",
        }

        with patch("handlers.transcribe", new=AsyncMock(return_value=("texto", "texto"))):
            with patch("handlers.summarize", new=AsyncMock(return_value="• Plan: detalle")):
                await handlers._process_audio(audio_data, message, context)

        self.assertTrue(any(text.startswith("Resumen de Lucía Martín:") for text in message.texts))

    def test_forwarded_sender_name_supports_hidden_users_and_chats(self) -> None:
        hidden_origin = SimpleNamespace(sender_user_name="Nombre oculto")
        chat_origin = SimpleNamespace(sender_chat=SimpleNamespace(title="Canal de noticias"))

        self.assertEqual(
            handlers._forwarded_sender_name(SimpleNamespace(forward_origin=hidden_origin)),
            "Nombre oculto",
        )
        self.assertEqual(
            handlers._forwarded_sender_name(SimpleNamespace(forward_origin=chat_origin)),
            "Canal de noticias",
        )

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