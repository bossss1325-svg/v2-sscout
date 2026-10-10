import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from scout.aivio import CDP_ENDPOINT
from scout.lead_normalization import (
    deduplicate_leads,
    extract_lead,
    normalize_company_name,
    normalize_phone,
    normalize_text,
    normalize_url,
)
from scout.storage import V2StateStore
from scout.task_executor import TaskExecutor, _browser_execution_lock
from scout.v2_delivery import build_result_package, evaluate_lead_criteria, validate_result_package
from scout.v2_models import SearchLeadsPayload, Task, TaskResult, TaskStatus, TaskType, V2Lead


def task_request(task_type, payload=None, authorization=None):
    request = {
        "task_id": str(uuid.uuid4()),
        "type": task_type,
        "payload": payload or {},
    }
    if authorization is not None:
        request["authorization"] = authorization
    return request


class FakeIntegration:
    def __init__(self):
        self.health = {
            "browser_connected": True,
            "active_tab": {"title": "AIVIO", "url": "https://app.aivio.example"},
            "aivio_available": True,
        }
        self.generate_error = None
        self.search_authorization = None
        self.open_authorization = None
        self.generate_authorization = None

    def search_leads(self, **kwargs):
        self.search_authorization = kwargs.get("allow_credit_consumption")
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress({"phase": "results_ready", "page": 1, "record_count": 1})
        return {
            "status": "completed",
            "city": kwargs["city"],
            "state": kwargs["state"],
            "category": kwargs["category"],
            "requested_quantity": kwargs["quantity"],
            "results": [V2Lead(company_name="Restaurante Exemplo", city=kwargs["city"],
                               state=kwargs["state"], category=kwargs["category"],
                               raw_data={"search_result": {
                                   "title": "Restaurante Exemplo",
                                   "url": "https://app.aivio.example/company/123",
                               }}).to_dict()],
            "warnings": [],
        }

    def open_company(self, lead, **kwargs):
        self.open_authorization = kwargs.get("allow_credit_consumption")
        lead.phone = "5511999999999"
        return lead

    def generate_site(self, lead, **kwargs):
        self.generate_authorization = kwargs
        if self.generate_error:
            raise self.generate_error
        return lead, [{"type": "website", "url": "https://cliente.aivio.example", "title": "Restaurante"}], []

    def browser_health(self):
        return self.health


