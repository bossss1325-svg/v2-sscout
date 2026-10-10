import json
import os
import sys
import unittest
from unittest.mock import patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scout.aivio import CDP_ENDPOINT
from scout.cli import main
from scout.v2_models import TaskResult, TaskStatus, TaskType


class FakeServer:
    server_address = ("127.0.0.1", 8080)

    def __init__(self):
        self.served = False
        self.closed = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True


class CliTests(unittest.TestCase):
    def test_no_arguments_starts_the_existing_api_on_loopback(self):
        server = FakeServer()

        with (
            patch.object(sys, "argv", ["auren-scout"]),
            patch.dict(os.environ, {"SCOUT_HOST": "127.0.0.1", "SCOUT_PORT": "8080"}, clear=False),
            patch("scout.cli.create_server", return_value=server) as create_server,
            patch("scout.cli.TaskExecutor") as task_executor,
            patch("scout.cli.BrowserController") as browser_controller,
        ):
            main()

        self.assertEqual(create_server.call_args.args[1:], ("127.0.0.1", 8080))
        self.assertTrue(server.served)
        self.assertTrue(server.closed)
        task_executor.assert_not_called()
        browser_controller.assert_not_called()

    def test_task_without_an_explicit_type_is_not_executed(self):
        with (
            patch.object(sys, "argv", ["auren-scout", "task"]),
            patch("scout.cli.TaskExecutor") as executor,
            self.assertRaises(SystemExit),
        ):
            main()

        executor.assert_not_called()

    def test_non_loopback_host_is_rejected(self):
        with (
            patch.object(sys, "argv", ["auren-scout"]),
            patch.dict(os.environ, {"SCOUT_HOST": "0.0.0.0"}, clear=False),
            patch("scout.cli.create_server") as create_server,
            self.assertRaises(SystemExit),
        ):
            main()

        create_server.assert_not_called()

    def test_v2_browser_status_defaults_to_its_own_endpoint(self):
        with (
            patch.object(sys, "argv", ["AurenScout-V2.exe", "browser-status"]),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print"),
        ):
            controller.return_value.getStatus.return_value = {"connected": True}
            main()

        controller.assert_called_once_with(cdp_endpoint=CDP_ENDPOINT)
        controller.return_value.connect.assert_called_once_with()
        controller.return_value.disconnect.assert_called_once_with()

    def test_v2_browser_status_accepts_its_explicit_endpoint(self):
        with (
            patch.object(sys, "argv", ["AurenScout-V2.exe", "browser-status"]),
            patch.dict(os.environ, {"SCOUT_CDP_ENDPOINT": CDP_ENDPOINT}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print"),
        ):
            controller.return_value.getStatus.return_value = {"connected": True}
            main()

        controller.assert_called_once_with(cdp_endpoint=CDP_ENDPOINT)

    def test_v2_browser_status_rejects_the_v1_endpoint(self):
        with (
            patch.object(sys, "argv", ["AurenScout-V2.exe", "browser-status"]),
            patch.dict(os.environ, {"SCOUT_CDP_ENDPOINT": "http://127.0.0.1:9222"}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            self.assertRaises(SystemExit),
        ):
            main()

        controller.assert_not_called()

    def test_browser_inspect_prints_valid_json_without_interaction(self):
        inspection = {
            "connected": True,
            "title": "AIVIO Dashboard",
            "url": "https://aivio.example/dashboard",
            "text": "Página atual",
            "inputs": [],
            "buttons": [],
            "links": [],
        }
        with (
            patch.object(sys, "argv", ["AurenScout-V2.exe", "browser-inspect"]),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print") as print_output,
        ):
            controller.return_value.inspectPage.return_value = inspection
            main()

        output = json.loads(print_output.call_args.args[0])
        self.assertEqual(output, inspection)
        controller.assert_called_once_with(cdp_endpoint=CDP_ENDPOINT)
        controller.return_value.connect.assert_called_once_with()
        controller.return_value.inspectPage.assert_called_once_with()
        controller.return_value.disconnect.assert_called_once_with()
        controller.return_value.navigate.assert_not_called()

    def test_browser_inspect_category_prints_json_and_calls_diagnostic_only(self):
        inspection = {
            "connected": True,
            "title": "AIVIO Dashboard",
            "url": "https://aivio.example/dashboard",
            "inputs_before_open": [],
            "category_button": {"text": "Escolha o ramo"},
            "dropdown_opened": True,
            "new_elements": [{"tag": "div", "text": "Restaurantes", "role": "option"}],
            "element_limit_reached": False,
        }
        with (
            patch.object(sys, "argv", ["AurenScout-V2.exe", "browser-inspect-category"]),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print") as print_output,
        ):
            controller.return_value.inspectCategoryDropdown.return_value = inspection
            main()

        self.assertEqual(json.loads(print_output.call_args.args[0]), inspection)
        controller.assert_called_once_with(cdp_endpoint=CDP_ENDPOINT)
        controller.return_value.inspectCategoryDropdown.assert_called_once_with()
        controller.return_value.navigate.assert_not_called()

    def test_browser_test_category_uses_aivio_category_selection_without_search(self):
        category = "Restaurantes, padarias e lanchonetes"
        with (
            patch.object(
                sys, "argv",
                ["AurenScout-V2.exe", "browser-test-category", "--category", category],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("scout.cli.AivioIntegration") as integration,
            patch("builtins.print") as print_output,
        ):
            integration.return_value.select_category.return_value = {
                "aivio": True,
                "category": category,
                "combobox_found": True,
                "click_performed": True,
                "dropdown_open": True,
                "option_found": True,
                "option_clicked": True,
                "category_confirmed": True,
            }
            main()

        output = json.loads(print_output.call_args.args[0])
        self.assertTrue(output["connected"])
        self.assertTrue(output["aivio"])
        self.assertEqual(output["category"], category)
        self.assertTrue(output["combobox_found"])
        self.assertTrue(output["click_performed"])
        self.assertTrue(output["dropdown_open"])
        self.assertTrue(output["option_found"])
        self.assertTrue(output["option_clicked"])
        self.assertTrue(output["category_confirmed"])
        controller.assert_called_once_with(cdp_endpoint=CDP_ENDPOINT)
        controller.return_value.connect.assert_called_once_with()
        controller.return_value.disconnect.assert_called_once_with()
        integration.assert_called_once_with(controller.return_value)
        integration.return_value.select_category.assert_called_once_with(category)
        controller.return_value.inspectCategoryDropdown.assert_not_called()
        controller.return_value.click_button.assert_not_called()

    def test_browser_test_category_rejects_empty_category_argument(self):
        with (
            patch.object(
                sys, "argv",
                ["AurenScout-V2.exe", "browser-test-category", "--category", "   "],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("scout.cli.AivioIntegration") as integration,
            patch("builtins.print") as print_output,
            self.assertRaises(SystemExit) as context,
        ):
            integration.return_value.select_category.side_effect = ValueError(
                "Informe uma categoria para selecionar"
            )
            main()

        self.assertEqual(context.exception.code, 1)
        self.assertFalse(json.loads(print_output.call_args.args[0])["category_confirmed"])
        integration.return_value.select_category.assert_called_once_with("   ")
        controller.return_value.click_button.assert_not_called()

    def test_browser_test_city_uses_the_requested_city_exactly(self):
        for city in ("Porto Alegre", "Canoas", "Caxias do Sul"):
            with self.subTest(city=city):
                with (
                    patch.object(
                        sys, "argv",
                        ["AurenScout-V2.exe", "browser-test-city", "--city", city],
                    ),
                    patch.dict(os.environ, {}, clear=True),
                    patch("scout.cli.BrowserController") as controller,
                    patch("scout.cli.TaskExecutor") as executor,
                    patch("builtins.print") as print_output,
                ):
                    controller.return_value.read_visible_text.return_value = (
                        f"Digite uma cidade...\n{city}\n{city} Centro"
                    )
                    main()

                output = json.loads(print_output.call_args.args[0])
                self.assertEqual(output["city"], city)
                self.assertEqual(output["typed"], city)
                self.assertEqual(output["suggestions"], [city, f"{city} Centro"])
                self.assertTrue(output["city_found"])
                self.assertEqual(output["exact_match_count"], 1)
                instance = controller.return_value
                instance.fill_input.assert_called_once_with(
                    city, placeholder="Digite uma cidade..."
                )
                instance.wait_for_text.assert_called_once_with(city, timeout_ms=5_000)
                instance.click_text.assert_not_called()
                instance.click_button.assert_not_called()
                instance.open_dropdown.assert_not_called()
                instance.select_option.assert_not_called()
                instance.navigate.assert_not_called()
                executor.assert_not_called()

    def test_browser_test_city_prints_connection_failure_as_json(self):
        with (
            patch.object(
                sys, "argv",
                ["AurenScout-V2.exe", "browser-test-city", "--city", "Canoas"],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print") as print_output,
            self.assertRaises(SystemExit) as context,
        ):
            controller.return_value.connect.side_effect = RuntimeError("CDP unavailable")
            main()

        self.assertEqual(context.exception.code, 1)
        self.assertEqual(
            json.loads(print_output.call_args.args[0]),
            {
                "connected": False,
                "city_input_found": False,
                "city": "Canoas",
                "typed": "Canoas",
                "suggestions": [],
                "city_found": False,
                "exact_match_count": 0,
            },
        )
        controller.return_value.disconnect.assert_called_once_with()
        controller.return_value.element_visible.assert_not_called()

    def test_browser_test_city_reports_missing_city_input(self):
        with (
            patch.object(
                sys, "argv",
                ["AurenScout-V2.exe", "browser-test-city", "--city", "Caxias do Sul"],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print") as print_output,
            self.assertRaises(SystemExit) as context,
        ):
            controller.return_value.element_visible.return_value = False
            main()

        self.assertEqual(context.exception.code, 1)
        output = json.loads(print_output.call_args.args[0])
        self.assertTrue(output["connected"])
        self.assertFalse(output["city_input_found"])
        self.assertEqual(output["suggestions"], [])
        controller.return_value.click_element.assert_not_called()
        controller.return_value.fill_input.assert_not_called()

    def test_browser_test_city_returns_false_when_exact_suggestion_is_missing(self):
        with (
            patch.object(
                sys, "argv",
                ["AurenScout-V2.exe", "browser-test-city", "--city", "Porto Alegre"],
            ),
            patch.dict(os.environ, {}, clear=True),
            patch("scout.cli.BrowserController") as controller,
            patch("builtins.print") as print_output,
        ):
            controller.return_value.wait_for_text.side_effect = PlaywrightTimeoutError(
                "suggestion timeout"
            )
            controller.return_value.read_visible_text.return_value = (
                "Porto Alegre - Centro\nPorto Alegre-RS"
            )
            main()

        output = json.loads(print_output.call_args.args[0])
        self.assertEqual(output["suggestions"], ["Porto Alegre - Centro", "Porto Alegre-RS"])
        self.assertFalse(output["city_found"])
        self.assertEqual(output["exact_match_count"], 0)
        controller.return_value.read_visible_text.assert_called_once_with()

    def test_task_command_executes_and_prints_standardized_result(self):
        result = TaskResult(
            task_id="12345678-1234-5678-1234-567812345678",
            type=TaskType.HEALTH_CHECK,
            status=TaskStatus.COMPLETED,
        )
        with (
            patch.object(sys, "argv", ["auren-scout", "task", "HEALTH_CHECK", "--payload", "{}"]),
            patch("scout.cli.TaskExecutor") as executor_class,
        ):
            executor_class.return_value.execute.return_value = result
            with patch("builtins.print") as print_result:
                main()
        printed = json.loads(print_result.call_args.args[0])
        self.assertEqual(printed["type"], "HEALTH_CHECK")
        self.assertEqual(printed["status"], "completed")

    def test_task_command_returns_nonzero_for_timeout(self):
        result = TaskResult(
            task_id="12345678-1234-5678-1234-567812345678",
            type=TaskType.HEALTH_CHECK,
            status=TaskStatus.TIMEOUT,
        )
        with (
            patch.object(
                sys, "argv",
                ["auren-scout", "task", "HEALTH_CHECK", "--payload", "{}"],
            ),
            patch("scout.cli.TaskExecutor") as executor_class,
            patch("builtins.print"),
        ):
            executor_class.return_value.execute.return_value = result
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code, 1)

    def test_task_command_forwards_explicit_authorization(self):
        result = TaskResult(
            task_id="12345678-1234-5678-1234-567812345678",
            type=TaskType.SEARCH_LEADS,
            status=TaskStatus.COMPLETED,
        )
        with (
            patch.object(
                sys,
                "argv",
                [
                    "auren-scout",
                    "task",
                    "SEARCH_LEADS",
                    "--authorization",
                    '{"allow_credit_consumption":true,"authorized_by":"auren"}',
                    "--payload",
                    '{"city":"Porto Alegre","state":"RS","category":"restaurantes"}',
                ],
            ),
            patch("scout.cli.TaskExecutor") as executor_class,
            patch("builtins.print"),
        ):
            executor_class.return_value.execute.return_value = result
            main()
        request = executor_class.return_value.execute.call_args.args[0]
        self.assertTrue(request["authorization"]["allow_credit_consumption"])


if __name__ == "__main__":
    unittest.main()
