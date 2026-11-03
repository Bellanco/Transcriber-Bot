"""Pruebas del formato de resumen."""

import unittest

from formatter import _format_summary_from_topics, format_summary


class SummaryFormattingTests(unittest.TestCase):
    def test_preserves_relevant_detail_in_long_summary_point(self) -> None:
        summary = (
            "Considera mudarse en enero o febrero tras ahorrar dos meses, aunque reconoce "
            "que el ahorro será insuficiente para la vivienda y que no espera ayudas "
            "políticas para la clase media."
        )

        rendered = _format_summary_from_topics(
            [{"tema": "Plan de mudanza y ahorro", "resumen": summary, "posicion_inicial": 0}]
        )

        self.assertIn("el ahorro será insuficiente para la vivienda", rendered)
        self.assertIn("no espera ayudas políticas para la clase media", rendered)
        self.assertNotIn("...", rendered)

    def test_points_over_32_words_preserve_final_conditions(self) -> None:
        summary = " ".join(f"dato{index}" for index in range(50))
        summary += " condicionado a la aprobación del viernes"
        rendered = _format_summary_from_topics(
            [{"tema": "Decisión", "resumen": summary, "posicion_inicial": 0}]
        )
        self.assertEqual(rendered, f"• Decisión: {summary}")
        self.assertEqual(format_summary(f"Decisión: {summary}"), rendered)

    def test_preserves_all_topics_and_sorts_by_position(self) -> None:
        topics = [
            {"tema": f"Tema {index}", "resumen": f"Detalle {index}", "posicion_inicial": index}
            for index in reversed(range(30))
        ]
        rendered = _format_summary_from_topics(topics)
        self.assertEqual(
            rendered.split("\n\n"),
            [f"• Tema {index}: Detalle {index}" for index in range(30)],
        )


if __name__ == "__main__":
    unittest.main()