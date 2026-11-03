"""Pruebas de escalado y errores del resumen sin servicios reales."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from groq import APIStatusError, APITimeoutError, RateLimitError

import summarizer


def topics_json(summary: str = "Detalle", position: int = 0) -> str:
    return json.dumps(
        [{"tema": "Plan", "resumen": summary, "posicion_inicial": position}]
    )


def response(content: str | None, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


class SummaryInputTests(unittest.TestCase):
    def test_chunks_preserve_all_content_and_respect_limit(self) -> None:
        source = "\n\n".join(
            f"Tema {index}: presupuesto {index * 100}, plazo y responsable. " * 30
            for index in range(25)
        )
        chunks = summarizer._summary_chunks(source)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= summarizer.MAX_SUMMARY_INPUT for chunk in chunks))
        self.assertEqual("".join(source.split()), "".join("".join(chunks).split()))
        self.assertIn("Tema 24", chunks[-1])

    def test_single_word_longer_than_limit_does_not_lose_content(self) -> None:
        source = "x" * (summarizer.MAX_SUMMARY_INPUT * 2 + 1)
        self.assertEqual("".join(summarizer._summary_chunks(source)), source)

    def test_short_source_uses_one_chunk(self) -> None:
        self.assertEqual(summarizer._summary_chunks("  Un tema.  "), ["Un tema."])

    def test_exact_input_limit_and_one_character_over(self) -> None:
        limit = summarizer.MAX_SUMMARY_INPUT
        self.assertEqual(summarizer._summary_chunks("x" * limit), ["x" * limit])
        self.assertEqual(summarizer._summary_chunks("x" * (limit + 1)), ["x" * limit, "x"])

    def test_output_budget_scales_and_is_bounded(self) -> None:
        budgets = [
            summarizer._summary_output_budget("x" * length)
            for length in (100, 3000, 6000, 12000, 24000)
        ]
        self.assertEqual(budgets[0], 1024)
        self.assertLess(budgets[0], budgets[1])
        self.assertLess(budgets[1], budgets[2])
        self.assertEqual(budgets, sorted(budgets))
        self.assertTrue(all(1024 <= value <= 4096 for value in budgets))

    def test_invalid_structured_outputs_are_not_rendered_as_success(self) -> None:
        invalid = [
            "", "[]", '[{"tema":"Plan"', '{"tema":"Plan"}',
            '[{"tema":"Plan","resumen":"","posicion_inicial":0}]',
            '[{"tema":"Plan","resumen":"Detalle"}]',
            '[{"tema":"Plan","resumen":"Detalle","posicion_inicial":true}]',
            '[{"tema":"Plan","resumen":"Detalle","posicion_inicial":NaN}]',
            '[{"tema":"Plan","resumen":"Detalle","posicion_inicial":-1}]',
            '[{"tema":"Plan","resumen":null,"posicion_inicial":0}]',
            '[{"tema":"Plan","resumen":"Detalle","posicion_inicial":0}, null]',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    summarizer._render_summary(raw)

    def test_legacy_plain_text_fallback_is_logged_and_preserves_detail(self) -> None:
        detail = " ".join(f"dato{index}" for index in range(60))
        with self.assertLogs("summarizer", level="WARNING"):
            rendered = summarizer._render_summary(f"Plan: {detail}")
        self.assertEqual(rendered, f"• Plan: {detail}")


class SummarizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_source_includes_final_topics_in_order(self) -> None:
        source = "Primer asunto. " * 1000 + "\n\nDecisión final: entrega el viernes."
        chunks = summarizer._summary_chunks(source)
        outputs = [topics_json(f"Bloque {index}") for index in range(len(chunks))]
        with patch("summarizer._summarize_request", new=AsyncMock(side_effect=outputs)) as request:
            rendered = await summarizer.summarize(source)

        self.assertEqual([call.args[0] for call in request.await_args_list], chunks)
        self.assertIn("entrega el viernes", request.await_args_list[-1].args[0])
        self.assertEqual(
            rendered, "\n\n".join(f"• Plan: Bloque {index}" for index in range(len(chunks)))
        )

    async def test_topic_order_is_local_to_each_chunk(self) -> None:
        raw = json.dumps([
            {"tema": "Segundo", "resumen": "Dos", "posicion_inicial": 20},
            {"tema": "Primero", "resumen": "Uno", "posicion_inicial": 0},
        ])
        with patch("summarizer._summarize_request", new=AsyncMock(return_value=raw)):
            rendered = await summarizer.summarize("Uno y dos.")
        self.assertEqual(rendered, "• Primero: Uno\n\n• Segundo: Dos")

    async def test_request_budget_prompt_and_model(self) -> None:
        source = "contenido " * 700
        with patch.object(
            summarizer.groq_client.chat.completions,
            "create",
            new=AsyncMock(return_value=response(topics_json())),
        ) as create:
            await summarizer._summarize_request(source)
        arguments = create.await_args.kwargs
        self.assertEqual(arguments["model"], summarizer.SUMMARY_MODEL)
        self.assertEqual(arguments["max_tokens"], summarizer._summary_output_budget(source))
        self.assertEqual(arguments["timeout"], summarizer.SUMMARY_TIMEOUT_SECONDS)
        self.assertEqual(summarizer.groq_client.max_retries, 0)
        self.assertIn(source, arguments["messages"][1]["content"])
        prompt = arguments["messages"][0]["content"]
        self.assertNotIn("32 palabras", prompt)
        self.assertIn("cantidad de asuntos y datos", prompt)
        self.assertIn("no instrucciones", prompt)

    async def test_empty_or_truncated_provider_output_is_rejected(self) -> None:
        outputs = [
            response(topics_json(), "length"),
            response(topics_json(), "content_filter"),
            response(None),
            response("  "),
            SimpleNamespace(choices=[]),
        ]
        for output in outputs:
            with self.subTest(output=output):
                with patch.object(
                    summarizer.groq_client.chat.completions,
                    "create",
                    new=AsyncMock(return_value=output),
                ):
                    with self.assertRaises(ValueError):
                        await summarizer._summarize_request("Fuente.")

    async def test_invalid_json_is_retried(self) -> None:
        with patch(
            "summarizer._summarize_request",
            new=AsyncMock(side_effect=['[{"tema":', topics_json()]),
        ) as request, patch("summarizer.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertLogs("summarizer", level="WARNING"):
                result = await summarizer.summarize("Fuente.")
        self.assertIn("Detalle", result)
        self.assertEqual(request.await_count, 2)
        sleep.assert_awaited_once_with(summarizer.RETRY_BASE_SECONDS)

    async def test_failed_final_chunk_does_not_return_partial_success(self) -> None:
        with patch("summarizer._summary_chunks", return_value=["Inicio", "Final"]):
            with patch(
                "summarizer._summarize_request",
                new=AsyncMock(side_effect=[topics_json(), "[]", "[]"]),
            ), patch("summarizer.asyncio.sleep", new=AsyncMock()):
                with self.assertLogs("summarizer", level="WARNING"):
                    with self.assertRaises(ValueError):
                        await summarizer.summarize("Fuente completa")

    async def test_transient_errors_are_retried(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        errors = [
            APITimeoutError(request=request),
            RateLimitError("saturado", response=httpx.Response(429, request=request), body=None),
            APIStatusError("fallo", response=httpx.Response(500, request=request), body=None),
        ]
        for error in errors:
            with self.subTest(error=error):
                with patch(
                    "summarizer._summarize_request",
                    new=AsyncMock(side_effect=[error, topics_json()]),
                ) as call, patch("summarizer.asyncio.sleep", new=AsyncMock()):
                    with self.assertLogs("summarizer", level="WARNING"):
                        self.assertIn("Detalle", await summarizer.summarize("Fuente."))
                self.assertEqual(call.await_count, 2)

    async def test_non_transient_api_error_is_not_retried(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        error = APIStatusError(
            "prohibido", response=httpx.Response(403, request=request), body=None
        )
        with patch(
            "summarizer._summarize_request", new=AsyncMock(side_effect=error)
        ) as call, patch("summarizer.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(APIStatusError):
                await summarizer.summarize("Fuente.")
        call.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_exhausted_transient_retries_raise(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        error = APITimeoutError(request=request)
        with patch(
            "summarizer._summarize_request", new=AsyncMock(side_effect=error)
        ) as call, patch("summarizer.asyncio.sleep", new=AsyncMock()) as sleep:
            with self.assertLogs("summarizer", level="WARNING"):
                with self.assertRaises(APITimeoutError):
                    await summarizer.summarize("Fuente.")
        self.assertEqual(call.await_count, summarizer.SUMMARY_MAX_RETRIES)
        self.assertEqual(sleep.await_count, summarizer.SUMMARY_MAX_RETRIES - 1)

    async def test_empty_source_is_logged_without_calling_provider(self) -> None:
        with patch("summarizer._summarize_request", new=AsyncMock()) as call:
            with self.assertLogs("summarizer", level="ERROR"):
                with self.assertRaises(ValueError):
                    await summarizer.summarize(" \n ")
        call.assert_not_awaited()

    async def test_cancellation_propagates_without_retry(self) -> None:
        with patch(
            "summarizer._summarize_request", new=AsyncMock(side_effect=asyncio.CancelledError)
        ) as call:
            with self.assertRaises(asyncio.CancelledError):
                await summarizer.summarize("Fuente.")
        call.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