class TaskContractTests(unittest.TestCase):
    def test_search_payload_preserves_city_value(self):
        for city in ("Porto Alegre", "Canoas", "Caxias do Sul"):
            with self.subTest(city=city):
                task = Task.from_dict(task_request(
                    "SEARCH_LEADS",
                    {"city": city, "state": "RS", "category": "restaurantes"},
                    authorization={"allow_credit_consumption": True, "authorized_by": "auren"},
                ))
                self.assertEqual(task.payload.city, city)

    def test_search_requires_boss_supplied_city_and_category(self):
        base = {"city": "Porto Alegre", "state": "RS", "category": "restaurantes"}
        authorization = {"allow_credit_consumption": True, "authorized_by": "auren"}
        for missing in ("city", "category"):
            with self.subTest(missing=missing):
                payload = {key: value for key, value in base.items() if key != missing}
                with self.assertRaisesRegex(ValueError, f"payload\\.{missing}"):
                    Task.from_dict(task_request(
                        "SEARCH_LEADS",
                        payload,
                        authorization=authorization,
                    ))

    def test_search_task_validates_required_payload_and_uuid(self):
        task = Task.from_dict(task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
            "quantity": 10, "filters": {"has_website": False},
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"}))
        self.assertEqual(task.type, TaskType.SEARCH_LEADS)
        self.assertIsInstance(task.payload, SearchLeadsPayload)
        self.assertEqual(task.payload.quantity, 10)
        default_quantity_task = Task.from_dict(task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"}))
        self.assertEqual(default_quantity_task.payload.quantity, 10)
        detailed_task = Task.from_dict(task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
            "objective": "Encontrar restaurantes sem site",
            "inclusion_criteria": ["atende no salão"],
            "exclusion_criteria": ["rede nacional"],
            "fields_of_interest": ["company_name", "website"],
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"}))
        self.assertEqual(detailed_task.payload.objective, "Encontrar restaurantes sem site")
        self.assertEqual(detailed_task.payload.inclusion_criteria, ["atende no salão"])
        self.assertEqual(detailed_task.payload.fields_of_interest, ["company_name", "website"])
        for quantity in (0, 11, 100, True):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(ValueError, "entre 1 e 10"):
                    Task.from_dict(task_request("SEARCH_LEADS", {
                        "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
                        "quantity": quantity,
                    }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"}))
        with self.assertRaisesRegex(ValueError, "UUID"):
            Task.from_dict({"task_id": "not-a-uuid", "type": "HEALTH_CHECK", "payload": {}})
        with self.assertRaisesRegex(ValueError, "payload.state"):
            Task.from_dict(task_request("SEARCH_LEADS", {
                "city": "Porto Alegre", "category": "restaurantes",
            }))
        with self.assertRaisesRegex(ValueError, "allow_credit_consumption"):
            Task.from_dict(task_request("SEARCH_LEADS", {
                "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
            }))
        with self.assertRaisesRegex(ValueError, "do Auren"):
            Task.from_dict(task_request("SEARCH_LEADS", {
                "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
            }, authorization={"allow_credit_consumption": True}))
        with self.assertRaisesRegex(ValueError, "identificar Auren"):
            Task.from_dict(task_request("SEARCH_LEADS", {
                "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
            }, authorization={"allow_credit_consumption": True, "authorized_by": "operator"}))

    def test_lead_round_trip_has_standard_fields(self):
        lead = V2Lead(company_name="Restaurante Exemplo", city="Porto Alegre")
        encoded = lead.to_dict()
        decoded = V2Lead.from_dict(encoded)
        self.assertEqual(decoded.to_dict(), encoded)
        self.assertIsNone(encoded["has_website"])
        self.assertEqual(encoded["source"], "AIVIO")

    def test_result_package_has_sources_missing_fields_and_human_summary(self):
        task = Task.from_dict(task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"}))
        lead = V2Lead(
            company_name="Restaurante Observado",
            city="Porto Alegre",
            state="RS",
            source_url="https://directory.example/place/1",
            raw_data={"search_result": {"text": "Restaurante Observado"}},
        )
        result = TaskResult(
            task_id=task.task_id,
            type=TaskType.SEARCH_LEADS,
            status=TaskStatus.PARTIAL,
            leads=[lead],
            started_at="2026-10-10T10:00:00+00:00",
            finished_at="2026-10-10T10:01:00+00:00",
        )

        package = validate_result_package(build_result_package(task, result))

        self.assertEqual(package["found_count"], 1)
        self.assertEqual(package["results"][0]["evidence"][0]["source_url"], lead.source_url)
        self.assertIn("phone", package["results"][0]["missing_or_unverified"])
        self.assertIn("1/10", package["summary"])
        self.assertEqual(package["delivery"]["status"], "pending")

    def test_negated_text_does_not_satisfy_or_trigger_criteria(self):
        result = evaluate_lead_criteria(
            "a empresa não é franquia e não atende no salão",
            ["atende no salão"],
            ["franquia"],
        )

        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["inclusion_evidence"][0]["matched"])
        self.assertTrue(result["inclusion_evidence"][0]["negated"])
        self.assertFalse(result["exclusion_evidence"][0]["matched"])
        self.assertTrue(result["exclusion_evidence"][0]["negated"])


class LeadNormalizationTests(unittest.TestCase):
    def test_normalizes_text_phone_url_and_company_name(self):
        self.assertEqual(normalize_text("  Rua  Central\n  10 "), "Rua Central 10")
        self.assertEqual(normalize_phone("+55 (51) 99999-0000"), "+5551999990000")
        self.assertEqual(normalize_url("  example.com/path/  "), "https://example.com/path")
        self.assertIsNone(normalize_url("javascript:alert(1)"))
        self.assertEqual(normalize_company_name("  Clínica São José! "), "clinica sao jose")

    def test_extracts_observed_lead_fields_and_preserves_raw_input(self):
        raw = {
            "title": "Restaurante Exemplo",
            "url": "https://app.aivio.example/company/1",
            "text": "Telefone: +55 (51) 99999-0000\nEndereço: Rua Central, 10",
            "links": [
                {"title": "Site", "url": "example.com"},
                {"title": "Instagram", "url": "https://instagram.com/exemplo"},
            ],
        }
        lead = extract_lead(raw, city="Porto Alegre", state="RS", category="restaurantes")
        self.assertEqual(lead.company_name, "Restaurante Exemplo")
        self.assertEqual(lead.phone, "+5551999990000")
        self.assertEqual(lead.address, "Rua Central, 10")
        self.assertEqual(lead.website, "https://example.com/")
        self.assertEqual(lead.instagram, "https://instagram.com/exemplo")
        self.assertTrue(lead.has_website)
        self.assertEqual(lead.raw_data["search_result"], raw)

    def test_urls_and_raw_data_redact_sensitive_parameters_and_tokens(self):
        url = normalize_url(
            "https://app.aivio.example/company/1?access_token=secret&view=leads#private"
        )
        self.assertEqual(url, "https://app.aivio.example/company/1?view=leads")

        lead = extract_lead({
            "title": "Empresa Exemplo",
            "url": "https://app.aivio.example/company/2?session=private",
            "text": "Bearer abc.def.ghi",
            "links": [],
            "metadata": {"access_token": "raw-secret"},
        })
        self.assertEqual(lead.source_url, "https://app.aivio.example/company/2")
        self.assertNotIn("private", str(lead.raw_data))
        self.assertNotIn("raw-secret", str(lead.raw_data))
        self.assertNotIn("Bearer", str(lead.raw_data))

    def test_deduplication_uses_phone_domain_name_and_company_url(self):
        first = V2Lead(company_name="Loja A", city="Porto Alegre", phone="5511999999999")
        same_phone = V2Lead(company_name="Loja B", city="São Paulo", phone="5511999999999")
        same_domain = V2Lead(company_name="Loja C", website="https://www.exemplo.com")
        same_domain_alt_url = V2Lead(company_name="Loja D", website="https://exemplo.com/")
        same_name_city = V2Lead(company_name="Clínica São José", city="Porto Alegre")
        same_name_city_duplicate = V2Lead(company_name="Clinica Sao Jose", city="Porto Alegre")
        same_company_url = V2Lead(company_name="Outra razão social", company_url="https://app.aivio.example/1")
        same_company_url_duplicate = V2Lead(company_name="Outra empresa", company_url="https://app.aivio.example/1")
        unique_similar = V2Lead(company_name="Clínica São Jose", city="Canoas")

        result = deduplicate_leads([
            first, same_phone, same_domain, same_domain_alt_url,
            same_name_city, same_name_city_duplicate,
            same_company_url, same_company_url_duplicate, unique_similar,
        ])
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].company_name, "Loja A")
        self.assertTrue(result[0].raw_data["duplicates"])

    def test_shared_search_source_url_does_not_deduplicate_distinct_companies(self):
        first = V2Lead(
            company_name="Restaurante Um",
            city="Porto Alegre",
            source_url="https://directory.example/search",
        )
        second = V2Lead(
            company_name="Restaurante Dois",
            city="Porto Alegre",
            source_url="https://directory.example/search",
        )

        result = deduplicate_leads([first, second])

        self.assertEqual([lead.company_name for lead in result], ["Restaurante Um", "Restaurante Dois"])


class V2StateStoreTests(unittest.TestCase):
    def test_task_records_and_leads_survive_store_reinitialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            lead = V2Lead(company_name="Empresa Persistida").to_dict()
            record = {"status": "completed", "progress": {"pages": 1}}

            first = V2StateStore(path)
            first.save_task_record("task-1", record)
            first.replace_leads([lead])
            reopened = V2StateStore(path)

            self.assertEqual(reopened.get_task_record("task-1"), record)
            self.assertTrue(reopened.task_exists("task-1"))
            self.assertEqual(reopened.load_leads(), [lead])

    def test_open_store_instances_merge_task_updates_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            first = V2StateStore(path)
            second = V2StateStore(path)

            first.save_task_record("task-1", {"status": "completed"})
            second.save_task_record("task-2", {"status": "running"})

            self.assertEqual(
                set(first.load_task_records()),
                {"task-1", "task-2"},
            )

    def test_task_id_reservation_is_atomic_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            first = V2StateStore(path)
            second = V2StateStore(path)

            self.assertTrue(first.create_task_record("task-1", {"status": "queued"}))
            self.assertFalse(second.create_task_record("task-1", {"status": "queued"}))

    def test_old_state_schema_is_read_without_losing_existing_v2_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            legacy_task = {"task_id": "task-1", "status": "completed"}
            with open(path, "w", encoding="utf-8") as state_file:
                import json

                json.dump({"tasks": {"task-1": legacy_task}, "leads": {}}, state_file)

            store = V2StateStore(path)
            store.save_result_package("task-1", {
                "package_id": "package-1",
                "delivery": {"status": "pending", "attempts": 0},
            })
            reopened = V2StateStore(path)

            self.assertEqual(reopened.get_task_record("task-1"), legacy_task)
            self.assertEqual(reopened.get_result_package("package-1")["package_id"], "package-1")

    def test_operator_history_and_package_delivery_survive_reinitialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            first = V2StateStore(path)
            record = {"task_id": "operator-1", "status": "completed", "operations": [{"action": "observe"}]}
            package = {
                "package_id": "package-1",
                "delivery": {
                    "status": "pending", "attempts": 0,
                    "last_attempt_at": None, "delivered_at": None, "error": None,
                },
            }
            self.assertTrue(first.create_operator_task("operator-1", record))
            first.save_result_package("operator-1", package)
            first.record_package_delivery("package-1", "failed", detail="Auren indisponível")

            reopened = V2StateStore(path)

            self.assertEqual(reopened.get_operator_task("operator-1"), record)
            self.assertEqual(reopened.get_result_package("operator-1")["delivery"]["status"], "failed")
            self.assertEqual(reopened.get_result_package("package-1")["delivery"]["attempts"], 1)


class TaskExecutorTests(unittest.TestCase):
    def setUp(self):
        self.state_directory = tempfile.TemporaryDirectory()
        self.integration = FakeIntegration()
        self.state_path = os.path.join(self.state_directory.name, "state.json")
        self.executor = TaskExecutor(
            integration=self.integration,
            state_store=V2StateStore(self.state_path),
        )

    def tearDown(self):
        self.state_directory.cleanup()

    def test_executes_search_and_returns_standardized_result(self):
        request = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})
        result = self.executor.execute(request).to_dict()
        self.assertEqual(result["task_id"], request["task_id"])
        self.assertEqual(result["type"], "SEARCH_LEADS")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["leads"][0]["company_name"], "Restaurante Exemplo")
        self.assertTrue(result["started_at"])
        self.assertTrue(result["finished_at"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["result"]["leads"][0]["company_name"], "Restaurante Exemplo")
        self.assertEqual(result["result_package"]["task_id"], request["task_id"])
        self.assertEqual(result["data"]["package_id"], result["result_package"]["package_id"])
        self.assertIsNone(result["error"])
        self.assertTrue(self.integration.search_authorization)

    def test_search_task_and_progress_survive_executor_restart(self):
        request = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})

        completed = self.executor.execute(request)
        restarted = TaskExecutor(
            integration=FakeIntegration(),
            state_store=V2StateStore(self.state_path),
        )

        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        recovered_task = restarted.get_task_result(request["task_id"])
        self.assertEqual(recovered_task["progress"]["phase"], "completed")
        self.assertEqual(recovered_task["progress_events"][0]["phase"], "results_ready")
        self.assertEqual(len(restarted.state_store.load_leads()), 1)
        package = restarted.get_result_package(request["task_id"])
        self.assertEqual(package["task_id"], request["task_id"])
        self.assertEqual(package["delivery"]["status"], "pending")
        with self.assertRaisesRegex(ValueError, "já foi executado"):
            restarted.execute(request)

    def test_interrupted_search_is_marked_failed_without_automatic_replay(self):
        request = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})
        task_id = request["task_id"]
        interrupted = TaskResult(
            task_id=task_id,
            type=TaskType.SEARCH_LEADS,
            status=TaskStatus.RUNNING,
            started_at="2026-10-09T00:00:00+00:00",
            finished_at="",
        ).to_dict()
        interrupted["_task_request"] = request
        self.executor.state_store.save_task_record(task_id, interrupted)

        restarted = TaskExecutor(
            integration=FakeIntegration(),
            state_store=V2StateStore(self.state_path),
        )
        recovered = restarted.get_task_result(task_id)

        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"]["code"], "task_interrupted")
        self.assertEqual(recovered["result_package"]["execution"]["status"], "failed")
        self.assertEqual(recovered["result_package"]["found_count"], 0)
        self.assertIsNone(restarted.integration.search_authorization)

    def test_restart_does_not_mark_another_live_process_task_as_interrupted(self):
        task_id = str(uuid.uuid4())
        active = TaskResult(
            task_id=task_id,
            type=TaskType.SEARCH_LEADS,
            status=TaskStatus.RUNNING,
            worker_pid=os.getpid(),
            finished_at="",
        ).to_dict()
        self.executor.state_store.save_task_record(task_id, active)

        restarted = TaskExecutor(
            integration=FakeIntegration(),
            state_store=V2StateStore(self.state_path),
        )

        self.assertEqual(restarted.get_task_result(task_id)["status"], "running")

    def test_duplicate_lead_from_later_batch_is_not_saved_or_returned_twice(self):
        first = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})
        second = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre", "state": "RS", "category": "restaurantes",
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})

        first_result = self.executor.execute(first)
        second_result = self.executor.execute(second)

        self.assertEqual(len(first_result.leads), 1)
        self.assertEqual(second_result.leads, [])
        self.assertEqual(len(self.executor.state_store.load_leads()), 1)
        self.assertEqual(
            second_result.result_package["duplicates"][0]["company_name"],
            "Restaurante Exemplo",
        )

    def test_search_applies_only_observed_criteria_and_preserves_exclusions(self):
        class CriteriaIntegration(FakeIntegration):
            def search_leads(self, **kwargs):
                self.search_authorization = kwargs.get("allow_credit_consumption")
                return {
                    "status": "completed",
                    "city": kwargs["city"],
                    "state": kwargs["state"],
                    "category": kwargs["category"],
                    "requested_quantity": kwargs["quantity"],
                    "results": [
                        V2Lead(
                            company_name="Restaurante Local",
                            city=kwargs["city"], state=kwargs["state"], category=kwargs["category"],
                            source_url="https://directory.example/local",
                            raw_data={"search_result": {"text": "Atende no salão"}},
                        ).to_dict(),
                        V2Lead(
                            company_name="Rede Nacional",
                            city=kwargs["city"], state=kwargs["state"], category=kwargs["category"],
                            source_url="https://directory.example/chain",
                            raw_data={"search_result": {"text": "Rede nacional"}},
                        ).to_dict(),
                    ],
                    "warnings": [],
                }

        executor = TaskExecutor(
            integration=CriteriaIntegration(),
            state_store=V2StateStore(self.state_path),
        )
        request = task_request("SEARCH_LEADS", {
            "city": "Porto Alegre",
            "state": "RS",
            "category": "restaurantes",
            "quantity": 2,
            "inclusion_criteria": ["Atende no salão"],
            "exclusion_criteria": ["Rede nacional"],
        }, authorization={"allow_credit_consumption": True, "authorized_by": "auren"})

        result = executor.execute(request)

        self.assertEqual([lead.company_name for lead in result.leads], ["Restaurante Local"])
        package = result.result_package
        self.assertEqual(package["results"][0]["qualification"]["status"], "qualified")
        self.assertEqual(package["excluded_results"][0]["qualification"]["status"], "excluded")

    def test_v2_defaults_to_its_own_cdp_endpoint(self):
        with patch.dict("os.environ", {}, clear=True):
            executor = TaskExecutor()

        self.assertEqual(CDP_ENDPOINT, "http://127.0.0.1:9223")
        self.assertEqual(executor.browser_controller.cdp_endpoint, CDP_ENDPOINT)

    def test_v2_rejects_the_v1_cdp_endpoint(self):
        with patch.dict("os.environ", {"SCOUT_CDP_ENDPOINT": "http://127.0.0.1:9222"}):
            with self.assertRaisesRegex(ValueError, "127.0.0.1:9223"):
                TaskExecutor()

    def test_executes_open_company_and_generate_site(self):
        lead = V2Lead(company_name="Restaurante Exemplo").to_dict()
        opened = self.executor.execute(task_request(
            "OPEN_COMPANY", {"lead": lead},
            authorization={"allow_credit_consumption": True},
        ))
        generated = self.executor.execute(task_request(
            "GENERATE_SITE", {"lead": lead},
            authorization={
                "allow_credit_consumption": True,
                "allow_external_effects": True,
            },
        ))
        self.assertEqual(opened.status, TaskStatus.COMPLETED)
        self.assertEqual(opened.leads[0].phone, "5511999999999")
        self.assertEqual(generated.artifacts[0]["type"], "website")
        self.assertEqual(generated.status, TaskStatus.COMPLETED)
        self.assertEqual(generated.leads[0].website, "https://cliente.aivio.example")
        self.assertTrue(self.integration.open_authorization)
        self.assertTrue(self.integration.generate_authorization["allow_credit_consumption"])
        self.assertTrue(self.integration.generate_authorization["allow_external_effects"])

    def test_generation_failures_are_explicit(self):
        self.integration.generate_error = TimeoutError(
            "generation timed out for https://aivio.example/?access_token=secret-value"
        )
        lead = V2Lead(company_name="Restaurante Exemplo").to_dict()
        result = self.executor.execute(task_request(
            "GENERATE_SITE", {"lead": lead},
            authorization={
                "allow_credit_consumption": True,
                "allow_external_effects": True,
            },
        ))
        self.assertEqual(result.status, TaskStatus.TIMEOUT)
        self.assertIn("generation timed out", result.errors[0])
        self.assertEqual(result.error["code"], "task_timeout")
        self.assertNotIn("secret-value", result.error["message"])

    def test_health_check_reports_actual_adapter_state(self):
        result = self.executor.execute(task_request("HEALTH_CHECK"))
        self.assertEqual(result.data["browser_connected"], True)
        self.assertEqual(result.data["aivio_available"], True)

    def test_browser_tasks_are_serialized_across_executor_instances(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        first_request = task_request("HEALTH_CHECK")
        second_request = task_request("HEALTH_CHECK")

        class BlockingIntegration(FakeIntegration):
            def browser_health(self):
                first_entered.set()
                release_first.wait(timeout=2)
                return self.health

        class SecondIntegration(FakeIntegration):
            def browser_health(self):
                second_entered.set()
                return self.health

        first = TaskExecutor(integration=BlockingIntegration())
        second = TaskExecutor(integration=SecondIntegration())
        threads = [
            threading.Thread(target=first.execute, args=(first_request,)),
            threading.Thread(target=second.execute, args=(second_request,)),
        ]
        threads[0].start()
        self.assertTrue(first_entered.wait(timeout=1))
        self.assertEqual(first.get_task_result(first_request["task_id"])["status"], "running")
        threads[1].start()
        queued_result = second.get_task_result(second_request["task_id"])
        self.assertEqual(queued_result["status"], "queued")
        self.assertEqual(queued_result["started_at"], "")
        self.assertEqual(queued_result["finished_at"], "")
        self.assertFalse(second_entered.wait(timeout=0.05))
        release_first.set()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(second.get_task_result(second_request["task_id"])["status"], "completed")

    def test_browser_execution_lock_serializes_separate_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            started_path = os.path.join(directory, "started")
            acquired_path = os.path.join(directory, "acquired")
            child_code = (
                "import sys\n"
                "from pathlib import Path\n"
                "from scout.task_executor import _browser_execution_lock\n"
                "Path(sys.argv[1]).touch()\n"
                "with _browser_execution_lock('http://127.0.0.1:9223'):\n"
                "    Path(sys.argv[2]).touch()\n"
            )
            process = None
            with _browser_execution_lock("http://127.0.0.1:9223"):
                process = subprocess.Popen(
                    [sys.executable, "-c", child_code, started_path, acquired_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + 3
                while not os.path.exists(started_path) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(os.path.exists(started_path))
                time.sleep(0.1)
                self.assertFalse(os.path.exists(acquired_path))
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr.decode("utf-8"))
            self.assertTrue(os.path.exists(acquired_path), stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
