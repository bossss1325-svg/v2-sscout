import unittest

from scout.models import Lead
from scout.scoring import classify_opportunity, score_lead


class ScoringTests(unittest.TestCase):
    def test_no_website_increases_score(self):
        lead = Lead("Clínica Exemplo", "dentista", "Porto Alegre", "RS", "mock", website_status="not_found")

        result = score_lead(lead)

        self.assertEqual(result.value, 40)
        self.assertEqual(result.level, "medium")
        self.assertIn("Nenhum site identificado na pesquisa", result.reasons)

    def test_unknown_signals_do_not_add_points(self):
        lead = Lead("Empresa", "serviço", "Canoas", "RS", "mock")

        result = score_lead(lead)

        self.assertEqual(result.value, 0)
        self.assertEqual(result.reasons, ())

    def test_scores_are_capped_and_classified(self):
        lead = Lead(
            "Empresa", "serviço", "Gravataí", "RS", "mock", website_status="not_found",
            signals={"active": True, "strongPublicPresence": True, "positiveReputation": True,
                     "commercialCategory": True, "outdatedSite": True},
        )

        result = score_lead(lead)

        self.assertEqual(result.value, 77)
        self.assertEqual(result.level, "good")

    def test_classification_boundaries(self):
        self.assertEqual(classify_opportunity(39), "low")
        self.assertEqual(classify_opportunity(40), "medium")
        self.assertEqual(classify_opportunity(70), "good")
        self.assertEqual(classify_opportunity(85), "high")


if __name__ == "__main__":
    unittest.main()