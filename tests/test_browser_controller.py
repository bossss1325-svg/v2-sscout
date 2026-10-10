import json
import unittest
from unittest.mock import patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scout.browser_controller import (
    INSPECT_CATEGORY_DOM_SCRIPT,
    INSPECT_PAGE_SCRIPT,
    SEARCH_RESULT_STATE_SCRIPT,
    BrowserController,
    V1_CDP_ENDPOINT,
    V2_CDP_ENDPOINT,
)


class FakePage:
    def __init__(self, title, url, active=False):
        self._title = title
        self.url = url
        self.active = active
        self.navigated_to = None
        self.clicked = False
        self.filled = False
        self.category_clicked = False
        self.category_button_count = 1
        self.category_inspections = []
        self.search_result_states = []
        self.last_search_result_state = None
        self.stable_content_states = []
        self.stable_snapshots = []
        self.last_stable_content_state = None
        self.last_stable_snapshot = None

    def is_closed(self):
        return False

    def title(self):
        return self._title

    def evaluate(self, expression):
        if "url: window.location.href" in expression and "text_hash: (() => {" in expression:
            if self.stable_snapshots:
                self.last_stable_snapshot = self.stable_snapshots.pop(0)
            return self.last_stable_snapshot or {"url": self.url, "text_hash": "unchanged"}
        if "text_length: text.length" in expression:
            if self.stable_content_states:
                self.last_stable_content_state = self.stable_content_states.pop(0)
            return self.last_stable_content_state or {"text_length": 0, "loading": False}
        if expression == SEARCH_RESULT_STATE_SCRIPT:
            if self.search_result_states:
                self.last_search_result_state = self.search_result_states.pop(0)
            return self.last_search_result_state or {
                "records": [], "no_results": False, "loading": False,
            }
        if expression == INSPECT_CATEGORY_DOM_SCRIPT:
            return self.category_inspections.pop(0)
        if expression == INSPECT_PAGE_SCRIPT:
            return {
                "title": self._title,
                "url": f"{self.url}?access_token=do-not-return#private",
                "text": "Página atual do AIVIO",
                "inputs": [
                    {"tag": "input", "type": "text", "name": "city", "placeholder": "Cidade",
                     "aria_label": "Cidade", "value": "Porto Alegre"},
                    {"tag": "input", "type": "password", "name": "password", "placeholder": "",
                     "aria_label": "", "value": "do-not-return"},
                    {"tag": "input", "type": "text", "name": "api_token", "placeholder": "",
                     "aria_label": "", "value": "do-not-return"},
                ],
                "buttons": [{"text": "Buscar", "aria_label": "", "type": "submit"}],
                "links": [{"text": "Ajuda", "href": "https://aivio.example/help"}],
            }
        self.asserted_expression = expression
        return self.active

    def get_by_role(self, role, name):
        if role != "button" or name.pattern != r"^\s*Escolha o ramo\s*$":
            raise AssertionError("unexpected role locator")
        return FakeCategoryButton(self)

    def wait_for_timeout(self, timeout):
        self.waited_for_timeout = timeout

    def goto(self, url, wait_until, timeout=30_000):
        self.navigated_to = (url, wait_until)
        self.url = url
        self._title = "Destino"

    def screenshot(self):
        return b"image"


class FakeCategoryButton:
    def __init__(self, page):
        self.page = page

    def count(self):
        return self.page.category_button_count

    def click(self):
        self.page.category_clicked = True


class FakeBrowserSession:
    def __init__(self, product):
        self.product = product
        self.detached = False

    def send(self, method):
        self.method = method
        return {"product": self.product}

    def detach(self):
        self.detached = True


class FakeBrowser:
    def __init__(self, product, pages):
        self.session = FakeBrowserSession(product)
        self.contexts = [type("Context", (), {"pages": pages})()]
        self.connected = True

    def is_connected(self):
        return self.connected

    def new_browser_cdp_session(self):
        return self.session


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = type(
            "Chromium",
            (),
            {"connect_over_cdp": lambda _, endpoint, **kwargs: browser},
        )()
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


