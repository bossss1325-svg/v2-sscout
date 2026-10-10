import unittest
import tempfile
import json
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from scout.v3_models import V3Task, V3TaskType
from scout.v3_browser import HumanInterventionRequired, V3_CDP_ENDPOINT, WhatsAppWebAdapter
from scout.v3_executor import V3TaskExecutor
from scout.v3_storage import V3StateStore
from scout.api import create_server
from scout.discovery import MockSearchProvider
from scout.service import ScoutService


def task_request(task_type, payload, authorization=None):
    return {
        "task_id": str(uuid.uuid4()),
        "type": task_type,
        "payload": payload,
        "authorization": {"authorized_by": "auren", **(authorization or {})},
    }


class V3TaskContractTests(unittest.TestCase):
    def test_send_requires_allow_external_effects_and_human_approval(self):
        payload = {"message_id": "draft-1", "approval_id": "approval-1"}
        for authorization in (
            {},
            {"allow_external_effects": True},
            {"human_approved": True},
        ):
            with self.subTest(authorization=authorization):
                with self.assertRaises(ValueError):
                    V3Task.from_dict(task_request("SEND_MESSAGE", payload, authorization))

        task = V3Task.from_dict(task_request("SEND_MESSAGE", payload, {
            "allow_external_effects": True,
            "human_approved": True,
        }))

        self.assertEqual(task.type, V3TaskType.SEND_MESSAGE)

    def test_message_draft_requires_recorded_consent(self):
        payload = {
            "recipient_id": "conversation-123",
            "body": "Olá, conforme solicitado.",
            "consent_source": "Formulário de contato, 2026-10-10",
        }
        with self.assertRaisesRegex(ValueError, "consentimento verificável"):
            V3Task.from_dict(task_request("PREPARE_MESSAGE", payload))

        task = V3Task.from_dict(task_request(
            "PREPARE_MESSAGE", payload, {"consent_verified": True},
        ))
        self.assertEqual(task.type, V3TaskType.PREPARE_MESSAGE)

    def test_conversation_reads_are_bounded_and_named(self):
        valid = V3Task.from_dict(task_request(
            "READ_CONVERSATION", {"recipient_id": "conversation-123", "limit": 50},
        ))
        self.assertEqual(valid.payload["limit"], 50)

        for payload in ({}, {"recipient_id": "conversation-123", "limit": 51}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                V3Task.from_dict(task_request("READ_CONVERSATION", payload))

    def test_opt_out_requires_an_explicit_recipient(self):
        task = V3Task.from_dict(task_request(
            "RECORD_OPT_OUT", {"recipient_id": "conversation-123"},
        ))
        self.assertEqual(task.type, V3TaskType.RECORD_OPT_OUT)

    def test_task_payload_rejects_unrecognized_fields(self):
        with self.assertRaisesRegex(ValueError, "campos desconhecidos"):
            V3Task.from_dict(task_request("READ_CONVERSATION", {
                "recipient_id": "conversation-123",
                "send_all": True,
            }))


class FakeWhatsAppLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind

    def count(self):
        return 1

    def nth(self, index):
        self.assert_index(index)
        return self

    def assert_index(self, index):
        if index != 0:
            raise AssertionError(index)

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def inner_text(self):
        return self.page.composer_text

    def fill(self, body, timeout):
        self.page.composer_text = body

    def click(self, timeout):
        self.page.state["messages"].append({
            "text": self.page.composer_text,
            "outgoing": True,
            "timestamp": "sent",
        })
        self.page.composer_text = ""


class FakeWhatsAppPage:
    def __init__(self, title="Contato de teste"):
        self.url = "https://web.whatsapp.com/"
        self._title = "WhatsApp"
        self.state = {
            "title": title,
            "messages": [
                {"text": "Mensagem recebida", "outgoing": False, "timestamp": "10:00"},
            ],
        }
        self.composer_text = ""
        self.locator_calls = []

    def is_closed(self):
        return False

    def title(self):
        return self._title

    def evaluate(self, expression):
        if expression == "document.visibilityState === 'visible' && document.hasFocus()":
            return True
        return self.state

    def locator(self, selector):
        self.locator_calls.append(selector)
        return FakeWhatsAppLocator(self, "composer")

    def get_by_role(self, role, name):
        self.send_button_name = name
        return FakeWhatsAppLocator(self, "send")

    def wait_for_function(self, expression, arg, timeout):
        outgoing = [item for item in self.state["messages"] if item.get("outgoing")]
        if len(outgoing) <= arg["beforeCount"] or not any(
            item["text"] == arg["body"] for item in outgoing
        ):
            raise AssertionError("sent message not observable")


class FakeWhatsAppBrowser:
    def __init__(self, pages):
        self.contexts = [type("Context", (), {"pages": pages})()]

    def is_connected(self):
        return True


class V3BrowserAdapterTests(unittest.TestCase):
    def test_adapter_refuses_v2_or_other_cdp_endpoints(self):
        self.assertEqual(V3_CDP_ENDPOINT, "http://127.0.0.1:9224")
        with self.assertRaisesRegex(ValueError, "9224"):
            WhatsAppWebAdapter("http://127.0.0.1:9223")

    def test_reads_only_an_exactly_named_active_conversation(self):
        page = FakeWhatsAppPage()
        adapter = WhatsAppWebAdapter()
        adapter._browser = FakeWhatsAppBrowser([page])

        result = adapter.read_conversation("Contato de teste", limit=1)

        self.assertEqual(result["conversation_title"], "Contato de teste")
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["text"], "Mensagem recebida")

    def test_refuses_a_different_active_conversation_before_composing(self):
        page = FakeWhatsAppPage(title="Outra conversa")
        adapter = WhatsAppWebAdapter()
        adapter._browser = FakeWhatsAppBrowser([page])

        with self.assertRaisesRegex(HumanInterventionRequired, "não corresponde"):
            adapter.send_message("Contato de teste", "Olá")

        self.assertEqual(page.locator_calls, [])

    def test_send_adapter_requires_observable_outgoing_message(self):
        page = FakeWhatsAppPage()
        adapter = WhatsAppWebAdapter()
        adapter._browser = FakeWhatsAppBrowser([page])

        result = adapter.send_message("Contato de teste", "Olá, conforme solicitado.")

        self.assertEqual(result["status"], "sent_confirmed")
        self.assertEqual(page.state["messages"][-1]["text"], "Olá, conforme solicitado.")


