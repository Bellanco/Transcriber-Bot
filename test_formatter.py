"""Pruebas del formato de resumen."""

import unittest

from formatter import _format_summary_from_topics


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


if __name__ == "__main__":
    unittest.main()