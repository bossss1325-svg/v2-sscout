import argparse
import hashlib
import json
import logging
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.sync_api import Page

from scout.browser_controller import BrowserController, V2_CDP_ENDPOINT
from scout.lead_normalization import (
    deduplicate_leads,
    extract_lead,
    normalize_company_name,
    normalize_url,
)
from scout.v2_models import V2Lead

CDP_ENDPOINT = V2_CDP_ENDPOINT
logger = logging.getLogger("scout.aivio")
class AivioIntegration:
    def __init__(self, browser_controller: BrowserController, generation_timeout_ms: int = 120_000) -> None:
        if browser_controller.cdp_endpoint != CDP_ENDPOINT:
            raise ValueError(f"A integração AIVIO aceita somente CDP local em {CDP_ENDPOINT}")
        if not 1_000 <= generation_timeout_ms <= 600_000:
            raise ValueError("generation_timeout_ms deve estar entre 1000 e 600000")
        self.browser_controller = browser_controller
        self.generation_timeout_ms = generation_timeout_ms

    def search_city(
        self,
        city: str,
        open_first_company: bool = False,
        *,
        state: str | None = None,
        category: str | None = None,
        quantity: int = 20,
        filters: dict[str, Any] | None = None,
        allow_credit_consumption: bool = False,
    ) -> dict[str, Any]:
        if state is not None or category is not None or filters is not None or quantity != 20:
            if not state or not category:
                raise ValueError("Informe state e category para a pesquisa V2 estruturada")
            response = self.search_leads(
                city,
                state,
                category,
                quantity,
                filters,
                allow_credit_consumption=allow_credit_consumption,
            )
            response["companyDetails"] = None
            if open_first_company and response["results"]:
                first_lead = V2Lead.from_dict(response["results"][0])
                if first_lead.company_url:
                    response["companyDetails"] = self.read_company_page(
                        self._require_aivio_page(),
                        first_lead.company_url,
                    )
            return response

        if not allow_credit_consumption:
            raise PermissionError("SEARCH_LEADS exige autorização explícita para possível consumo de créditos")
        if not city.strip():
            raise ValueError("Informe uma cidade para pesquisar")

        self._require_aivio_page()
        self._select_city(city)
        before = self.browser_controller.capture_page_state()
        self._click_search_button()
        self.browser_controller.wait_for_page_change(before, timeout_ms=20_000)
        records = self.browser_controller.read_semantic_records()
        results = [self._record_to_search_result(item) for item in records]
        response: dict[str, Any] = {
            "status": "completed",
            "city": city,
            "results": results,
            "companyDetails": None,
        }
        if open_first_company:
            company = next(
                (
                    result for result in results
                    if result.get("url")
                ),
                None,
            )
            if company is None:
                raise RuntimeError("Nenhum link de empresa foi identificado nos resultados AIVIO")
            response["companyDetails"] = self.read_company_page(
                self._require_aivio_page(),
                company["url"],
            )
        return response

    def search_leads(
        self,
        city: str,
        state: str,
        category: str,
        quantity: int = 10,
        filters: dict[str, Any] | None = None,
        *,
        allow_credit_consumption: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if not allow_credit_consumption:
            raise PermissionError("SEARCH_LEADS exige autorização explícita para possível consumo de créditos")
        search_values = {"city": city, "state": state, "category": category}
        for name, value in search_values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Informe {name} para pesquisar")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10:
            raise ValueError("quantity deve ser um inteiro entre 1 e 10")
        filters = filters or {}
        if not isinstance(filters, dict):
            raise ValueError("filters deve ser um objeto")
        if "has_website" in filters and not isinstance(filters["has_website"], bool):
            raise ValueError("filters.has_website deve ser booleano")

        self._require_aivio_page()
        self._fill_search_fields(search_values)
        raw_results, search_warnings = self._run_search_pages(
            quantity,
            filters,
            on_progress=on_progress,
        )
        leads = deduplicate_leads([
            extract_lead(item, city=city, state=state, category=category)
            for item in raw_results
        ])
        if "has_website" in filters:
            expected = filters["has_website"]
            leads = [lead for lead in leads if lead.has_website is expected]
        leads = leads[:quantity]
        return {
            "status": "completed" if len(leads) >= quantity else "partial",
            "city": city,
            "state": state.strip(),
            "category": category.strip(),
            "requested_quantity": quantity,
            "results": [lead.to_dict() for lead in leads],
            "warnings": search_warnings + ([] if len(leads) >= quantity else [
                f"Foram encontrados {len(leads)} leads válidos de {quantity} solicitados."
            ]),
        }

    def open_company(
        self,
        lead: V2Lead,
        *,
        allow_credit_consumption: bool = False,
    ) -> V2Lead:
        page = self._require_aivio_page()
        direct_url = lead.company_url or lead.source_url
        if direct_url:
            self._assert_same_origin(page.url, direct_url)
            details = self.read_company_page(page, direct_url)
            return self._merge_company_details(lead, details)

        if not lead.city or not lead.state:
            raise RuntimeError("A busca para localizar a empresa exige city e state no lead")
        if not lead.category:
            raise RuntimeError("A busca para localizar a empresa exige category no lead")
        if not allow_credit_consumption:
            raise PermissionError(
                "OPEN_COMPANY sem URL exige autorização explícita para possível consumo de créditos"
            )
        search_values = {"city": lead.city, "state": lead.state}
        if lead.category:
            search_values["category"] = lead.category
        self._fill_search_fields(search_values)
        raw_results, _ = self._run_search_pages(100, {})
        requested_name = normalize_company_name(lead.company_name)
        match = next(
            (
                result for result in raw_results
                if normalize_company_name(result.get("title")) == requested_name
            ),
            None,
        )
        if match is None or not isinstance(match.get("url"), str):
            raise RuntimeError(f"Empresa não localizada no AIVIO: {lead.company_name}")
        self._assert_same_origin(page.url, match["url"])
        details = self.read_company_page(page, match["url"])
        return self._merge_company_details(lead, details)

    def generate_site(
        self,
        lead: V2Lead,
        *,
        allow_credit_consumption: bool = False,
        allow_external_effects: bool = False,
    ) -> tuple[V2Lead, list[dict[str, Any]], list[str]]:
        if not allow_credit_consumption or not allow_external_effects:
            raise PermissionError(
                "GENERATE_SITE exige autorização explícita para consumo de créditos e efeitos externos"
            )
        updated_lead = self.open_company(lead, allow_credit_consumption=allow_credit_consumption)
        page = self._require_aivio_page()
        generation_action = self._generation_button()
        if generation_action is None:
            raise RuntimeError("Ação de geração de site não identificada nos controles visíveis do AIVIO")
        before = self.browser_controller.capture_page_state()
        before_links = self.browser_controller.read_links()
        self.browser_controller.click_button(text=generation_action)
        self.browser_controller.wait_for_page_change(before, timeout_ms=self.generation_timeout_ms)
        self._assert_aivio_page(page)
        after_text = self.browser_controller.read_visible_text()
        after_links = self.browser_controller.read_links()
        old_urls = {
            link.get("href") for link in before_links
            if isinstance(link, dict) and isinstance(link.get("href"), str)
        }
        artifacts = self._artifacts_from_links([
            {"url": item.get("href"), "title": item.get("text", "")}
            for item in after_links if isinstance(item, dict)
        ], old_urls)
        success = bool(re.search(
            r"site.{0,30}(gerado|criado|pronto|publicado|conclu[ií]do)|(?:gerado|criado).{0,30}site",
            after_text,
            re.I,
        ))
        if not success:
            raise RuntimeError("A geração não foi confirmada por uma mensagem observável do AIVIO")
        warnings: list[str] = []
        if not artifacts:
            warnings.append("O AIVIO indicou conclusão, mas nenhum artefato novo foi identificado.")
        return updated_lead, artifacts, warnings

    def browser_health(self) -> dict[str, Any]:
        try:
            self.browser_controller.connect(timeout_ms=1_000)
            browser_status = self.browser_controller.getStatus()
        except Exception as error:
            logger.info("Estado do browser indisponível: %s", error)
            return {
                "browser_connected": False,
                "edge_connected": False,
                "edge_product": None,
                "browser_version": None,
                "active_tab": None,
                "aivio_available": False,
                "aivio_hostname": None,
                "aivio_url": None,
                "cdp_endpoint": self.browser_controller.cdp_endpoint,
                "expected_edge_profile": "EdgeProfile-V2",
                "operational": False,
                "browser_error": str(error),
            }
        active_tab = browser_status.get("activeTab")
        active_url = active_tab.get("url") if isinstance(active_tab, dict) else None
        hostname = urlsplit(active_url).hostname if isinstance(active_url, str) else None
        page = self.browser_controller.getActivePage()
        aivio_available = bool(
            page is not None
            and hostname
            and "aivio" in hostname.casefold()
        )
        browser_connected = bool(browser_status.get("connected"))
        return {
            "browser_connected": browser_connected,
            "edge_connected": browser_connected,
            "edge_product": browser_status.get("browser"),
            "browser_version": browser_status.get("browser_version"),
            "active_tab": active_tab,
            "aivio_available": aivio_available,
            "aivio_hostname": hostname,
            "aivio_url": active_url,
            "cdp_endpoint": browser_status.get("cdp_endpoint", self.browser_controller.cdp_endpoint),
            "expected_edge_profile": "EdgeProfile-V2",
            "operational": browser_connected and page is not None and aivio_available,
            "aivio_error": None if aivio_available else "A aba ativa não está em um hostname AIVIO.",
        }

    def select_category(self, category: str) -> dict[str, Any]:
        if not isinstance(category, str) or not category.strip():
            raise ValueError("Informe uma categoria para selecionar")
        self._require_aivio_page()
        selected_category = category.strip()
        steps = self._select_category(selected_category)
        return {
            "aivio": True,
            "category": selected_category,
            **steps,
        }

    def _fill_search_fields(self, fields: dict[str, str]) -> None:
        buttons = self.browser_controller.read_buttons()
        self._require_unique_category_combobox(buttons)
        search_label = self._search_button_label(buttons)
        if search_label is None:
            raise RuntimeError('Botão "Buscar" (ou legado "Ver agora") não encontrado no AIVIO')

        self._select_city(fields["city"])
        self._select_category(fields["category"])

    def _select_category(self, category: str) -> dict[str, bool]:
        if not isinstance(category, str) or not category.strip():
            raise ValueError("Informe uma categoria para selecionar")
        category = category.strip()
        steps = {
            "combobox_found": False,
            "click_performed": False,
            "dropdown_open": False,
            "option_found": False,
            "option_clicked": False,
            "category_confirmed": False,
        }
        self.browser_controller.open_dropdown(
            role="combobox",
            data_attributes={"data-slot": "select-trigger"},
        )
        steps["combobox_found"] = True
        steps["click_performed"] = True
        self.browser_controller.wait_for_dropdown_open(timeout_ms=5_000)
        steps["dropdown_open"] = True
        self.browser_controller.select_option(category, timeout_ms=5_000)
        steps["option_found"] = True
        steps["option_clicked"] = True
        steps["category_confirmed"] = True
        return steps

    def _select_city(self, city: str) -> None:
        if not isinstance(city, str) or not city.strip():
            raise ValueError("Informe uma cidade para pesquisar")
        city_field = {"placeholder": "Digite uma cidade..."}
        self.browser_controller.click_element(**city_field)
        self.browser_controller.fill_input(city, **city_field)
        self.browser_controller.wait_for_element(text=city, timeout_ms=5_000)
        self.browser_controller.click_text(city)

    @staticmethod
    def _require_unique_button(buttons: list[dict[str, Any]], label: str) -> dict[str, Any]:
        matches = [
            item for item in buttons
            if isinstance(item, dict)
            and not item.get("disabled")
            and (
                str(item.get("text") or "").strip().casefold() == label.casefold()
                or str(item.get("aria_label") or "").strip().casefold() == label.casefold()
            )
        ]
        if not matches:
            raise RuntimeError(f'Botão "{label}" não encontrado no AIVIO')
        if len(matches) != 1:
            raise RuntimeError(f'Botão "{label}" ambíguo: encontrados {len(matches)}')
        return matches[0]

    @staticmethod
    def _require_unique_category_combobox(buttons: list[dict[str, Any]]) -> dict[str, Any]:
        matches = [
            item for item in buttons
            if isinstance(item, dict)
            and not item.get("disabled")
            and (
                str(item.get("role") or "").casefold() == "combobox"
                or (
                    isinstance(item.get("data_attributes"), dict)
                    and item["data_attributes"].get("data-slot") == "select-trigger"
                )
            )
        ]
        if not matches:
            raise RuntimeError("Combobox de categoria não encontrado no AIVIO")
        if len(matches) != 1:
            raise RuntimeError(f"Combobox de categoria ambíguo: encontrados {len(matches)}")
        return matches[0]

    @staticmethod
    def _search_button_label(buttons: list[dict[str, Any]]) -> str | None:
        for label in ("Buscar", "Ver agora"):
            try:
                AivioIntegration._require_unique_button(buttons, label)
            except RuntimeError as error:
                if "não encontrado" in str(error):
                    continue
                raise
            return label
        return None

    def _click_search_button(self) -> None:
        label = self._search_button_label(self.browser_controller.read_buttons())
        if label is None:
            raise RuntimeError('Botão "Buscar" (ou legado "Ver agora") não encontrado no AIVIO')
        self.browser_controller.click_button(text=label)

    def _generation_button(self) -> str | None:
        labels = [
            item.get("text") or item.get("aria_label")
            for item in self.browser_controller.read_buttons()
            if isinstance(item, dict)
            and not item.get("disabled")
            and isinstance(item.get("text") or item.get("aria_label"), str)
            and re.search(
                r"(?:gerar|criar).{0,30}site|site.{0,30}(?:gerar|criar)",
                item.get("text") or item.get("aria_label") or "",
                re.I,
            )
        ]
        unique_labels = list(dict.fromkeys(labels))
        if len(unique_labels) > 1:
            raise RuntimeError("Ação de geração ambígua na página AIVIO")
        return unique_labels[0] if unique_labels else None

    def _record_to_search_result(self, record: dict[str, Any]) -> dict[str, Any]:
        active_tab = self.browser_controller.active_tab()
        current_url = active_tab.get("url", "") if isinstance(active_tab, dict) else ""
        same_origin_links = [
            item for item in record.get("links", [])
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and self._is_same_origin(current_url, item["url"])
        ]
        company_url = same_origin_links[0]["url"] if same_origin_links else None
        title = record.get("title") or (record.get("text", "").splitlines() or [""])[0]
        return {
            "title": title,
            "text": record.get("text", ""),
            "url": company_url,
            "links": [
                {"title": item.get("text", ""), "url": item.get("url")}
                for item in record.get("links", [])
                if isinstance(item, dict) and item.get("url")
            ],
            "source_url": record.get("source_url"),
            "elementType": "article-or-listitem",
        }

    def _run_search_pages(
        self,
        quantity: int,
        filters: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if on_progress is not None:
            on_progress({"phase": "searching", "page": 1})
        before = self.browser_controller.capture_search_results_state()
        self._click_search_button()
        page_state = self.browser_controller.wait_for_stable_search_results(
            before,
            timeout_ms=20_000,
        )
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen_pages: set[str] = set()
        visited_pages = 0
        while visited_pages < 100:
            if on_progress is not None:
                on_progress({
                    "phase": "results_ready",
                    "page": visited_pages + 1,
                    "record_count": page_state["record_count"],
                    "no_results": page_state["no_results"],
                })
            semantic_records = self.browser_controller.read_semantic_records()
            if not isinstance(semantic_records, list) or any(
                not isinstance(item, dict) for item in semantic_records
            ):
                raise RuntimeError("A leitura dos resultados AIVIO retornou um formato inválido")
            if not semantic_records and page_state["no_results"]:
                break
            if not semantic_records:
                raise RuntimeError("A página indicou resultados, mas nenhum registro semântico foi lido")
            current = [self._record_to_search_result(item) for item in semantic_records]
            signature = hashlib.sha256(
                json.dumps(current, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if signature in seen_pages:
                warnings.append("A paginação repetiu resultados; interrompida para evitar loop.")
                break
            seen_pages.add(signature)
            results.extend(item for item in current if isinstance(item, dict))
            visited_pages += 1
            leads = deduplicate_leads([extract_lead(item) for item in results])
            if "has_website" in filters:
                expected = filters["has_website"]
                leads = [lead for lead in leads if lead.has_website is expected]
            if len(leads) >= quantity:
                break
            next_button = self._next_page_button()
            if next_button is None:
                break
            if on_progress is not None:
                on_progress({
                    "phase": "next_page",
                    "page": visited_pages + 1,
                    "records_collected": len(results),
                })
            before = self.browser_controller.capture_search_results_state()
            self.browser_controller.click_button(text=next_button)
            page_state = self.browser_controller.wait_for_stable_search_results(
                before,
                timeout_ms=20_000,
            )
        else:
            warnings.append("Limite de 100 páginas atingido.")
        return results, warnings

    def _next_page_button(self) -> str | None:
        matches = [
            (item.get("text") or item.get("aria_label") or "").strip()
            for item in self.browser_controller.read_buttons()
            if isinstance(item, dict)
            and not item.get("disabled")
            and re.search(
                r"\b(next|pr[oó]xima|avan[cç]ar|mais resultados|ver mais)\b",
                item.get("text") or item.get("aria_label") or "",
                re.I,
            )
        ]
        labels = list(dict.fromkeys(matches))
        if len(labels) > 1:
            raise RuntimeError("Controles de paginação ambíguos")
        return labels[0] if labels else None

    @staticmethod
    def _merge_company_details(lead: V2Lead, details: dict[str, Any]) -> V2Lead:
        enriched = extract_lead(
            {
                "title": details.get("title"),
                "url": details.get("url"),
                "text": details.get("text"),
                "links": details.get("links", []),
            },
            city=lead.city,
            state=lead.state,
            category=lead.category,
            company_details=details,
        )
        for field_name in (
            "category", "phone", "address", "city", "state", "website", "instagram",
            "source_url", "company_url", "has_website",
        ):
            if getattr(enriched, field_name) is not None:
                setattr(lead, field_name, getattr(enriched, field_name))
        lead.raw_data["company_details"] = enriched.raw_data["company_details"]
        return lead

    @staticmethod
    def _artifacts_from_links(
        links: Any,
        previous_urls: set[str],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        if not isinstance(links, list):
            return artifacts
        for link in links:
            if not isinstance(link, dict):
                continue
            url = link.get("url")
            if not isinstance(url, str) or url in previous_urls:
                continue
            safe_url = normalize_url(url)
            if safe_url is None:
                continue
            url = safe_url
            parsed = urlsplit(url)
            title = link.get("title") if isinstance(link.get("title"), str) else ""
            if parsed.path.casefold().endswith(".pdf"):
                artifacts.append({"type": "pdf", "path": None, "url": url})
            elif parsed.scheme in {"http", "https"} and parsed.hostname:
                artifacts.append({"type": "website", "url": url, "title": title})
        return artifacts

    def read_company_page(self, page: Page, company_url: str) -> dict[str, Any]:
        self._assert_same_origin(page.url, company_url)
        self.browser_controller.navigate(company_url)
        active_tab = self.browser_controller.active_tab()
        if not isinstance(active_tab, dict):
            raise RuntimeError("A navegação não deixou uma aba ativa para leitura")
        current_page = self.browser_controller.getActivePage()
        if current_page is None:
            raise RuntimeError("A aba ativa não está disponível após a navegação")
        details = {
            "title": active_tab["title"],
            "url": active_tab["url"],
            "text": self.browser_controller.read_visible_text(),
            "links": [
                {"title": item.get("text", ""), "url": item.get("href")}
                for item in self.browser_controller.read_links()
                if isinstance(item, dict) and item.get("href")
            ],
        }
        self._assert_aivio_page(current_page)
        return details

    def _require_aivio_page(self) -> Page:
        page = self.browser_controller.getActivePage()
        if page is None:
            raise RuntimeError("Nenhuma aba ativa disponível; abra e autentique o AIVIO manualmente")
        self._assert_aivio_page(page)
        return page

    @staticmethod
    def _assert_aivio_page(page: Page) -> None:
        current_url = page.url or ""
        hostname = urlsplit(current_url).hostname or ""
        title = page.title() or ""
        if "aivio" not in hostname.casefold() and not re.search(r"\baivio\b", title, re.I):
            raise RuntimeError("A aba ativa não foi reconhecida como AIVIO; abra a página manualmente")

    @staticmethod
    def _assert_same_origin(current_url: str, target_url: str) -> None:
        if not AivioIntegration._is_same_origin(current_url, target_url):
            raise ValueError("A abertura de perfil aceita somente links HTTP(S) da mesma origem AIVIO")

    @staticmethod
    def _is_same_origin(current_url: str, target_url: str) -> bool:
        try:
            current = urlsplit(current_url or "")
            target = urlsplit(target_url or "")
            if (
                not current.scheme or not current.hostname
                or not target.scheme or not target.hostname
                or target.scheme.casefold() not in {"http", "https"}
                or target.username is not None
                or target.password is not None
            ):
                return False
            current_port = current.port or (443 if current.scheme.casefold() == "https" else 80)
            target_port = target.port or (443 if target.scheme.casefold() == "https" else 80)
            return (
                current.scheme.casefold(),
                current.hostname.casefold(),
                current_port,
            ) == (
                target.scheme.casefold(),
                target.hostname.casefold(),
                target_port,
            )
        except ValueError:
            return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scout.aivio")
    parser.add_argument("city", help="cidade a pesquisar na página AIVIO já aberta")
    parser.add_argument(
        "--open-first-company",
        action="store_true",
        help="abrir o primeiro perfil vinculado após ler os resultados",
    )
    parser.add_argument(
        "--allow-credit-consumption",
        action="store_true",
        help="autoriza explicitamente a busca, que pode consumir créditos AIVIO",
    )
    args = parser.parse_args()

    controller = BrowserController(cdp_endpoint=CDP_ENDPOINT)
    try:
        controller.connect()
        result = AivioIntegration(controller).search_city(
            args.city,
            args.open_first_company,
            allow_credit_consumption=args.allow_credit_consumption,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        controller.disconnect()


if __name__ == "__main__":
    main()
