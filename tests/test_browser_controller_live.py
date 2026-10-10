import json
import os
import unittest

from scout.browser_controller import BrowserController


@unittest.skipUnless(
    os.getenv("SCOUT_CDP_ENDPOINT"),
    "configure SCOUT_CDP_ENDPOINT to run the live Microsoft Edge CDP test",
)
class LiveBrowserControllerTests(unittest.TestCase):
    def test_connects_and_reports_tabs_and_active_tab_as_json(self):
        controller = BrowserController()
        try:
            controller.connect()
            status = json.loads(json.dumps(controller.getStatus()))
        finally:
            controller.disconnect()

        self.assertTrue(status["connected"])
        self.assertEqual(status["browser"], "Microsoft Edge")
        self.assertIsInstance(status["tabs"], list)
        self.assertTrue(status["tabs"])
        for tab in status["tabs"]:
            self.assertIsInstance(tab["title"], str)
            self.assertIsInstance(tab["url"], str)
        self.assertIsNotNone(status["activeTab"])
        self.assertIn("title", status["activeTab"])
        self.assertIn("url", status["activeTab"])


if __name__ == "__main__":
    unittest.main()
