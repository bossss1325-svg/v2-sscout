import unittest
from unittest.mock import patch

from scout.aivio import AivioIntegration
from scout.v2_models import V2Lead


class FakePage:
    def __init__(self):
        self.url = "https://app.aivio.example/search"
        self._title = "AIVIO - Pesquisa"

    def title(self):
        return self._title


class FakeBrowserController:
    def __init__(self, *, suggestions=None, buttons=None, pages=None, category_options=None):
        self.page = FakePage()
        self.cdp_endpoint = "http://127.0.0.1:9223"
        self.suggestions = suggestions if suggestions is not None else [
            "Porto",
            "Porto Alegre",
            "Porto Seguro-BA",
            "Porto Velho-RO",
            "Porto Covo",
        ]
        self.buttons = buttons if buttons is not None else [
            {
                "text": "Escolha o ramo",
                "disabled": False,
                "role": "combobox",
                "data_attributes": {"data-slot": "select-trigger"},
            },
            {"text": "Buscar", "disabled": False},
        ]
        self.pages = pages if pages is not None else [[{
            "title": "Restaurante Exemplo",
            "text": "Restaurante Exemplo\nCentro, Porto Alegre\nTelefone: (51) 99999-0000",
            "links": [{
                "text": "Empresa",
                "url": "https://app.aivio.example/company/123",
            }],
            "source_url": self.page.url,
        }]]
        self.page_index = 0
        self.operations = []
        self.text = "Pesquisa de empresas"
        self.links = []
        self.fail_wait = False
        self.no_results = False
        self.selected_city = None
        self.category_options = category_options if category_options is not None else [
            "Restaurantes, padarias e lanchonetes",
            "Bares e casas noturnas",
            "Salões de beleza e manicure",
            "Barbearias",
            "Clínicas de estética e massagem",
            "restaurantes",
        ]
        self.category_menu_open = False
        self.selected_category = "Escolha o ramo"

    def getActivePage(self):
        return self.page

    def connect(self, **kwargs):
        self.operations.append(("connect", kwargs))

    def active_tab(self):
        return {"title": self.page.title(), "url": self.page.url}

    def getStatus(self):
        return {
            "connected": True,
            "browser": "Microsoft Edge",
            "browser_version": "Edg/130.0.0.0",
            "cdp_endpoint": self.cdp_endpoint,
            "activeTab": self.active_tab(),
        }

    def read_buttons(self):
        return list(self.buttons)

    def read_inputs(self):
        self.operations.append(("read_inputs",))
        return [
            {
                "tag": "input",
                "type": "text",
                "placeholder": "Digite uma cidade...",
                "data_attributes": {"data-slot": "input"},
            },
            {
                "tag": "input",
                "type": "text",
                "id": "base-ui-_r_q_-hidden-input",
                "placeholder": "",
                "aria_label": "",
                "name": "",
                "labels": [],
                "data_attributes": {},
            },
        ]

    def click_element(self, **target):
        self.operations.append(("click_element", target))
        return {"clicked": True, "page_changed": False}

    def fill_input(self, value, **target):
        self.operations.append(("fill", value, target))
        self.typed_city = value
        return {"filled": True, "value_changed": True}

    def wait_for_element(self, *, text=None, role=None, accessible_name=None, timeout_ms):
        text = accessible_name or text
        self.operations.append(("wait_for_element", role, text, timeout_ms))
        matches = [suggestion for suggestion in self.suggestions if suggestion == text]
        if matches:
            if len(matches) > 1:
                raise ValueError(f"Alvo ambíguo para sugestão {text}")
            return True
        if role is None:
            raise TimeoutError(f"Sugestão exata não apareceu: {text}")
        options = [
            option for option in self.category_options
            if option == text
        ] if self.category_menu_open and role == "option" else []
        if not options:
            raise TimeoutError(f"Opção visível não apareceu: {text}")
        if len(options) > 1:
            raise ValueError(f"Opção ambígua: {text}")
        return True

    def click_text(self, text):
        self.operations.append(("click_text", text))
        if self.category_menu_open:
            matches = [option for option in self.category_options if option == text]
            if not matches:
                raise RuntimeError(f"Elemento não encontrado: opção {text}")
            if len(matches) > 1:
                raise ValueError(f"Alvo ambíguo para opção {text}")
            self.selected_category = matches[0]
            self.category_menu_open = False
            return {"clicked": True, "page_changed": False}
        matches = [suggestion for suggestion in self.suggestions if suggestion == text]
        if not matches:
            raise RuntimeError(f"Elemento não encontrado: sugestão {text}")
        if len(matches) > 1:
            raise ValueError(f"Alvo ambíguo para sugestão {text}")
        self.selected_city = matches[0]
        return {"clicked": True, "page_changed": True}

    def open_dropdown(self, trigger=None, **target):
        self.operations.append(("open_dropdown", trigger, target))
        self.category_menu_open = True
        return {"opened": True, "kind": "custom"}

    def wait_for_dropdown_open(self, *, timeout_ms):
        self.operations.append(("wait_for_dropdown_open", timeout_ms))
        if not self.category_menu_open:
            raise TimeoutError("O dropdown não abriu")
        return True

    def select_option(self, value, *, timeout_ms=5_000):
        self.operations.append(("select_option", value))
        matches = [option for option in self.category_options if option == value]
        if not matches:
            raise RuntimeError(f"Opção inexistente: {value}")
        if len(matches) > 1:
            raise ValueError(f"Opção ambígua: {value}")
        if not self.category_menu_open:
            raise RuntimeError("O dropdown não está aberto")
        self.selected_category = matches[0]
        self.category_menu_open = False
        return {"selected": True, "value_changed": True}

    def read_dropdown_state(self):
        self.operations.append(("read_dropdown_state",))
        return {"text": self.selected_category, "expanded": str(self.category_menu_open).lower()}

    def capture_page_state(self):
        return {"url": self.page.url, "text_hash": str(self.page_index)}

    def capture_search_results_state(self):
        self.operations.append(("capture_search_results_state",))
        return {"url": self.page.url, "results_hash": "before-search"}

    def click_button(self, *, text, **target):
        self.operations.append(("click_button", text))
        if text in {"Buscar", "Ver agora"}:
            self.page_index = min(self.page_index + 1, len(self.pages))
            self.text = "Resultados de busca"
        elif text in {"Next", "Próxima"}:
            self.page_index = min(self.page_index + 1, len(self.pages) - 1)
        elif "site" in text.casefold():
            self.text = "Site gerado com sucesso"
            self.links.append({
                "text": "Site gerado",
                "href": "https://restaurante.aivio.site/",
            })
        return {"clicked": True}

    def wait_for_page_change(self, before, timeout_ms=5_000):
        self.operations.append(("wait_for_page_change", timeout_ms))
        if self.fail_wait:
            raise TimeoutError("A página não mudou")
        return True

    def wait_for_stable_search_results(self, before, timeout_ms=20_000, stable_ms=750):
        self.operations.append(("wait_for_stable_search_results", timeout_ms, stable_ms))
        if self.fail_wait:
            raise TimeoutError("Resultados reais e estáveis não apareceram")
        records = self.read_semantic_records()
        return {
            "record_count": len(records),
            "no_results": self.no_results and not records,
            "signature": str(self.page_index),
        }

    def read_semantic_records(self):
        index = min(self.page_index, len(self.pages) - 1)
        return self.pages[index]

    def navigate(self, url):
        self.operations.append(("navigate", url))
        self.page.url = url
        self.page._title = "Restaurante Exemplo | AIVIO"
        self.text = "Restaurante Exemplo\nEndereço: Rua Central, 10\nTelefone: +55 51 0000-0000"
        self.links = [{"text": "Site", "href": "https://example.invalid/"}]
        return {"url": url}

    def read_visible_text(self):
        return self.text

    def read_links(self):
        return list(self.links)


class AivioIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeBrowserController()
        self.integration = AivioIntegration(self.controller)

    def test_search_requires_explicit_credit_authorization(self):
        with self.assertRaisesRegex(PermissionError, "autorização explícita"):
            self.integration.search_leads("Porto Alegre", "RS", "restaurantes")
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))

    def test_search_batch_is_limited_to_ten_leads(self):
        for quantity in (0, 11, 100, True):
            with self.subTest(quantity=quantity):
                with self.assertRaisesRegex(ValueError, "entre 1 e 10"):
                    self.integration.search_leads(
                        "Porto Alegre",
                        "RS",
                        "restaurantes",
                        quantity=quantity,
                        allow_credit_consumption=True,
                    )
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))

    def test_search_uses_confirmed_control_names_and_returns_normalized_leads(self):
        result = self.integration.search_leads(
            "Porto Alegre",
            "RS",
            "restaurantes",
            quantity=1,
            allow_credit_consumption=True,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["results"][0]["company_name"], "Restaurante Exemplo")
        self.assertEqual(result["results"][0]["phone"], "51999990000")
        self.assertEqual(result["results"][0]["company_url"], "https://app.aivio.example/company/123")
        self.assertIn(
            ("click_element", {"placeholder": "Digite uma cidade..."}),
            self.controller.operations,
        )
        self.assertIn(
            ("fill", "Porto Alegre", {"placeholder": "Digite uma cidade..."}),
            self.controller.operations,
        )
        self.assertIn(("wait_for_element", None, "Porto Alegre", 5_000), self.controller.operations)
        self.assertIn(("click_text", "Porto Alegre"), self.controller.operations)
        self.assertEqual(self.controller.selected_city, "Porto Alegre")
        self.assertIn((
            "open_dropdown",
            None,
            {"role": "combobox", "data_attributes": {"data-slot": "select-trigger"}},
        ), self.controller.operations)
        self.assertIn(("wait_for_dropdown_open", 5_000), self.controller.operations)
        self.assertIn(("select_option", "restaurantes"), self.controller.operations)
        self.assertEqual(self.controller.selected_category, "restaurantes")
        self.assertIn(("click_button", "Buscar"), self.controller.operations)
        operations = [item[0] for item in self.controller.operations]
        self.assertLess(operations.index("click_element"), operations.index("fill"))
        self.assertLess(operations.index("fill"), operations.index("wait_for_element"))
        self.assertLess(operations.index("wait_for_element"), operations.index("click_text"))
        self.assertLess(operations.index("click_text"), operations.index("open_dropdown"))
        self.assertLess(operations.index("open_dropdown"), operations.index("wait_for_dropdown_open"))
        self.assertLess(operations.index("wait_for_dropdown_open"), operations.index("select_option"))
        self.assertLess(operations.index("select_option"), operations.index("click_button"))
        self.assertNotIn(("read_inputs",), self.controller.operations)
        self.assertTrue(any(
            item[0] == "wait_for_stable_search_results"
            for item in self.controller.operations
        ))

    def test_structured_search_defaults_to_ten_without_changing_legacy_search_city(self):
        structured = self.integration.search_leads(
            "Porto Alegre", "RS", "restaurantes", allow_credit_consumption=True,
        )
        self.assertEqual(structured["requested_quantity"], 10)

        self.controller.operations.clear()
        legacy = self.integration.search_city(
            "Porto Alegre", allow_credit_consumption=True,
        )

        self.assertEqual(legacy["status"], "completed")
        self.assertTrue(any(item[0] == "wait_for_page_change" for item in self.controller.operations))
        self.assertFalse(any(
            item[0] == "capture_search_results_state"
            for item in self.controller.operations
        ))

    def test_select_category_uses_shared_dropdown_logic_without_search(self):
        category = "Restaurantes, padarias e lanchonetes"

        result = self.integration.select_category(category)

        self.assertEqual(result["category"], category)
        self.assertTrue(result["aivio"])
        self.assertTrue(result["combobox_found"])
        self.assertTrue(result["click_performed"])
        self.assertTrue(result["dropdown_open"])
        self.assertTrue(result["option_found"])
        self.assertTrue(result["option_clicked"])
        self.assertTrue(result["category_confirmed"])
        self.assertEqual(self.controller.selected_category, category)
        operations = [operation[0] for operation in self.controller.operations]
        self.assertLess(operations.index("open_dropdown"), operations.index("wait_for_dropdown_open"))
        self.assertLess(operations.index("wait_for_dropdown_open"), operations.index("select_option"))
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))

    def test_select_category_requires_aivio_page(self):
        self.controller.page.url = "https://example.com/"
        self.controller.page._title = "Example"

        with self.assertRaisesRegex(RuntimeError, "não foi reconhecida como AIVIO"):
            self.integration.select_category("Barbearias")

        self.assertFalse(any(item[0] == "open_dropdown" for item in self.controller.operations))
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))

    def test_search_passes_each_requested_city_to_the_autocomplete_unchanged(self):
        for city in ("Porto Alegre", "Canoas", "Caxias do Sul"):
            with self.subTest(city=city):
                controller = FakeBrowserController(suggestions=[city])
                integration = AivioIntegration(controller)
                result = integration.search_leads(
                    city,
                    "RS",
                    "restaurantes",
                    quantity=1,
                    allow_credit_consumption=True,
                )

                self.assertEqual(result["city"], city)
                self.assertIn(
                    ("fill", city, {"placeholder": "Digite uma cidade..."}),
                    controller.operations,
                )
                self.assertIn(("wait_for_element", None, city, 5_000), controller.operations)
                self.assertIn(("click_text", city), controller.operations)
                self.assertEqual(controller.selected_city, city)

    def test_city_suggestion_requires_exact_match_not_prefix_match(self):
        self.controller.suggestions = ["Porto", "Porto Seguro-BA", "Porto Velho-RO"]
        with self.assertRaisesRegex(TimeoutError, "Sugestão exata não apareceu"):
            self.integration.search_leads(
                "Porto Alegre",
                "RS",
                "restaurantes",
                allow_credit_consumption=True,
            )
        self.assertIsNone(self.controller.selected_city)
        self.assertFalse(any(item[0] == "open_dropdown" for item in self.controller.operations))
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))

    def test_city_suggestion_ambiguity_is_rejected(self):
        self.controller.suggestions = ["Porto Alegre", "Porto Alegre"]
        with self.assertRaisesRegex(ValueError, "ambíguo"):
            self.integration.search_leads(
                "Porto Alegre",
                "RS",
                "restaurantes",
                allow_credit_consumption=True,
            )
        self.assertFalse(any(item[0] == "open_dropdown" for item in self.controller.operations))

    def test_category_and_search_controls_must_be_unique(self):
        self.controller.buttons.append({"text": "Buscar", "disabled": False})
        with self.assertRaisesRegex(RuntimeError, "Buscar.*ambíguo"):
            self.integration.search_leads(
                "Porto Alegre", "RS", "restaurantes", allow_credit_consumption=True,
            )

    def test_category_dropdown_waits_for_and_selects_requested_visible_option(self):
        category = "Bares e casas noturnas"

        self.integration._fill_search_fields({
            "city": "Porto Alegre",
            "category": category,
        })

        self.assertEqual(self.controller.selected_category, category)
        operations = [operation[0] for operation in self.controller.operations]
        self.assertLess(
            operations.index("open_dropdown"),
            operations.index("wait_for_dropdown_open"),
        )
        self.assertLess(
            operations.index("wait_for_dropdown_open"),
            operations.index("select_option"),
        )
        self.assertFalse(any(item[0] == "click_button" for item in self.controller.operations))
        self.assertFalse(any(item[0] == "click_text" and item[1] == category
                             for item in self.controller.operations))

    def test_category_dropdown_rejects_missing_or_ambiguous_requested_option(self):
        for options, error in (
            (["Restaurantes"], "Opção inexistente"),
            (["Bares e casas noturnas", "Bares e casas noturnas"], "Opção ambígua"),
        ):
            with self.subTest(options=options):
                controller = FakeBrowserController(category_options=options)
                integration = AivioIntegration(controller)
                with self.assertRaisesRegex((RuntimeError, ValueError), error):
                    integration._fill_search_fields({
                        "city": "Porto Alegre",
                        "category": "Bares e casas noturnas",
                    })
                self.assertFalse(any(item[0] == "click_button" for item in controller.operations))

    def test_missing_category_control_fails_before_form_interaction(self):
        self.controller.buttons = [{"text": "Buscar", "disabled": False}]
        with self.assertRaisesRegex(RuntimeError, "Combobox de categoria não encontrado"):
            self.integration.search_leads(
                "Porto Alegre", "RS", "restaurantes", allow_credit_consumption=True,
            )
        self.assertFalse(any(item[0] == "fill" for item in self.controller.operations))

    def test_repeated_pages_stop_pagination(self):
        self.controller.pages = [[{
            "title": "Restaurante Exemplo",
            "text": "Restaurante Exemplo",
            "links": [],
            "source_url": self.controller.page.url,
        }]]
        self.controller.buttons.append({"text": "Next", "disabled": False})
        result = self.integration.search_leads(
            "Porto Alegre", "RS", "restaurantes", quantity=2,
            allow_credit_consumption=True,
        )
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(any("repetiu resultados" in warning for warning in result["warnings"]))

    def test_page_change_timeout_is_not_reported_as_success(self):
        self.controller.fail_wait = True
        with self.assertRaisesRegex(TimeoutError, "Resultados reais e estáveis"):
            self.integration.search_leads(
                "Porto Alegre", "RS", "restaurantes", allow_credit_consumption=True,
            )

    def test_explicit_no_results_is_returned_as_empty_partial_batch(self):
        self.controller.pages = [[]]
        self.controller.no_results = True

        result = self.integration.search_leads(
            "Porto Alegre", "RS", "restaurantes", allow_credit_consumption=True,
        )

        self.assertEqual(result["results"], [])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("0 leads válidos" in warning for warning in result["warnings"]))

    def test_health_reports_edge_version_endpoint_and_expected_v2_profile(self):
        health = self.integration.browser_health()

        self.assertTrue(health["browser_connected"])
        self.assertEqual(health["edge_product"], "Microsoft Edge")
        self.assertEqual(health["browser_version"], "Edg/130.0.0.0")
        self.assertEqual(health["cdp_endpoint"], "http://127.0.0.1:9223")
        self.assertEqual(health["expected_edge_profile"], "EdgeProfile-V2")
        self.assertTrue(health["aivio_available"])

    def test_open_company_uses_controller_navigation_and_same_origin(self):
        lead = V2Lead(
            company_name="Restaurante Exemplo",
            company_url="https://app.aivio.example/company/123",
        )
        updated = self.integration.open_company(lead)
        self.assertIn(
            ("navigate", "https://app.aivio.example/company/123"),
            self.controller.operations,
        )
        self.assertEqual(updated.address, "Rua Central, 10")
        with self.assertRaisesRegex(ValueError, "mesma origem"):
            self.integration.open_company(V2Lead(
                company_name="Externa",
                company_url="https://outside.example/company/1",
            ))

    def test_open_company_search_requires_credit_authorization(self):
        lead = V2Lead(
            company_name="Restaurante Exemplo",
            city="Porto Alegre",
            state="RS",
            category="restaurantes",
        )
        with self.assertRaisesRegex(PermissionError, "autorização explícita"):
            self.integration.open_company(lead)

    def test_open_company_search_requires_category_before_form_interaction(self):
        lead = V2Lead(company_name="Restaurante Exemplo", city="Porto Alegre", state="RS")
        with self.assertRaisesRegex(RuntimeError, "exige category"):
            self.integration.open_company(lead, allow_credit_consumption=True)
        self.assertFalse(any(item[0] == "fill" for item in self.controller.operations))

    def test_site_generation_requires_both_authorizations_and_observes_artifact(self):
        lead = V2Lead(
            company_name="Restaurante Exemplo",
            company_url="https://app.aivio.example/company/123",
        )
        self.controller.buttons.append({"text": "Gerar site", "disabled": False})
        with self.assertRaises(PermissionError):
            self.integration.generate_site(lead)
        updated, artifacts, warnings = self.integration.generate_site(
            lead,
            allow_credit_consumption=True,
            allow_external_effects=True,
        )
        self.assertTrue(updated.company_name)
        self.assertEqual(artifacts[0]["url"], "https://restaurante.aivio.site/")
        self.assertEqual(warnings, [])

    def test_new_link_without_generation_confirmation_is_not_success(self):
        lead = V2Lead(
            company_name="Restaurante Exemplo",
            company_url="https://app.aivio.example/company/123",
        )
        self.controller.buttons.append({"text": "Gerar site", "disabled": False})

        def add_unconfirmed_link(*, text):
            self.controller.links.append({
                "text": text,
                "href": "https://restaurante.aivio.site/",
            })

        with patch.object(
            self.controller,
            "click_button",
            side_effect=add_unconfirmed_link,
        ):
            with self.assertRaisesRegex(RuntimeError, "não foi confirmada"):
                self.integration.generate_site(
                    lead,
                    allow_credit_consumption=True,
                    allow_external_effects=True,
                )


if __name__ == "__main__":
    unittest.main()
