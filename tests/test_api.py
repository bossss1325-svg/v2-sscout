import json
import os
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

from scout.api import create_server
from scout.browser_operator import BrowserOperator, BrowserOperatorExecutor
from scout.discovery import MockSearchProvider
from scout.service import ScoutService
from scout.task_executor import TaskExecutor
from scout.storage import V2StateStore
from scout.v2_models import V2Lead


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(ScoutService(MockSearchProvider()), port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, payload):
        request = urllib.request.Request(
            f"{self.base_url}/scout/search", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def post_task(self, payload):
        request = urllib.request.Request(
            f"{self.base_url}/tasks", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def test_search_response_is_structured_sorted_and_retrievable(self):
        status, payload = self.post({
            "query": "dentistas", "city": "Porto Alegre", "state": "RS", "limit": 20,
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["errors"], 0)
        self.assertEqual(payload["results"][0]["companyName"], "Clínica Exemplo Porto Alegre")
        self.assertEqual(payload["results"][0]["source"], "mock://catalog")
        self.assertEqual(payload["results"][0]["websiteStatus"], "not_verified")
        self.assertIsNone(payload["results"][0]["hasWebsite"])
        self.assertIsNone(payload["results"][0]["phone"])

        lead_id = payload["results"][0]["id"]
        with urllib.request.urlopen(f"{self.base_url}/scout/lead/{lead_id}") as response:
            lead = json.loads(response.read())
        self.assertEqual(lead["id"], lead_id)

    def test_invalid_search_request_returns_400(self):
        status, payload = self.post({"query": "dentistas"})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_request")

    def test_health_endpoint(self):
        with urllib.request.urlopen(f"{self.base_url}/health") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["api_operational"])
        self.assertFalse(payload["operational"])
        self.assertFalse(payload["browser_connected"])
        self.assertFalse(payload["aivio_available"])
        self.assertIsNone(payload["browser_version"])
        self.assertEqual(payload["expected_edge_profile"], "EdgeProfile-V2")
        self.assertEqual(payload["cdp_endpoint"], "http://127.0.0.1:9223")

    def test_tasks_endpoint_runs_health_task_with_standard_result(self):
        task_id = str(uuid.uuid4())
        status, payload = self.post_task({
            "task_id": task_id,
            "type": "HEALTH_CHECK",
            "payload": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["type"], "HEALTH_CHECK")
        self.assertEqual(payload["status"], "completed")
        self.assertFalse(payload["data"]["browser_connected"])
        self.assertEqual(payload["errors"], [])
        with urllib.request.urlopen(f"{self.base_url}/tasks/{task_id}") as response:
            stored_result = json.loads(response.read())
        self.assertEqual(stored_result["task_id"], task_id)
        self.assertEqual(stored_result["status"], "completed")

    def test_tasks_endpoint_reports_unknown_task(self):
        request = urllib.request.Request(
            f"{self.base_url}/tasks/{uuid.uuid4()}",
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request)
        self.assertEqual(context.exception.code, 404)
        self.assertEqual(json.loads(context.exception.read())["error"], "task_not_found")
        context.exception.close()

    def test_tasks_endpoint_rejects_reused_task_id(self):
        task = {
            "task_id": str(uuid.uuid4()),
            "type": "HEALTH_CHECK",
            "payload": {},
        }
        self.assertEqual(self.post_task(task)[0], 200)
        status, payload = self.post_task(task)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_task")

    def test_tasks_endpoint_rejects_invalid_task(self):
        status, payload = self.post_task({"task_id": "invalid", "type": "NOPE", "payload": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_task")

    def test_operator_endpoint_accepts_generic_structured_task_and_stores_result(self):
        class FakeOperatorExecutor:
            def __init__(self):
                self.result = None

            def execute(self, task):
                self.result = {
                    "task_id": task["task_id"],
                    "status": "completed",
                    "action": {"index": 0, "name": "observe"},
                    "result": [{"title": "Example"}],
                    "evidence": [],
                    "operations": [],
                }
                return self.result

            def get_result(self, task_id):
                if self.result and self.result["task_id"] == task_id:
                    return self.result
                return None

        executor = FakeOperatorExecutor()
        server = create_server(
            ScoutService(MockSearchProvider()),
            port=0,
            operator_executor=executor,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        task_id = str(uuid.uuid4())
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/operator/tasks",
                data=json.dumps({
                    "task_id": task_id,
                    "actions": [{"action": "observe"}],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.loads(response.read())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["action"]["name"], "observe")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/operator/tasks/{task_id}"
            ) as response:
                stored_result = json.loads(response.read())
            self.assertEqual(stored_result["task_id"], task_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_task_timeout_is_not_returned_as_success(self):
        class TimeoutIntegration:
            def generate_site(self, lead, **kwargs):
                raise TimeoutError("generation exceeded deadline")

        server = create_server(
            ScoutService(MockSearchProvider()),
            port=0,
            task_executor=TaskExecutor(integration=TimeoutIntegration()),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/tasks",
                data=json.dumps({
                    "task_id": str(uuid.uuid4()),
                    "type": "GENERATE_SITE",
                    "payload": {"lead": {"company_name": "Empresa Exemplo"}},
                    "authorization": {
                        "allow_credit_consumption": True,
                        "allow_external_effects": True,
                    },
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            response = json.loads(context.exception.read())
            self.assertEqual(context.exception.code, 504)
            self.assertEqual(response["status"], "timeout")
            self.assertEqual(response["error"]["code"], "task_timeout")
            context.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_server_rejects_non_loopback_bind_addresses(self):
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            create_server(ScoutService(MockSearchProvider()), host="0.0.0.0", port=0)

    def test_legacy_server_creation_does_not_require_v2_cdp_configuration(self):
        with patch.dict(os.environ, {"SCOUT_CDP_ENDPOINT": "https://remote.example:9222"}):
            server = create_server(ScoutService(MockSearchProvider()), port=0)
        server.server_close()


class V2ResultDeliveryApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_store = V2StateStore(os.path.join(self.directory.name, "state.json"))

        class SearchIntegration:
            def search_leads(self, **kwargs):
                return {
                    "status": "completed",
                    "city": kwargs["city"],
                    "state": kwargs["state"],
                    "category": kwargs["category"],
                    "requested_quantity": kwargs["quantity"],
                    "results": [V2Lead(
                        company_name="Empresa Observada",
                        city=kwargs["city"],
                        state=kwargs["state"],
                        category=kwargs["category"],
                        source_url="https://directory.example/company/1",
                        raw_data={"search_result": {"text": "Empresa Observada"}},
                    ).to_dict()],
                    "warnings": [],
                }

        executor = TaskExecutor(
            integration=SearchIntegration(),
            state_store=self.state_store,
        )
        operator_executor = BrowserOperatorExecutor(state_store=self.state_store)
        self.server = create_server(
            ScoutService(MockSearchProvider()),
            port=0,
            task_executor=executor,
            operator_executor=operator_executor,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request_json(self, path, payload=None, method="GET"):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def test_auren_can_retrieve_and_acknowledge_persisted_search_package(self):
        task_id = str(uuid.uuid4())
        status, task_result = self.request_json("/tasks", {
            "task_id": task_id,
            "type": "SEARCH_LEADS",
            "payload": {
                "city": "Porto Alegre",
                "state": "RS",
                "category": "restaurantes",
                "quantity": 1,
                "objective": "Encontrar restaurantes",
            },
            "authorization": {
                "authorized_by": "auren",
                "allow_credit_consumption": True,
            },
        }, "POST")
        self.assertEqual(status, 200)
        package_id = task_result["result_package"]["package_id"]

        listed_status, pending = self.request_json("/v2/results/pending")
        loaded_status, package = self.request_json(f"/v2/results/{package_id}")
        failed_status, failed = self.request_json(
            f"/v2/results/{package_id}/delivery",
            {"status": "failed", "detail": "Auren temporariamente indisponível"},
            "POST",
        )
        delivered_status, delivered = self.request_json(
            f"/v2/results/{package_id}/delivery", {"status": "delivered"}, "POST",
        )
        repeated_status, repeated = self.request_json(
            f"/v2/results/{package_id}/delivery", {"status": "delivered"}, "POST",
        )

        self.assertEqual(listed_status, 200)
        self.assertEqual(pending["results"][0]["task_id"], task_id)
        self.assertEqual(loaded_status, 200)
        self.assertEqual(package["results"][0]["source_url"], "https://directory.example/company/1")
        self.assertEqual(failed_status, 200)
        self.assertEqual(failed["attempts"], 1)
        self.assertEqual(delivered_status, 200)
        self.assertEqual(repeated_status, 200)
        self.assertEqual(repeated["attempts"], 2)
        self.assertEqual(delivered["status"], "delivered")

    def test_generic_operator_also_delivers_evidence_package_to_auren(self):
        task_id = str(uuid.uuid4())
        result = {
            "task_id": task_id,
            "status": "completed",
            "action": None,
            "result": [{"text": "Página observada"}],
            "evidence": [{"after": {"url": "https://example.test/results"}}],
            "operations": [{"action": "observe", "status": "completed"}],
        }
        with patch.object(BrowserOperator, "execute", return_value=result):
            status, operator_result = self.request_json("/operator/tasks", {
                "task_id": task_id,
                "goal": "Inspecionar página de resultados",
                "actions": [{"action": "observe"}],
            }, "POST")
        package_id = operator_result["result_package"]["package_id"]

        package_status, package = self.request_json(f"/v2/results/{package_id}")

        self.assertEqual(status, 200)
        self.assertEqual(package_status, 200)
        self.assertEqual(package["objective"], "Inspecionar página de resultados")
        self.assertEqual(package["operator_result"]["evidence"], result["evidence"])

    def test_auren_can_submit_declarative_research_and_recover_structured_leads(self):
        task_id = str(uuid.uuid4())
        raw_record = {
            "title": "Restaurante observado",
            "text": "Restaurante observado\nTelefone: (51) 99999-0000\nAtende no salão",
            "links": [{"text": "Empresa", "url": "https://directory.example/place/1"}],
            "source_url": "https://directory.example/search",
        }
        browser_result = {
            "task_id": task_id,
            "status": "completed",
            "action": {"index": 1, "name": "read_records"},
            "result": [{"query": "restaurantes"}, {"count": 1, "records": [raw_record]}],
            "evidence": [],
            "operations": [
                {"action": "search", "status": "completed"},
                {"action": "read_records", "status": "completed"},
            ],
        }
        task = {
            "task_id": task_id,
            "goal": "Encontrar restaurantes locais",
            "query": "restaurantes",
            "quantity": 5,
            "niche": "restaurantes",
            "locality": "Porto Alegre, RS",
            "inclusion_criteria": ["Atende no salão"],
            "fields_of_interest": ["company_name", "phone", "source_url"],
        }
        with patch.object(BrowserOperator, "execute", return_value=browser_result):
            status, result = self.request_json("/operator/tasks", task, "POST")
        self.assertEqual(status, 200, result)

        package_status, package = self.request_json(
            f"/v2/results/{result['result_package']['package_id']}"
        )

        self.assertEqual(status, 200)
        self.assertEqual(package_status, 200)
        self.assertEqual(result["leads"][0]["phone"], "51999990000")
        self.assertEqual(package["objective"], "Encontrar restaurantes locais")
        self.assertEqual(package["locality"], "Porto Alegre, RS")
        self.assertEqual(package["results"][0]["qualification"]["status"], "qualified")


if __name__ == "__main__":
    unittest.main()