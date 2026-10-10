import unittest

from scout.models import Lead
from scout.research import CompanyResearcher


class ResearchTests(unittest.TestCase):
    def test_unsourced_optional_data_and_signals_are_removed(self):
        lead = Lead(
            "Clínica Exemplo", "dentistas", "Porto Alegre", "RS", "mock",
            phone="5551999999999", whatsapp="https://wa.me/5551999999999",
            website="https://example.invalid", instagram="https://instagram.com/example",
            address="Rua Inventada, 123", signals={"active": True},
            sources={key: "mock://fixture" for key in ("companyName", "category", "city", "state")},
        )

        researched = CompanyResearcher().research(lead)

        self.assertIsNone(researched.phone)
        self.assertIsNone(researched.whatsapp)
        self.assertIsNone(researched.website)
        self.assertIsNone(researched.instagram)
        self.assertIsNone(researched.address)
        self.assertEqual(researched.signals, {})

    def test_missing_source_for_required_field_is_rejected(self):
        lead = Lead("Empresa", "categoria", "Cidade", "RS", "mock")

        with self.assertRaisesRegex(ValueError, "campo obrigatório sem fonte"):
            CompanyResearcher().research(lead)

    def test_confirmed_no_website_is_scored_but_unverified_is_not(self):
        source = "mock://fixture"
        sourced = {key: source for key in ("companyName", "category", "city", "state")}
        sourced_lead = Lead("Sem site", "dentistas", "Porto Alegre", "RS", source,
                            website_status="not_found", sources=sourced)
        unknown_lead = Lead("Incerto", "dentistas", "Porto Alegre", "RS", source, sources=sourced)

        researched_no_site = CompanyResearcher().research(sourced_lead)
        researched_unknown = CompanyResearcher().research(unknown_lead)

        self.assertEqual(researched_no_site.opportunity_score, 40)
        self.assertEqual(researched_no_site.website_status, "not_found")
        self.assertFalse(researched_no_site.to_dict()["hasWebsite"])
        self.assertEqual(researched_unknown.opportunity_score, 0)
        self.assertEqual(researched_unknown.website_status, "not_verified")
        self.assertIsNone(researched_unknown.to_dict()["hasWebsite"])


if __name__ == "__main__":
    unittest.main()