class V3StateStoreTests(unittest.TestCase):
    def test_messages_tasks_and_opt_out_survive_reinitialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v3-state.json"
            first = V3StateStore(path)
            self.assertTrue(first.create_task("task-1", {"status": "completed"}))
            self.assertTrue(first.create_message("message-1", {
                "recipient_id": "conversation-123",
                "body": "Olá",
                "status": "sent",
                "sent_at": "2026-10-10T10:00:00+00:00",
            }))
            first.record_opt_out("conversation-456", "Não entrar em contato")

            reopened = V3StateStore(path)

            self.assertEqual(reopened.get_task("task-1")["status"], "completed")
            self.assertEqual(len(reopened.message_history("conversation-123")), 1)
            self.assertTrue(reopened.is_opted_out("conversation-456"))
            self.assertEqual(reopened.latest_send_time(), "2026-10-10T10:00:00+00:00")

    def test_duplicate_draft_is_detected_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v3-state.json"
            first = V3StateStore(path)
            second = V3StateStore(path)
            first.create_message("message-1", {
                "recipient_id": "conversation-123",
                "body": "Olá",
                "status": "draft",
            })

            self.assertTrue(second.has_duplicate("conversation-123", "Olá"))


class FakeWhatsAppAdapter:
    def __init__(self, *, send_status="sent_confirmed"):
        self.send_status = send_status
        self.connect_calls = 0
        self.read_calls = []
        self.send_calls = []
        self.conversation = {"messages": [], "total_visible": 0}

    def connect(self):
        self.connect_calls += 1

    def read_conversation(self, recipient_id, limit):
        self.read_calls.append((recipient_id, limit))
        return {"recipient_id": recipient_id, **self.conversation}

    def send_message(self, recipient_id, body):
        self.send_calls.append((recipient_id, body))
        return {"status": self.send_status, "recipient_id": recipient_id}


