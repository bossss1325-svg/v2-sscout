import unittest
from email.message import Message

from scout.website import FetchResult, WebsiteAnalyzer


class FakeClient:
    def __init__(self, status=200, body="", allowed=True, content_type="text/html; charset=utf-8"):
        self.allowed = allowed
        headers = Message()
        headers["Content-Type"] = content_type
        self.response = FetchResult(status, headers, body.encode("utf-8"), 12)

    def can_fetch(self, url):
        return self.allowed

    def fetch(self, url):
        return self.response


class WebsiteTests(unittest.TestCase):
    def test_working_site_extracts_public_contact_and_mobile_signals(self):
        html = (
            '<html><head><title>Clínica</title><meta name="viewport" content="width=device-width">'
            '</head><body><main><h1>Serviços odontológicos</h1><a href="tel:+555130001111">Ligue</a>'
            '<a href="https://wa.me/555130001111">WhatsApp</a><form></form>'
            '<a href="/contato">Agende</a></main></body></html>'
        )
        analysis = WebsiteAnalyzer(FakeClient(body=html)).analyze("https://clinic.example/")

        self.assertEqual(analysis.status, "working")
        self.assertTrue(analysis.https)
        self.assertTrue(analysis.mobile_friendly)
        self.assertEqual(analysis.phone_number, "+555130001111")
        self.assertEqual(analysis.whatsapp_url, "https://wa.me/555130001111")
        self.assertTrue(analysis.has_form)
        self.assertTrue(analysis.has_call_to_action)
        self.assertEqual(analysis.quality_score, 100)
        self.assertIsNone(analysis.appearance_score)

    def test_broken_site_is_reported(self):
        analysis = WebsiteAnalyzer(FakeClient(status=503)).analyze("https://clinic.example/")

        self.assertEqual(analysis.status, "broken")
        self.assertIn("Resposta HTTP 503", analysis.findings)

    def test_http_and_missing_mobile_viewport_are_measured(self):
        analyzer = WebsiteAnalyzer(FakeClient(body="<html><body>Conteúdo</body></html>"))
        result = analyzer.analyze("http://clinic.example/")

        self.assertFalse(result.https)
        self.assertFalse(result.mobile_friendly)
        self.assertLess(result.quality_score, 100)

    def test_robots_denial_prevents_page_fetch(self):
        client = FakeClient(allowed=False)
        result = WebsiteAnalyzer(client).analyze("https://clinic.example/")

        self.assertEqual(result.status, "blocked_by_robots")
        self.assertIn("não realizada", result.findings[0])

    def test_unlabeled_button_is_not_counted_as_a_call_to_action(self):
        result = WebsiteAnalyzer(FakeClient(body="<html><body><button>Próximo</button></body></html>"))
        analysis = result.analyze("https://clinic.example/")

        self.assertFalse(analysis.has_call_to_action)


if __name__ == "__main__":
    unittest.main()