class BrowserControllerTests(unittest.TestCase):
    def setUp(self):
        self.inactive_page = FakePage("AIVIO", "https://aivio.example/", active=False)
        self.active_page = FakePage("AIVIO - Painel", "https://aivio.example/home", active=True)
        self.browser = FakeBrowser("Edg/130.0.0.0", [self.inactive_page, self.active_page])
        self.playwright = FakePlaywright(self.browser)
        self.manager = FakePlaywrightManager(self.playwright)
        self.controller = BrowserController("http://127.0.0.1:9222")

    def connect(self):
        with patch("scout.browser_controller.sync_playwright", return_value=self.manager):
            self.controller.connect()

    def test_connect_status_lists_tabs_and_identifies_active_tab_as_json(self):
        self.connect()

        status = self.controller.getStatus()
        decoded = json.loads(json.dumps(status))

        self.assertTrue(decoded["connected"])
        self.assertEqual(decoded["browser"], "Microsoft Edge")
        self.assertEqual(decoded["browser_version"], "Edg/130.0.0.0")
        self.assertEqual(decoded["cdp_endpoint"], "http://127.0.0.1:9222")
        self.assertEqual(decoded["tabs"], [
            {"title": "AIVIO", "url": "https://aivio.example/"},
            {"title": "AIVIO - Painel", "url": "https://aivio.example/home"},
        ])
        self.assertEqual(decoded["activeTab"], {
            "title": "AIVIO - Painel",
            "url": "https://aivio.example/home",
        })
        self.assertEqual(self.browser.session.method, "Browser.getVersion")
        self.assertTrue(self.browser.session.detached)
        self.controller.disconnect()
        self.assertTrue(self.playwright.stopped)
        self.assertTrue(self.browser.connected)
        self.assertIsNone(self.controller._browser_version)

    def test_tab_status_removes_query_and_fragment_secrets(self):
        self.active_page.url = "https://aivio.example/home?access_token=hidden#private"
        self.connect()

        status = self.controller.getStatus()

        self.assertEqual(status["activeTab"]["url"], "https://aivio.example/home")
        self.assertNotIn("access_token", json.dumps(status))
        self.assertNotIn("private", json.dumps(status))

    def test_reconnect_stops_stale_playwright_after_edge_disconnects(self):
        self.connect()
        old_playwright = self.playwright
        self.browser.connected = False
        replacement_browser = FakeBrowser("Edg/131.0.0.0", [self.active_page])
        replacement_playwright = FakePlaywright(replacement_browser)
        self.manager.playwright = replacement_playwright

        with patch("scout.browser_controller.sync_playwright", return_value=self.manager):
            self.controller.connect()

        self.assertTrue(old_playwright.stopped)
        self.assertIs(self.controller._browser, replacement_browser)

    def test_navigation_only_navigates_the_active_tab(self):
        self.connect()

        result = self.controller.navigate("https://example.com/path")

        self.assertEqual(result, {"title": "Destino", "url": "https://example.com/path"})
        self.assertIsNone(self.inactive_page.navigated_to)
        self.assertEqual(self.active_page.navigated_to, ("https://example.com/path", "domcontentloaded"))

    def test_navigation_rejects_non_http_urls_and_embedded_credentials(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.controller.navigate(url)

    def test_waits_for_dynamic_results_and_stability(self):
        self.active_page.search_result_states = [
            {"records": [], "no_results": False, "loading": True},
            {"records": ["Empresa real"], "no_results": False, "loading": False},
            {"records": ["Empresa real"], "no_results": False, "loading": False},
            {"records": ["Empresa real"], "no_results": False, "loading": False},
        ]
        self.controller._active_page_override = self.active_page

        result = self.controller.wait_for_stable_search_results(
            {"url": "https://aivio.example/search", "results_hash": "old"},
            timeout_ms=1_000,
            stable_ms=1,
        )

        self.assertEqual(result["record_count"], 1)
        self.assertFalse(result["no_results"])

    def test_accepts_stable_explicit_empty_results(self):
        self.active_page.search_result_states = [
            {"records": [], "no_results": True, "loading": False},
            {"records": [], "no_results": True, "loading": False},
            {"records": [], "no_results": True, "loading": False},
        ]
        self.controller._active_page_override = self.active_page

        result = self.controller.wait_for_stable_search_results(
            {"url": "https://aivio.example/search", "results_hash": "old"},
            timeout_ms=1_000,
            stable_ms=1,
        )

        self.assertEqual(result["record_count"], 0)
        self.assertTrue(result["no_results"])

    def test_url_change_without_results_times_out(self):
        self.active_page.search_result_states = [
            {"records": [], "no_results": False, "loading": False},
        ]
        self.controller._active_page_override = self.active_page

        with self.assertRaisesRegex(PlaywrightTimeoutError, "Resultados reais e estáveis"):
            self.controller.wait_for_stable_search_results(
                {"url": "https://aivio.example/before", "results_hash": "old"},
                timeout_ms=2,
                stable_ms=1,
            )

    def test_generic_stable_wait_requires_new_content_not_only_url(self):
        self.active_page.stable_snapshots = [
            {"url": "https://aivio.example/after", "text_hash": "same-content"},
        ]
        self.active_page.stable_content_states = [
            {"text_length": 12, "loading": False},
        ]
        self.controller._active_page_override = self.active_page

        with self.assertRaisesRegex(PlaywrightTimeoutError, "Conteúdo novo e estável"):
            self.controller.wait_for_stable_content(
                {"url": "https://aivio.example/before", "text_hash": "same-content"},
                timeout_ms=2,
                stable_ms=1,
            )

    def test_generic_stable_wait_returns_only_after_content_is_stable(self):
        self.active_page.stable_snapshots = [
            {"url": "https://aivio.example/results", "text_hash": "new"},
        ] * 10
        self.active_page.stable_content_states = [
            {"text_length": 20, "loading": False},
        ] * 10
        self.controller._active_page_override = self.active_page

        result = self.controller.wait_for_stable_content(
            {"url": "https://aivio.example/search", "text_hash": "old"},
            timeout_ms=100,
            stable_ms=1,
        )

        self.assertEqual(result["url"], "https://aivio.example/results")
        self.assertEqual(result["text_length"], 20)

    def test_endpoint_is_required_and_must_not_contain_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "SCOUT_CDP_ENDPOINT"):
            BrowserController("").connect()
        with self.assertRaises(ValueError):
            BrowserController("http://user:pass@127.0.0.1:9222").connect()

    def test_defaults_to_the_v1_endpoint_when_environment_is_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(BrowserController().cdp_endpoint, V1_CDP_ENDPOINT)

    def test_accepts_both_version_specific_loopback_endpoints(self):
        for endpoint in (V1_CDP_ENDPOINT, V2_CDP_ENDPOINT):
            with self.subTest(endpoint=endpoint):
                controller = BrowserController(endpoint)
                with patch("scout.browser_controller.sync_playwright", return_value=self.manager):
                    controller.connect()
                controller.disconnect()

    def test_rejects_non_loopback_https_websocket_and_wrong_ports(self):
        invalid_endpoints = (
            "http://192.0.2.1:9223",
            "http://edge.example:9223",
            "https://127.0.0.1:9223",
            "ws://127.0.0.1:9223",
            "wss://edge.example:9223",
            "http://localhost:9223",
            "http://127.0.0.1:9224",
        )
        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                BrowserController(endpoint).connect()

    def test_non_edge_cdp_endpoint_is_rejected_and_disconnected(self):
        self.browser.session.product = "Chrome/130.0.0.0"
        with patch("scout.browser_controller.sync_playwright", return_value=self.manager):
            with self.assertRaisesRegex(RuntimeError, "não pertence ao Microsoft Edge"):
                self.controller.connect()
        self.assertTrue(self.playwright.stopped)
        self.assertIsNone(self.controller._browser)

    def test_read_page_info_and_screenshot_use_active_tab(self):
        self.connect()

        self.assertEqual(self.controller.readPageInfo(), {
            "title": "AIVIO - Painel",
            "url": "https://aivio.example/home",
        })
        self.assertEqual(self.controller.screenshot(), b"image")

    def test_inspect_page_reads_dom_without_interacting(self):
        self.connect()

        result = self.controller.inspectPage()

        self.assertTrue(result["connected"])
        self.assertEqual(result["title"], "AIVIO - Painel")
        self.assertEqual(result["url"], "https://aivio.example/home")
        self.assertEqual(result["text"], "Página atual do AIVIO")
        self.assertEqual(result["inputs"], [{
            "tag": "input", "type": "text", "name": "city", "id": None, "role": None,
            "placeholder": "Cidade", "aria_label": "Cidade", "autocomplete": None,
            "labels": None, "data_attributes": None, "value": "Porto Alegre",
        }, {
            "tag": "input", "type": "text", "name": "api_token", "id": None, "role": None,
            "placeholder": "", "aria_label": "", "autocomplete": None,
            "labels": None, "data_attributes": None, "value": None,
        }])
        self.assertEqual(result["buttons"], [{"text": "Buscar", "aria_label": "", "type": "submit"}])
        self.assertEqual(result["links"], [{"text": "Ajuda", "href": "https://aivio.example/help"}])
        self.assertNotIn("access_token", result["url"])
        self.assertNotIn("private", result["url"])
        self.assertFalse(self.active_page.clicked)
        self.assertFalse(self.active_page.filled)
        self.assertIsNone(self.active_page.navigated_to)

    def test_inspection_script_excludes_password_values_and_redacts_sensitive_urls(self):
        self.assertIn("element.type.toLowerCase() === 'password'", INSPECT_PAGE_SCRIPT)
        self.assertIn("url.search = ''", INSPECT_PAGE_SCRIPT)
        self.assertIn("url.hash = ''", INSPECT_PAGE_SCRIPT)

    def test_inspect_category_dropdown_reports_input_attributes_and_new_options(self):
        input_info = {
            "tag": "input",
            "type": "text",
            "name": "region",
            "id": "region-filter",
            "placeholder": "",
            "aria_label": "",
            "role": "combobox",
            "autocomplete": "address-level1",
            "labels": ["Estado"],
            "class": "region-control",
            "data_attributes": {"data-testid": "region"},
            "aria_attributes": {"aria-label": "Estado"},
        }
        city_input_info = {
            "tag": "input",
            "type": "text",
            "name": "city",
            "id": "city-filter",
            "placeholder": "Digite uma cidade...",
            "aria_label": "",
            "role": "",
            "autocomplete": "",
            "labels": [],
            "class": "city-control",
            "data_attributes": {},
            "aria_attributes": {},
        }
        button_info = {
            "tag": "button",
            "text": "Escolha o ramo",
            "role": "",
            "aria_label": "",
            "class": "category-trigger",
            "data_attributes": {},
            "aria_attributes": {"aria-haspopup": "listbox", "aria-expanded": "false"},
        }
        page_url = "https://aivio.example/dashboard"
        before = {
            "title": "AIVIO Dashboard",
            "url": page_url,
            "inputs": [city_input_info, input_info],
            "category_button": button_info,
            "elements": [{"signature": "category-trigger", **button_info}],
        }
        after = {
            "title": "AIVIO Dashboard",
            "url": page_url,
            "inputs": [city_input_info, input_info],
            "category_button": {**button_info, "aria_attributes": {"aria-expanded": "true"}},
            "elements": [
                {"signature": "category-trigger", **button_info},
                {
                    "signature": "restaurant-option",
                    "tag": "div",
                    "text": "Restaurantes",
                    "role": "option",
                    "aria_label": "",
                    "class": "category-option",
                    "data_attributes": {"data-value": "restaurants"},
                    "aria_attributes": {"aria-selected": "false"},
                },
            ],
        }
        self.active_page.category_inspections = [before, after]
        self.connect()

        result = self.controller.inspectCategoryDropdown()

        self.assertTrue(self.active_page.category_clicked)
        self.assertEqual(self.active_page.waited_for_timeout, 300)
        self.assertEqual(result["inputs_before_open"], [city_input_info, input_info])
        self.assertNotIn("second_text_input_without_placeholder", result)
        self.assertEqual(result["category_button"], button_info)
        self.assertEqual(result["new_elements"], [{
            "tag": "div",
            "text": "Restaurantes",
            "role": "option",
            "aria_label": "",
            "class": "category-option",
            "data_attributes": {"data-value": "restaurants"},
            "aria_attributes": {"aria-selected": "false"},
        }])
        self.assertFalse(self.active_page.filled)
        self.assertIsNone(self.active_page.navigated_to)

    def test_inspect_category_dropdown_refuses_ambiguous_button_without_clicking(self):
        self.active_page.category_button_count = 2
        self.active_page.category_inspections = [{
            "title": "AIVIO Dashboard",
            "url": "https://aivio.example/dashboard",
            "inputs": [],
            "elements": [],
        }]
        self.connect()

        with self.assertRaisesRegex(RuntimeError, "exatamente um botão"):
            self.controller.inspectCategoryDropdown()

        self.assertFalse(self.active_page.category_clicked)


if __name__ == "__main__":
    unittest.main()