class V3ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.adapter = FakeWhatsAppAdapter()
        self.store = V3StateStore(Path(self.directory.name) / "v3-state.json")
        self.executor = V3TaskExecutor(
            adapter=self.adapter,
            state_store=self.store,
            minimum_send_interval_seconds=1,
            max_sends_per_hour=10,
        )

    def tearDown(self):
        self.directory.cleanup()

    def run_task(self, task_type, payload, authorization=None):
        return self.executor.execute(task_request(task_type, payload, authorization))

    def test_prepare_persists_a_consent_scoped_draft_without_sending(self):
        result = self.run_task("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá, conforme solicitado.",
            "consent_source": "Solicitação registrada no formulário",
        }, {"consent_verified": True})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["status"], "draft")
        self.assertEqual(self.adapter.send_calls, [])
        self.assertTrue(self.store.has_duplicate(
            "conversation-123", "Olá, conforme solicitado.",
        ))

    def test_send_requires_matching_approval_and_persists_confirmation(self):
        draft_result = self.run_task("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá, conforme solicitado.",
            "consent_source": "Solicitação registrada no formulário",
        }, {"consent_verified": True})
        draft = draft_result["result"]

        sent = self.run_task("SEND_MESSAGE", {
            "message_id": draft["message_id"],
            "approval_id": draft["approval_id"],
        }, {"allow_external_effects": True, "human_approved": True})

        self.assertEqual(sent["status"], "completed")
        self.assertEqual(sent["result"]["status"], "sent")
        self.assertEqual(self.adapter.send_calls, [(
            "conversation-123", "Olá, conforme solicitado.",
        )])
        self.assertEqual(self.executor.message_history("conversation-123")[0]["status"], "sent")

    def test_opt_out_blocks_message_preparation(self):
        self.run_task("RECORD_OPT_OUT", {
            "recipient_id": "conversation-123",
            "reason": "Solicitou não receber contato",
        })

        result = self.run_task("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá",
            "consent_source": "Solicitação registrada",
        }, {"consent_verified": True})

        self.assertEqual(result["status"], "failed")
        self.assertIn("opt-out", result["error"]["message"])
        self.assertEqual(self.adapter.send_calls, [])

    def test_unconfirmed_send_is_marked_uncertain_and_never_retried(self):
        self.adapter.send_status = "unknown"
        draft = self.run_task("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá",
            "consent_source": "Solicitação registrada",
        }, {"consent_verified": True})["result"]
        send_payload = {
            "message_id": draft["message_id"],
            "approval_id": draft["approval_id"],
        }
        authorization = {"allow_external_effects": True, "human_approved": True}

        first = self.run_task("SEND_MESSAGE", send_payload, authorization)
        second = self.run_task("SEND_MESSAGE", send_payload, authorization)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(self.store.get_message(draft["message_id"])["status"], "send_uncertain")
        self.assertEqual(len(self.adapter.send_calls), 1)

    def test_send_rate_limit_applies_across_recipients(self):
        self.executor.max_sends_per_hour = 1
        for recipient in ("conversation-123", "conversation-456"):
            self.run_task("PREPARE_MESSAGE", {
                "recipient_id": recipient,
                "body": f"Olá {recipient}",
                "consent_source": "Solicitação registrada",
            }, {"consent_verified": True})
        drafts = [
            record for recipient in ("conversation-123", "conversation-456")
            for record in self.executor.message_history(recipient)
        ]
        for draft in drafts:
            send_task = task_request("SEND_MESSAGE", {
                "message_id": draft["message_id"],
                "approval_id": draft["approval_id"],
            }, {"allow_external_effects": True, "human_approved": True})
            result = self.executor.execute(send_task)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Limite horário", result["error"]["message"])
        self.assertEqual(len(self.adapter.send_calls), 1)

    def test_incoming_opt_out_phrase_is_persisted_and_blocks_future_drafts(self):
        self.adapter.conversation = {
            "messages": [{"text": "Por favor, não me envie mais mensagens", "outgoing": False}],
            "total_visible": 1,
        }
        read = self.run_task("READ_CONVERSATION", {
            "recipient_id": "conversation-123",
        })
        draft = self.run_task("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá",
            "consent_source": "Solicitação registrada",
        }, {"consent_verified": True})

        self.assertTrue(read["result"]["opt_out_detected"])
        self.assertTrue(self.store.is_opted_out("conversation-123"))
        self.assertEqual(draft["status"], "failed")
        self.assertEqual(self.adapter.send_calls, [])

    def test_interrupted_task_is_marked_failed_on_query_after_restart(self):
        task_id = str(uuid.uuid4())
        self.store.create_task(task_id, {
            "task_id": task_id,
            "type": "READ_CONVERSATION",
            "status": "running",
            "worker_pid": -1,
            "finished_at": None,
        })

        result = self.executor.get_task(task_id)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "task_interrupted")


