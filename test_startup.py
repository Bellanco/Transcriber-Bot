"""Pruebas de arranque resiliente y keep-alive."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
import utils


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_groq_failure_does_not_block_startup(self) -> None:
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("timeout")))
            )
        )
        with patch("utils.AsyncGroq", return_value=client):
            is_valid, msg = await utils.validate_groq_api("key")

        self.assertTrue(is_valid)
        self.assertIn("Aviso", msg)

    def test_keep_alive_url_uses_service_root(self) -> None:
        with patch(
            "main.resolve_webhook_settings",
            return_value=("https://bot.onrender.com/webhook", "webhook"),
        ):
            self.assertEqual(main._keep_alive_url(), "https://bot.onrender.com/")

    def test_keep_alive_url_is_none_without_webhook(self) -> None:
        with patch("main.resolve_webhook_settings", side_effect=EnvironmentError()):
            self.assertIsNone(main._keep_alive_url())

    async def test_post_init_schedules_keep_alive(self) -> None:
        job_queue = SimpleNamespace(run_repeating=Mock())
        application = SimpleNamespace(
            bot=SimpleNamespace(delete_message=AsyncMock()),
            bot_data={},
            user_data={},
            update_persistence=AsyncMock(),
            job_queue=job_queue,
        )

        with patch("main._keep_alive_url", return_value="https://bot.onrender.com/"), patch(
            "main.KEEP_ALIVE_INTERVAL_SECONDS", 600
        ):
            await main._post_init(application)

        names = [call.kwargs["name"] for call in job_queue.run_repeating.call_args_list]
        self.assertEqual(names, ["retention-cleanup", "keep-alive"])
        keep_alive_call = job_queue.run_repeating.call_args_list[1]
        self.assertEqual(keep_alive_call.kwargs["data"], "https://bot.onrender.com/")
        self.assertEqual(keep_alive_call.kwargs["interval"], 600)

    async def test_keep_alive_job_swallows_network_errors(self) -> None:
        context = SimpleNamespace(job=SimpleNamespace(data="https://bot.onrender.com/"))
        with patch("main.httpx.AsyncClient.get", new=AsyncMock(side_effect=main.httpx.ConnectError("x"))):
            await main._keep_alive_job(context)


if __name__ == "__main__":
    unittest.main()
