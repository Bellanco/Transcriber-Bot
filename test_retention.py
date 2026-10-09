"""Pruebas de caducidad de mensajes y estado local."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config import DATA_RETENTION_SECONDS, DATA_RETENTION_SWEEP_SECONDS, MAX_TELEGRAM_LENGTH
from formatter import stream_text
from main import _post_init
from telegram.error import TelegramError
from retention import (
    cleanup_expired_data,
    reply_with_retention,
    track_message,
    untrack_message,
)


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracks_message_with_one_day_expiry(self) -> None:
        application = SimpleNamespace(
            bot_data={},
            update_persistence=AsyncMock(),
        )
        message = SimpleNamespace(chat_id=123, message_id=456)

        await track_message(application, message)

        expiration = application.bot_data["message_expirations"][0]
        self.assertEqual(expiration["chat_id"], 123)
        self.assertEqual(expiration["message_id"], 456)
        self.assertAlmostEqual(expiration["expires_at"] - DATA_RETENTION_SECONDS, __import__("time").time(), delta=2)
        application.update_persistence.assert_awaited_once()

    async def test_cleanup_deletes_expired_messages_and_local_data(self) -> None:
        now = 200_000
        expired_message = {"chat_id": 1, "message_id": 2, "expires_at": now - 1}
        active_message = {"chat_id": 3, "message_id": 4, "expires_at": now + 60}
        stale_user_data = {"output_mode": "both", "_last_activity_at": now - DATA_RETENTION_SECONDS}
        active_user_data = {
            "output_mode": "summary",
            "_last_activity_at": now,
            "pending_forwarded_audios": [
                {"file_id": "old", "queued_at": now - DATA_RETENTION_SECONDS - 1},
                {"file_id": "new", "queued_at": now - 10},
            ],
        }
        application = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock()),
            bot_data={"message_expirations": [expired_message, active_message]},
            user_data={1: stale_user_data, 2: active_user_data},
            update_persistence=AsyncMock(),
        )

        await cleanup_expired_data(application, now=now)

        application.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=2)
        self.assertEqual(application.bot_data["message_expirations"], [active_message])
        self.assertEqual(stale_user_data, {})
        self.assertEqual(
            active_user_data["pending_forwarded_audios"],
            [{"file_id": "new", "queued_at": now - 10}],
        )
        application.update_persistence.assert_awaited_once()

    async def test_cleanup_retries_expired_messages_when_telegram_rejects_delete(self) -> None:
        now = 200_000
        expired_message = {"chat_id": 1, "message_id": 2, "expires_at": now - 1}
        application = SimpleNamespace(
            bot=SimpleNamespace(
                delete_message=AsyncMock(side_effect=TelegramError("forbidden"))
            ),
            bot_data={"message_expirations": [expired_message]},
            user_data={},
            update_persistence=AsyncMock(),
        )

        await cleanup_expired_data(application, now=now)

        self.assertEqual(application.bot_data["message_expirations"], [expired_message])
        application.update_persistence.assert_not_awaited()

    async def test_cleanup_starts_retention_for_existing_user_data(self) -> None:
        user_data = {"output_mode": "summary"}
        application = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock()),
            bot_data={},
            user_data={1: user_data},
            update_persistence=AsyncMock(),
        )

        await cleanup_expired_data(application, now=200_000)

        self.assertEqual(user_data["output_mode"], "summary")
        self.assertEqual(user_data["_last_activity_at"], 200_000)
        application.update_persistence.assert_awaited_once()

    async def test_reply_with_retention_tracks_sent_message(self) -> None:
        sent_message = SimpleNamespace(chat_id=123, message_id=456)
        source_message = SimpleNamespace(reply_text=AsyncMock(return_value=sent_message))
        user_data = {}
        application = SimpleNamespace(
            bot_data={},
            update_persistence=AsyncMock(),
        )

        result = await reply_with_retention(
            source_message, application, user_data, "resultado"
        )

        self.assertIs(result, sent_message)
        self.assertIn("_last_activity_at", user_data)
        self.assertEqual(application.bot_data["message_expirations"][0]["message_id"], 456)
        source_message.reply_text.assert_awaited_once_with("resultado")

    async def test_untrack_message_removes_persisted_expiration(self) -> None:
        message = SimpleNamespace(chat_id=123, message_id=456)
        application = SimpleNamespace(
            bot_data={
                "message_expirations": [
                    {"chat_id": 123, "message_id": 456, "expires_at": 100},
                    {"chat_id": 123, "message_id": 789, "expires_at": 100},
                ]
            },
            update_persistence=AsyncMock(),
        )

        await untrack_message(application, message)

        self.assertEqual(
            application.bot_data["message_expirations"],
            [{"chat_id": 123, "message_id": 789, "expires_at": 100}],
        )
        application.update_persistence.assert_awaited_once()

    async def test_post_init_schedules_periodic_retention_sweep(self) -> None:
        job_queue = SimpleNamespace(run_repeating=Mock())
        application = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock()),
            bot_data={},
            user_data={},
            update_persistence=AsyncMock(),
            job_queue=job_queue,
        )

        await _post_init(application)

        job_queue.run_repeating.assert_called_once()
        self.assertEqual(
            job_queue.run_repeating.call_args.kwargs["interval"],
            DATA_RETENTION_SWEEP_SECONDS,
        )

    async def test_streamed_transcription_tracks_each_sent_message(self) -> None:
        sent_messages = [
            SimpleNamespace(chat_id=123, message_id=1, edit_text=AsyncMock()),
            SimpleNamespace(chat_id=123, message_id=2, edit_text=AsyncMock()),
        ]
        source_message = SimpleNamespace(reply_text=AsyncMock(side_effect=sent_messages))
        application = SimpleNamespace(
            bot_data={},
            update_persistence=AsyncMock(),
        )

        with patch("config.STREAM_DELAY", 0):
            await stream_text(
                source_message,
                f"{'a' * MAX_TELEGRAM_LENGTH}\n\nb",
                application=application,
            )

        self.assertEqual(
            [item["message_id"] for item in application.bot_data["message_expirations"]],
            [1, 2],
        )
        self.assertEqual(source_message.reply_text.await_count, 2)


if __name__ == "__main__":
    unittest.main()