class V3ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.adapter = FakeWhatsAppAdapter()
        executor = V3TaskExecutor(
            adapter=self.adapter,
            state_store=V3StateStore(Path(self.directory.name) / "v3-state.json"),
            minimum_send_interval_seconds=1,
        )
        self.server = create_server(
            ScoutService(MockSearchProvider()),
            port=0,
            v3_task_executor=executor,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def post_v3_task(self, value):
        request = urllib.request.Request(
            f"{self.base_url}/v3/tasks",
            data=json.dumps(value).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, json.loads(response.read())

    def test_v3_api_exposes_persisted_draft_and_history(self):
        _, response = self.post_v3_task(task_request("PREPARE_MESSAGE", {
            "recipient_id": "conversation-123",
            "body": "Olá",
            "consent_source": "Solicitação registrada",
        }, {"consent_verified": True}))
        draft = response["result"]

        with urllib.request.urlopen(
            f"{self.base_url}/v3/tasks/{response['task_id']}"
        ) as result_response:
            persisted_task = json.loads(result_response.read())
        with urllib.request.urlopen(
            f"{self.base_url}/v3/messages/{draft['message_id']}"
        ) as message_response:
            persisted_message = json.loads(message_response.read())
        with urllib.request.urlopen(
            f"{self.base_url}/v3/history/conversation-123"
        ) as history_response:
            history = json.loads(history_response.read())

        self.assertEqual(persisted_task["status"], "completed")
        self.assertEqual(persisted_message["status"], "draft")
        self.assertEqual(history["messages"][0]["message_id"], draft["message_id"])
        self.assertEqual(self.adapter.send_calls, [])

    def test_v3_api_rejects_send_without_human_approval(self):
        status, response = self.post_v3_task(task_request("SEND_MESSAGE", {
            "message_id": "draft-1",
            "approval_id": "approval-1",
        }, {"allow_external_effects": True}))

        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "invalid_v3_task")
        self.assertEqual(self.adapter.send_calls, [])

    def test_v3_health_reports_browser_status_separately(self):
        with urllib.request.urlopen(f"{self.base_url}/v3/health") as response:
            health = json.loads(response.read())

        self.assertFalse(health["browser_connected"])
        self.assertFalse(health["whatsapp_available"])
        self.assertEqual(health["cdp_endpoint"], "http://127.0.0.1:9224")