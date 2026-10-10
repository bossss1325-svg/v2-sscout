import json
import os
import unittest
import uuid

from scout.aivio import AivioIntegration, CDP_ENDPOINT
from scout.browser_controller import BrowserController
from scout.task_executor import TaskExecutor


def _live_task_authorized():
    task_type = os.getenv("SCOUT_AIVIO_LIVE_TASK")
    try:
        payload = json.loads(os.getenv("SCOUT_AIVIO_LIVE_PAYLOAD", "{}"))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    authorization = {
        "allow_credit_consumption": os.getenv("SCOUT_AIVIO_LIVE_ALLOW_CREDIT_CONSUMPTION") == "1",
        "allow_external_effects": os.getenv("SCOUT_AIVIO_LIVE_ALLOW_EXTERNAL_EFFECTS") == "1",
        "authorized_by": os.getenv("SCOUT_AIVIO_LIVE_AUTHORIZED_BY", "").casefold(),
    }
    if task_type in {"SEARCH_LEADS"}:
        return authorization["allow_credit_consumption"] and authorization["authorized_by"] == "auren"
    if task_type == "OPEN_COMPANY":
        lead = payload.get("lead", {})
        if isinstance(lead, dict) and not (lead.get("company_url") or lead.get("source_url")):
            return authorization["allow_credit_consumption"]
    if task_type == "GENERATE_SITE":
        return (
            authorization["allow_credit_consumption"]
            and authorization["allow_external_effects"]
        )
    return task_type == "HEALTH_CHECK"


@unittest.skipUnless(
    os.getenv("AIVIO_LIVE_TEST") == "1"
    and os.getenv("AIVIO_TEST_CITY", "").strip()
    and os.getenv("AIVIO_ALLOW_CREDIT_CONSUMPTION") == "1",
    "set AIVIO_LIVE_TEST, AIVIO_TEST_CITY and AIVIO_ALLOW_CREDIT_CONSUMPTION=1 for a live search",
)
class LiveAivioIntegrationTests(unittest.TestCase):
    def test_searches_the_active_aivio_page(self):
        controller = BrowserController(cdp_endpoint=CDP_ENDPOINT)
        try:
            controller.connect()
            result = AivioIntegration(controller).search_city(
                os.environ["AIVIO_TEST_CITY"],
                open_first_company=os.getenv("AIVIO_OPEN_FIRST_COMPANY") == "1",
                allow_credit_consumption=True,
            )
        finally:
            controller.disconnect()

        encoded_result = json.loads(json.dumps(result, ensure_ascii=False))
        self.assertEqual(encoded_result["status"], "completed")
        self.assertEqual(encoded_result["city"], os.environ["AIVIO_TEST_CITY"].strip())
        self.assertIsInstance(encoded_result["results"], list)
        if os.getenv("AIVIO_OPEN_FIRST_COMPANY") == "1":
            self.assertIsNotNone(encoded_result["companyDetails"])


@unittest.skipUnless(
    os.getenv("SCOUT_AIVIO_LIVE_TEST") == "1"
    and os.getenv("SCOUT_AIVIO_LIVE_TASK", "").strip()
    and os.getenv("SCOUT_AIVIO_LIVE_PAYLOAD", "").strip()
    and _live_task_authorized(),
    "configure SCOUT_AIVIO_LIVE_TEST, task/payload and task-specific authorization variables",
)
class LiveV2TaskTests(unittest.TestCase):
    def test_executes_explicit_v2_task_against_aivio(self):
        task_type = os.environ["SCOUT_AIVIO_LIVE_TASK"]
        payload = json.loads(os.environ["SCOUT_AIVIO_LIVE_PAYLOAD"])
        result = TaskExecutor().execute({
            "task_id": str(uuid.uuid4()),
            "type": task_type,
            "payload": payload,
            "authorization": {
                "allow_credit_consumption":
                    os.getenv("SCOUT_AIVIO_LIVE_ALLOW_CREDIT_CONSUMPTION") == "1",
                "allow_external_effects":
                    os.getenv("SCOUT_AIVIO_LIVE_ALLOW_EXTERNAL_EFFECTS") == "1",
                "authorized_by": os.getenv("SCOUT_AIVIO_LIVE_AUTHORIZED_BY", "").casefold(),
            },
        })
        self.assertNotIn(result.status.value, {"failed", "timeout"}, msg="; ".join(result.errors))
        self.assertTrue(result.finished_at)


if __name__ == "__main__":
    unittest.main()
