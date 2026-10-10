from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any, Callable, cast
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scout.browser_controller import (
    BrowserController,
    V2_CDP_ENDPOINT,
)
from scout.lead_normalization import deduplicate_leads, extract_lead
from scout.storage import V2StateStore
from scout.task_executor import _browser_execution_lock
from scout.v2_delivery import build_operator_result_package, evaluate_lead_criteria
from scout.v2_models import V2Lead
_MEMORY_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")

logger = logging.getLogger("scout.browser_operator")


class HumanInterventionRequired(RuntimeError):
    pass

_TARGET_FIELDS = {
    "role",
    "accessible_name",
    "text",
    "aria_label",
    "label",
    "placeholder",
    "name",
    "element_id",
    "data_attributes",
    "tag_name",
}
_ACTION_FIELDS = {
    "navigate": {"url", "timeout_ms"},
    "observe": set(),
    "click": {"target", "timeout_ms"},
    "fill": {"target", "value", "timeout_ms"},
    "search": {"query", "target", "submit_target", "timeout_ms"},
    "select": {"target", "option", "timeout_ms"},
    "press": {"key"},
    "scroll": {"target", "delta_x", "delta_y"},
    "wait": {"target", "text", "url", "load_state", "timeout_ms"},
    "wait_stable": {"timeout_ms", "stable_ms"},
    "read_text": {"target", "max_chars"},
    "read_records": {"max_items"},
    "open_tab": {"url", "timeout_ms"},
    "switch_tab": {"title", "url"},
    "close_tab": {"title", "url"},
    "back": {"timeout_ms"},
    "forward": {"timeout_ms"},
    "reload": {"timeout_ms"},
    "verify": {"target", "text", "url", "state", "timeout_ms"},
}
_SAFE_ERROR_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
    re.compile(
        r"""(?i)(?:access[_ -]?token|refresh[_ -]?token|csrf(?:[_ -]?token)?|"""
        r"""authorization|cookie|password|secret|api[_ -]?key)\s*[:=]\s*"""
        r"""["']?[^\s,;"'}&#]+"""
    ),
)
_SAFE_KEYS = {
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Space",
}
_PROHIBITED_TARGET_PATTERN = re.compile(
    r"payment|pay\b|checkout|billing|transaction|pagamento|pagar|cobran[çc]a|"
    r"purchase|whats\s*app|whatsapp|wa\.me|\bmessage\b|\bmensagem\b|\bsend\b|\benviar\b|\bchat\b",
    re.IGNORECASE,
)


class V2BrowserController(BrowserController):
    """V2-only entry point with a V2-only CDP endpoint."""

    def __init__(self, cdp_endpoint: str | None = None) -> None:
        endpoint = (
            cdp_endpoint
            if cdp_endpoint is not None
            else os.getenv("SCOUT_CDP_ENDPOINT", V2_CDP_ENDPOINT)
        )
        if endpoint != V2_CDP_ENDPOINT:
            raise ValueError(f"O operador V2 aceita somente {V2_CDP_ENDPOINT}")
        super().__init__(cdp_endpoint=endpoint)

    def navigate(self, url: str, timeout_ms: int = 30_000) -> dict[str, str]:
        self._assert_safe_destination(url)
        return super().navigate(url, timeout_ms)

    def open_new_tab(self, url: str, timeout_ms: int = 30_000) -> dict[str, str]:
        self._assert_safe_destination(url)
        return super().open_new_tab(url, timeout_ms)

    @staticmethod
    def _assert_safe_destination(url: str) -> None:
        parts = urlsplit(url)
        descriptor = f"{parts.hostname or ''} {parts.path}"
        if _PROHIBITED_TARGET_PATTERN.search(descriptor):
            raise ValueError("Navegação para pagamentos ou serviços de comunicação bloqueada")

    def _guard_target_action(self, locator: Any) -> None:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        self._assert_safe_page_context(page)
        super()._guard_target_action(locator)
        descriptor = locator.evaluate(
            """element => [
              element.innerText || element.textContent || '',
              element.getAttribute('aria-label') || '',
              element.getAttribute('placeholder') || '',
              element.getAttribute('name') || '',
              element.getAttribute('href') || '',
              Array.from(element.labels || [])
                .map(label => label.innerText || label.textContent || '').join(' ')
            ].join(' ')"""
        )
        if _PROHIBITED_TARGET_PATTERN.search(descriptor):
            raise ValueError("Interações com pagamentos e mensagens são bloqueadas")

    def _target_strategies(
        self,
        *,
        role: str | None = None,
        accessible_name: str | None = None,
        text: str | None = None,
        aria_label: str | None = None,
        label: str | None = None,
        placeholder: str | None = None,
        name: str | None = None,
        element_id: str | None = None,
        data_attributes: dict[str, str] | None = None,
        tag_name: str | None = None,
    ) -> list[tuple[str, Any]]:
        has_other_selector = any((
            accessible_name,
            text,
            aria_label,
            label,
            placeholder,
            name,
            element_id,
            data_attributes,
            tag_name,
        ))
        if role and not has_other_selector:
            page = self.getActivePage()
            if page is None:
                raise RuntimeError("Não há uma aba ativa disponível")
            return [("role", page.get_by_role(cast(Any, role)))]
        if accessible_name and not any((
            role,
            text,
            aria_label,
            label,
            placeholder,
            name,
            element_id,
            data_attributes,
            tag_name,
        )):
            page = self.getActivePage()
            if page is None:
                raise RuntimeError("Não há uma aba ativa disponível")
            return [
                (
                    "nome acessível",
                    page.locator(
                        self._css_attribute_selector("aria-label", accessible_name)
                    ),
                ),
                ("label", page.get_by_label(accessible_name, exact=True)),
                ("texto", page.get_by_text(accessible_name, exact=True)),
            ]
        return super()._target_strategies(
            role=role,
            accessible_name=accessible_name,
            text=text,
            aria_label=aria_label,
            label=label,
            placeholder=placeholder,
            name=name,
            element_id=element_id,
            data_attributes=data_attributes,
            tag_name=tag_name,
        )

    def _click_explicit_target(self, locator: Any, timeout_ms: int) -> dict[str, bool]:
        self._guard_target_action(locator)
        return super()._click_explicit_target(locator, timeout_ms)

    def press_operator_key(self, key: str) -> None:
        if key not in _SAFE_KEYS:
            raise ValueError("Tecla não permitida pelo operador")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        self._assert_safe_page_context(page)
        focused = page.locator(":focus")
        if focused.count():
            self._guard_target_action(focused)
            if key == "Enter":
                focused_kind = focused.evaluate(
                    """element => ({
                      tag: element.tagName.toLowerCase(),
                      type: (element.getAttribute('type') || '').toLowerCase()
                    })"""
                )
                if (
                    focused_kind == {"tag": "button", "type": "submit"}
                    or focused_kind == {"tag": "input", "type": "submit"}
                ):
                    raise ValueError(
                        "Enter em controles de submissão é bloqueado; use um clique explícito"
                    )
        page.keyboard.press(key)

    def fill_semantic_input(
        self,
        value: str,
        *,
        timeout_ms: int,
        **target: Any,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        if not isinstance(value, str):
            raise ValueError("value deve ser texto")
        locator = self._find_target(**target)
        tag_name = locator.evaluate("(element) => element.tagName.toLowerCase()")
        if tag_name not in {"input", "textarea"}:
            raise ValueError("O alvo identificado não é um campo de texto")
        if self._is_sensitive_field(locator):
            raise ValueError("Preenchimento de campos sensíveis está bloqueado")
        self._guard_target_action(locator)
        before_value = locator.input_value()
        locator.fill(value, timeout=timeout_ms)
        after_value = locator.input_value()
        if after_value != value:
            raise RuntimeError("O valor preenchido não foi confirmado pelo controle")
        return {"filled": True, "value_changed": before_value != after_value}

    def wait_for_semantic_target(self, *, timeout_ms: int, **target: Any) -> bool:
        self._validate_timeout(timeout_ms)
        strategies = self._target_strategies(**target)
        deadline = time.monotonic() + timeout_ms / 1000
        for _, locator in strategies:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                locator.first.wait_for(state="visible", timeout=remaining_ms)
            except PlaywrightTimeoutError:
                continue
            self._unique_visible(locator, "elemento aguardado")
            return True
        raise PlaywrightTimeoutError(f"Elemento não apareceu em {timeout_ms} ms")

    def wait_for_load_state(self, load_state: str, timeout_ms: int) -> str:
        if load_state not in {"load", "domcontentloaded", "networkidle"}:
            raise ValueError("load_state deve ser load, domcontentloaded ou networkidle")
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.wait_for_load_state(cast(Any, load_state), timeout=timeout_ms)
        return load_state

    @staticmethod
    def _assert_safe_page_context(page: Any) -> None:
        if _PROHIBITED_TARGET_PATTERN.search(f"{page.url} {page.title()}"):
            raise ValueError("Interações bloqueadas nesta página por segurança")

    def scroll_container(self, *, delta_y: int, **target: Any) -> dict[str, int | bool]:
        if isinstance(delta_y, bool) or not isinstance(delta_y, int):
            raise ValueError("delta_y deve ser um inteiro")
        locator = self._find_target(**target)
        state = locator.evaluate(
            """(element, delta) => {
              const canScroll = node => {
                const style = window.getComputedStyle(node);
                return node.scrollHeight > node.clientHeight + 1 &&
                  /auto|scroll|overlay/.test(style.overflowY);
              };
              let container = canScroll(element) ? element : element.parentElement;
              while (container && container !== document.body &&
                     container !== document.documentElement && !canScroll(container)) {
                container = container.parentElement;
              }
              if (!container || container === document.body ||
                  container === document.documentElement) {
                return {scrolled: false, scroll_top: 0, scroll_height: 0};
              }
              const before = container.scrollTop;
              container.scrollTop += delta;
              return {
                scrolled: container.scrollTop !== before,
                scroll_top: container.scrollTop,
                scroll_height: container.scrollHeight
              };
            }""",
            delta_y,
        )
        if not isinstance(state, dict) or not all(
            key in state for key in ("scrolled", "scroll_top", "scroll_height")
        ):
            raise RuntimeError("A rolagem do container retornou um formato inválido")
        return state

    def scroll_page(self, *, delta_x: int, delta_y: int) -> dict[str, int | bool]:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (delta_x, delta_y)):
            raise ValueError("delta_x e delta_y devem ser inteiros")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        state = page.evaluate(
            """({deltaX, deltaY}) => {
              const beforeX = window.scrollX;
              const beforeY = window.scrollY;
              window.scrollBy(deltaX, deltaY);
              return {
                scrolled: window.scrollX !== beforeX || window.scrollY !== beforeY,
                scroll_x: window.scrollX,
                scroll_y: window.scrollY
              };
            }""",
            {"deltaX": delta_x, "deltaY": delta_y},
        )
        if not isinstance(state, dict) or not all(
            key in state for key in ("scrolled", "scroll_x", "scroll_y")
        ):
            raise RuntimeError("A rolagem da página retornou um formato inválido")
        return state


class BrowserOperator:
    """Execute an explicit, bounded sequence of browser actions supplied by AUREN."""

    def __init__(
        self,
        controller: V2BrowserController | None = None,
        on_action: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.controller = controller or V2BrowserController()
        self.on_action = on_action

    @staticmethod
    def validate_task(task: Any) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ValueError("task deve ser um objeto JSON")
        unsupported = set(task) - {
            "task_id", "goal", "query", "quantity", "niche", "locality",
            "inclusion_criteria", "exclusion_criteria", "fields_of_interest",
            "start_url", "actions", "memory",
        }
        if unsupported:
            raise ValueError(f"task contém campos não suportados: {', '.join(sorted(unsupported))}")
        task_id = task.get("task_id")
        if not isinstance(task_id, str):
            raise ValueError("task_id deve ser um UUID válido")
        try:
            task_id = str(uuid.UUID(task_id))
        except ValueError as error:
            raise ValueError("task_id deve ser um UUID válido") from error
        goal = task.get("goal", "")
        if not isinstance(goal, str) or len(goal) > 500:
            raise ValueError("goal deve ser texto com até 500 caracteres")
        memory = task.get("memory")
        if memory is not None:
            if (
                not isinstance(memory, dict)
                or len(memory) != 1
                or set(memory) not in ({"save_as"}, {"reuse"})
            ):
                raise ValueError("memory deve conter exatamente save_as ou reuse")
            memory_operation, memory_name = next(iter(memory.items()))
            if not isinstance(memory_name, str) or not _MEMORY_NAME_PATTERN.fullmatch(memory_name):
                raise ValueError(
                    f"memory.{memory_operation} deve ter 1 a 64 caracteres "
                    "alfanuméricos, ponto, hífen ou sublinhado"
                )
        research: dict[str, Any] | None = None
        actions: Any = None
        query = task.get("query")
        if query is not None:
            if not _nonempty_text(query) or len(query) > 500:
                raise ValueError("query deve ser texto não vazio com até 500 caracteres")
            if "actions" in task or memory is not None:
                raise ValueError("query declarativa não pode ser combinada com actions ou memory")
            quantity = task.get("quantity", 10)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10:
                raise ValueError("quantity deve ser um inteiro entre 1 e 10")
            criteria: dict[str, list[str]] = {}
            for name in ("inclusion_criteria", "exclusion_criteria"):
                values = task.get(name, [])
                if (
                    not isinstance(values, list)
                    or len(values) > 20
                    or any(not _nonempty_text(item) or len(item) > 300 for item in values)
                ):
                    raise ValueError(f"{name} deve ser uma lista de até 20 textos não vazios")
                criteria[name] = list(dict.fromkeys(item.strip() for item in values))
            allowed_fields = {
                "company_name", "phone", "address", "website", "instagram", "source_url",
                "company_url", "city", "state", "category", "has_website",
            }
            fields = task.get("fields_of_interest", [
                "company_name", "phone", "address", "website", "instagram", "source_url",
            ])
            if (
                not isinstance(fields, list)
                or not fields
                or len(fields) > len(allowed_fields)
                or any(field not in allowed_fields for field in fields)
            ):
                raise ValueError("fields_of_interest contém campos inválidos")
            niche = task.get("niche", "")
            locality = task.get("locality", "")
            start_url = task.get("start_url", "")
            if not isinstance(niche, str) or len(niche) > 200:
                raise ValueError("niche deve ser texto com até 200 caracteres")
            if not isinstance(locality, str) or len(locality) > 300:
                raise ValueError("locality deve ser texto com até 300 caracteres")
            if not isinstance(start_url, str) or len(start_url) > 2_000:
                raise ValueError("start_url deve ser texto com até 2000 caracteres")
            if start_url.strip():
                try:
                    source_url = urlsplit(start_url.strip())
                    valid_source = (
                        source_url.scheme in {"http", "https"}
                        and bool(source_url.hostname)
                        and source_url.username is None
                        and source_url.password is None
                        and source_url.port != 0
                    )
                except ValueError:
                    valid_source = False
                if not valid_source:
                    raise ValueError("start_url deve ser HTTP(S), público e sem credenciais")
            research = {
                "query": query.strip(),
                "quantity": quantity,
                "inclusion_criteria": criteria["inclusion_criteria"],
                "exclusion_criteria": criteria["exclusion_criteria"],
                "fields_of_interest": list(dict.fromkeys(fields)),
                "niche": niche.strip(),
                "locality": locality.strip(),
                "start_url": start_url.strip(),
            }
            actions = ([{"action": "navigate", "url": start_url.strip(), "timeout_ms": 20_000}]
                if start_url.strip() else []) + [
                {"action": "search", "query": query.strip(), "timeout_ms": 20_000},
                {"action": "read_records", "max_items": quantity},
            ]
        else:
            actions = task.get("actions")
        if memory and "reuse" in memory:
            if "actions" in task:
                raise ValueError("actions não pode ser informado junto com memory.reuse")
            actions = []
        elif not isinstance(actions, list) or not actions:
            raise ValueError("actions deve ser uma lista não vazia")
        if len(actions) > 50:
            raise ValueError("actions aceita no máximo 50 operações")
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                raise ValueError(f"actions[{index}] deve ser um objeto")
            action_name = action.get("action")
            if not isinstance(action_name, str) or action_name not in _ACTION_FIELDS:
                raise ValueError(f"actions[{index}].action não é suportada")
            unsupported = set(action) - _ACTION_FIELDS[action_name] - {"action"}
            if unsupported:
                raise ValueError(
                    f"actions[{index}] contém campos não suportados: {', '.join(sorted(unsupported))}"
                )
            timeout = action.get("timeout_ms", 5_000)
            if (
                "timeout_ms" in _ACTION_FIELDS[action_name]
                and (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, int)
                    or timeout < 1
                )
            ):
                raise ValueError(f"actions[{index}].timeout_ms deve ser um inteiro positivo")
            target = action.get("target")
            if target is not None:
                BrowserOperator._validate_target(target, index)
            submit_target = action.get("submit_target")
            if submit_target is not None:
                BrowserOperator._validate_target(submit_target, index)
            if action_name in {"click", "fill", "select"} and target is None:
                raise ValueError(f"actions[{index}].target é obrigatório")
            if action_name == "fill" and not isinstance(action.get("value"), str):
                raise ValueError(f"actions[{index}].value deve ser texto")
            if action_name == "search" and (
                not _nonempty_text(action.get("query"))
                or len(action["query"]) > 500
            ):
                raise ValueError(f"actions[{index}].query deve ter até 500 caracteres")
            if action_name == "read_records" and (
                isinstance(action.get("max_items", 200), bool)
                or not isinstance(action.get("max_items", 200), int)
                or not 1 <= action.get("max_items", 200) <= 1_000
            ):
                raise ValueError(f"actions[{index}].max_items deve estar entre 1 e 1000")
            if action_name == "read_text" and (
                isinstance(action.get("max_chars"), bool)
                or not isinstance(action.get("max_chars", 12_000), int)
                or action.get("max_chars", 12_000) < 0
            ):
                raise ValueError(f"actions[{index}].max_chars deve ser um inteiro não negativo")
            if action_name == "wait_stable":
                stable_ms = action.get("stable_ms", 750)
                if (
                    isinstance(stable_ms, bool)
                    or not isinstance(stable_ms, int)
                    or stable_ms < 1
                    or stable_ms > timeout
                ):
                    raise ValueError(
                        f"actions[{index}].stable_ms deve ser positivo e não superar timeout_ms"
                    )
            if action_name == "select" and not _nonempty_text(action.get("option")):
                raise ValueError(f"actions[{index}].option deve ser texto não vazio")
            if action_name in {"navigate", "open_tab"} and not _nonempty_text(action.get("url")):
                raise ValueError(f"actions[{index}].url deve ser texto não vazio")
            if action_name == "switch_tab":
                if (
                    (action.get("title") is None) == (action.get("url") is None)
                    or any(
                        key in action and not _nonempty_text(action[key])
                        for key in ("title", "url")
                    )
                ):
                    raise ValueError(f"actions[{index}] requer exatamente title ou url")
            if action_name == "close_tab":
                if (action.get("title") is None) != (action.get("url") is None):
                    raise ValueError(
                        f"actions[{index}] requer title e url juntos ou ambos omitidos"
                    )
                if any(
                    key in action and not _nonempty_text(action[key])
                    for key in ("title", "url")
                ):
                    raise ValueError(f"actions[{index}].title e url devem ser textos não vazios")
            if action_name == "wait":
                conditions = sum(
                    action.get(key) is not None
                    for key in ("target", "text", "url", "load_state")
                )
                if conditions != 1:
                    raise ValueError(
                        f"actions[{index}] requer exatamente target, text, url ou load_state"
                    )
                if "text" in action and not _nonempty_text(action["text"]):
                    raise ValueError(f"actions[{index}].text deve ser texto não vazio")
                if "url" in action and not _nonempty_text(action["url"]):
                    raise ValueError(f"actions[{index}].url deve ser texto não vazio")
                if "load_state" in action and (
                    not isinstance(action["load_state"], str)
                    or action["load_state"] not in {
                        "load",
                        "domcontentloaded",
                        "networkidle",
                    }
                ):
                    raise ValueError(f"actions[{index}].load_state não é suportado")
            if action_name == "verify":
                conditions = sum(
                    action.get(key) is not None
                    for key in ("target", "text", "url")
                )
                if conditions > 1 or (conditions == 0 and action.get("state") != "page_changed"):
                    raise ValueError(
                        f"actions[{index}] requer exatamente target, text, url ou state"
                    )
                if conditions == 1 and action.get("state") == "page_changed":
                    raise ValueError(
                        f"actions[{index}].state page_changed requer verificação sem target/text/url"
                    )
                state = action.get("state")
                if state is not None and not isinstance(state, str):
                    raise ValueError(f"actions[{index}].state não é suportado")
                if state not in {
                    None,
                    "page_changed",
                    "visible",
                    "enabled",
                    "selected",
                    "filled",
                }:
                    raise ValueError(f"actions[{index}].state não é suportado")
                if conditions == 1 and "target" not in action and state is not None:
                    raise ValueError(
                        f"actions[{index}].state só é permitido com target ou page_changed"
                    )
            if action_name == "press" and (
                not isinstance(action.get("key"), str)
                or action["key"] not in _SAFE_KEYS
            ):
                raise ValueError(f"actions[{index}].key não é permitida")
            if action_name == "scroll":
                for key in ("delta_x", "delta_y"):
                    value = action.get(key, 0)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(f"actions[{index}].{key} deve ser um inteiro")
                if target and action.get("delta_x", 0):
                    raise ValueError(
                        f"actions[{index}] com target aceita somente delta_y"
                    )
            if action_name == "verify":
                if "text" in action and not _nonempty_text(action["text"]):
                    raise ValueError(f"actions[{index}].text deve ser texto não vazio")
                if "url" in action and not _nonempty_text(action["url"]):
                    raise ValueError(f"actions[{index}].url deve ser texto não vazio")
        return {
            "task_id": task_id,
            **({"goal": goal.strip()} if goal.strip() else {}),
            **({"research": research} if research is not None else {}),
            **({"actions": actions} if not memory or "reuse" not in memory else {}),
            **({"memory": memory} if memory is not None else {}),
        }

    @staticmethod
    def _validate_target(target: Any, index: int) -> None:
        if not isinstance(target, dict) or not target:
            raise ValueError(f"actions[{index}].target deve ser um objeto não vazio")
        unknown = set(target) - _TARGET_FIELDS
        if unknown:
            raise ValueError(
                f"actions[{index}].target contém critérios não suportados: "
                f"{', '.join(sorted(unknown))}"
            )
        if not set(target) - {"tag_name"}:
            raise ValueError(
                f"actions[{index}].target precisa de um critério além de tag_name"
            )
        for name, value in target.items():
            if name == "data_attributes":
                if not isinstance(value, dict) or not value or any(
                    not isinstance(key, str)
                    or not key.startswith("data-")
                    or not isinstance(item, str)
                    for key, item in value.items()
                ):
                    raise ValueError(
                        f"actions[{index}].target.data_attributes deve conter data-* de texto"
                    )
            elif not _nonempty_text(value):
                raise ValueError(f"actions[{index}].target.{name} deve ser texto não vazio")

    def execute(self, task: Any) -> dict[str, Any]:
        try:
            validated = self.validate_task(task)
            if validated.get("memory"):
                raise ValueError("memory operacional requer BrowserOperatorExecutor")
        except ValueError as error:
            task_id = None
            if isinstance(task, dict) and isinstance(task.get("task_id"), str):
                try:
                    task_id = str(uuid.UUID(task["task_id"]))
                except ValueError:
                    pass
            return {
                "task_id": task_id,
                "status": "failed",
                "action": None,
                "result": None,
                "evidence": [],
                "error": {"code": "invalid_task", "message": str(error)},
            }

        records: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        failed: dict[str, Any] | None = None
        current_action: dict[str, Any] | None = None
        transition_baseline: dict[str, Any] | None = None
        try:
            self.controller.connect()
            for index, action in enumerate(validated["actions"]):
                current_action = {"index": index, "name": action["action"]}
                before = self._observe_state()
                result = self._run_action(action, before, transition_baseline)
                after = self._observe_state()
                record = {
                    "index": index,
                    "action": action["action"],
                    "status": "completed",
                    "result": self._sanitize_result(result),
                    "evidence": {
                        "before": self._public_observation(before),
                        "after": self._public_observation(after),
                    },
                }
                if isinstance(action.get("target"), dict):
                    record["target"] = self._safe_target(action["target"])
                records.append(record)
                evidence.append(record["evidence"])
                if self.on_action is not None:
                    self.on_action(deepcopy(record))
                if action["action"] not in {"observe", "read_text", "wait", "verify"}:
                    transition_baseline = before
                elif action["action"] == "verify":
                    transition_baseline = None
        except Exception as error:
            code = "operation_timeout" if isinstance(
                error, (PlaywrightTimeoutError, TimeoutError)
            ) else "human_intervention_required" if isinstance(
                error, HumanInterventionRequired
            ) or (isinstance(error, ValueError) and "ambíguo" in str(error).casefold()) else "operation_failed"
            failed = {"code": code, "message": self._safe_error(error, action if "action" in locals() else {})}
            logger.error(
                "Operação web V2 falhou task_id=%s action=%s error=%s",
                validated["task_id"],
                current_action["name"] if current_action else None,
                failed["message"],
            )
            if current_action is not None:
                records.append({
                    "index": current_action["index"],
                    "action": current_action["name"],
                    "status": "failed",
                    "error": failed,
                })
                if self.on_action is not None:
                    self.on_action(deepcopy(records[-1]))
        finally:
            try:
                self.controller.disconnect()
            except Exception as error:
                cleanup_error = self._safe_error(error, {})
                logger.error(
                    "Falha ao desconectar operador web V2 task_id=%s error=%s",
                    validated["task_id"],
                    cleanup_error,
                )
                if failed is None:
                    failed = {"code": "disconnect_failed", "message": cleanup_error}
                else:
                    failed["cleanup_error"] = cleanup_error

        needs_human = failed is not None and failed.get("code") == "human_intervention_required"
        status = "needs_human" if needs_human else "failed" if not records or (failed and not any(
            record["status"] == "completed" for record in records
        )) else "partial" if failed else "completed"
        output: dict[str, Any] = {
            "task_id": validated["task_id"],
            "status": status,
            "action": current_action,
            "result": [record.get("result") for record in records if record["status"] == "completed"],
            "evidence": evidence,
            "operations": records,
        }
        if failed is not None:
            output["error"] = failed
        return output

    def _observe_state(self) -> dict[str, Any]:
        tab = self.controller.getActiveTab()
        if tab is None:
            raise RuntimeError("Não há uma aba ativa disponível para observar")
        return {
            "title": tab["title"],
            "url": tab["url"],
            "state": self.controller.capture_page_state(),
        }

    def _public_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        state = dict(observation["state"])
        state_url = state.get("url")
        if isinstance(state_url, str):
            state["url"] = self.controller._safe_page_url(state_url)
        return {
            "title": observation["title"],
            "url": observation["url"],
            "state": state,
        }

    def _run_action(
        self,
        action: dict[str, Any],
        before: dict[str, Any],
        transition_baseline: dict[str, Any] | None,
    ) -> Any:
        name = action["action"]
        timeout = action.get("timeout_ms", 5_000)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError("timeout_ms deve ser um inteiro positivo")
        target = action.get("target")
        if name == "navigate":
            return self.controller.navigate(action["url"], timeout_ms=timeout)
        if name == "observe":
            return self.controller.inspectPage()
        if name == "click":
            target = self._require_target(target)
            self.controller.wait_for_semantic_target(timeout_ms=timeout, **target)
            found = self.controller.find_element(**target)
            if not found["found"]:
                raise RuntimeError("Elemento não encontrado para clique")
            return self.controller.click_element(timeout_ms=timeout, **target)
        if name == "fill":
            target = self._require_target(target)
            self.controller.wait_for_semantic_target(timeout_ms=timeout, **target)
            found = self.controller.find_element(**target)
            if not found["found"]:
                raise RuntimeError("Campo não encontrado para preenchimento")
            primitive_target = self._fill_input_target(target)
            if primitive_target is not None:
                return self.controller.fill_input(
                    action["value"], timeout_ms=timeout, **primitive_target
                )
            return self.controller.fill_semantic_input(
                action["value"], timeout_ms=timeout, **target
            )
        if name == "search":
            return self._search(action, timeout)
        if name == "select":
            target = self._require_target(target)
            self._assert_action_text_allowed(action["option"])
            self.controller.wait_for_semantic_target(timeout_ms=timeout, **target)
            found = self.controller.find_element(**target)
            if not found["found"]:
                raise RuntimeError("Dropdown não encontrado")
            if found["element"]["tag"] == "select":
                return self.controller.select_native_option(
                    action["option"], timeout_ms=timeout, **target
                )
            dropdown_target = self._dropdown_target(target)
            self.controller.open_dropdown(timeout_ms=timeout, **dropdown_target)
            return self.controller.select_option(action["option"], timeout_ms=timeout)
        if name == "press":
            key = action.get("key")
            if not isinstance(key, str):
                raise ValueError("key deve ser texto")
            self.controller.press_operator_key(key)
            return {"pressed": key}
        if name == "scroll":
            target = target if target is not None else {}
            delta_x = action.get("delta_x", 0)
            delta_y = action.get("delta_y", 0)
            for value in (delta_x, delta_y):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("delta_x e delta_y devem ser inteiros")
            if target:
                if delta_x:
                    raise ValueError("rolagem de container aceita somente delta_y")
                return self.controller.scroll_container(delta_y=delta_y, **target)
            return self.controller.scroll_page(delta_x=delta_x, delta_y=delta_y)
        if name == "wait":
            if target is not None:
                if not self.controller.wait_for_semantic_target(
                    timeout_ms=timeout, **target
                ):
                    raise RuntimeError("Elemento não apareceu")
                return {"waited_for": "element"}
            if "text" in action:
                self.controller.wait_for_text(action["text"], timeout_ms=timeout)
                return {"waited_for": "text"}
            if "load_state" in action:
                state = self.controller.wait_for_load_state(
                    cast(Any, action["load_state"]), timeout_ms=timeout
                )
                return {"waited_for": "load_state", "state": state}
            return {
                "url": self.controller._safe_page_url(
                    self.controller.wait_for_url(action["url"], timeout_ms=timeout)
                )
            }
        if name == "wait_stable":
            return self.controller.wait_for_stable_content(
                before["state"],
                timeout_ms=timeout,
                stable_ms=action.get("stable_ms", 750),
            )
        if name == "read_text":
            if target is None:
                return {"text": self.controller.read_visible_text(action.get("max_chars", 12_000))}
            found = self.controller.find_element(**target)
            if not found["found"]:
                raise RuntimeError("Elemento não encontrado para leitura")
            return {"text": found["element"]["text"]}
        if name == "read_records":
            records = self.controller.read_semantic_records(action.get("max_items", 200))
            page = self.controller.getActivePage()
            if page is None:
                raise RuntimeError("Não há uma aba ativa para identificar a origem dos records")
            return {
                "count": len(records),
                "records": records,
                "source_url": self.controller._safe_page_url(page.url),
            }
        if name == "open_tab":
            return self.controller.open_new_tab(action["url"], timeout_ms=timeout)
        if name == "switch_tab":
            return self.controller.switch_tab(title=action.get("title"), url=action.get("url"))
        if name == "close_tab":
            return self.controller.close_tab(title=action.get("title"), url=action.get("url"))
        if name in {"back", "forward", "reload"}:
            return getattr(self.controller, name)(timeout_ms=timeout)
        if name == "verify":
            return self._verify(
                action,
                transition_baseline or before,
                timeout,
            )
        raise ValueError(f"Ação não suportada: {name}")

    def _search(self, action: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        inspection = self.controller.inspectPage()
        inputs = inspection.get("inputs", [])
        target = action.get("target")
        field_info: dict[str, Any] | None = None
        if target is None:
            candidates = [
                item for item in inputs
                if isinstance(item, dict)
                and (
                    item.get("type") == "search"
                    or re.search(
                        r"search|pesquis|buscar|consulta|find",
                        " ".join(str(value) for value in (
                            item.get("name"), item.get("id"), item.get("aria_label"),
                            item.get("placeholder"), *(item.get("labels") or []),
                        )),
                        re.I,
                    )
                )
            ]
            if len(candidates) != 1:
                raise HumanInterventionRequired(
                    f"Esperava um único campo de busca semântico; encontrados {len(candidates)}"
                )
            field_info = candidates[0]
            target = self._input_target(field_info)
        found = self.controller.find_element(**target)
        if not found.get("found"):
            raise RuntimeError("Campo de busca não encontrado")
        if field_info is None and isinstance(found.get("element"), dict):
            field_info = found["element"]
        self.controller.wait_for_semantic_target(timeout_ms=timeout_ms, **target)
        before = self.controller.capture_page_state()
        self.controller.fill_semantic_input(action["query"], timeout_ms=timeout_ms, **target)

        submit_target = action.get("submit_target")
        submit_label: str | None = None
        if submit_target is None:
            buttons = inspection.get("buttons", [])
            matches = [
                item for item in buttons
                if isinstance(item, dict)
                and not item.get("disabled")
                and re.fullmatch(
                    r"\s*(search|pesquisar|buscar|find|go)\s*",
                    item.get("text") or item.get("aria_label") or "",
                    re.I,
                )
            ]
            labels = list(dict.fromkeys(
                (item.get("text") or item.get("aria_label") or "").strip()
                for item in matches
            ))
            if len(labels) > 1:
                raise HumanInterventionRequired("Botão de busca ambíguo")
            if labels:
                submit_label = labels[0]
                submit_target = {"role": "button", "accessible_name": submit_label}

        if submit_target is not None:
            self.controller.wait_for_semantic_target(timeout_ms=timeout_ms, **submit_target)
            self.controller.click_element(timeout_ms=timeout_ms, **submit_target)
        elif field_info is not None and field_info.get("type") == "search":
            field = self.controller._find_target(**target)
            field.focus()
            self.controller.press_operator_key("Enter")
            submit_label = "Enter"
        else:
            raise HumanInterventionRequired("Nenhum controle de envio da busca foi identificado")

        stable = self.controller.wait_for_stable_content(before, timeout_ms=timeout_ms)
        return {
            "query": action["query"],
            "search_field": self._safe_target(target),
            "submitted_by": self._safe_target(submit_target) if submit_target else submit_label,
            "stable_content": stable,
        }

    @staticmethod
    def _input_target(item: dict[str, Any]) -> dict[str, Any]:
        aria_label = item.get("aria_label")
        if isinstance(aria_label, str) and aria_label.strip():
            return {"aria_label": aria_label.strip()}
        labels = item.get("labels")
        if isinstance(labels, list) and len(labels) == 1 and isinstance(labels[0], str):
            return {"label": labels[0]}
        for key in ("placeholder", "name", "element_id"):
            value = item.get("id") if key == "element_id" else item.get(key)
            if isinstance(value, str) and value.strip():
                return {key: value.strip()}
        raise RuntimeError("Campo de busca sem critério acessível estável")

    def _verify(self, action: dict[str, Any], before: dict[str, Any], timeout: int) -> dict[str, Any]:
        if "target" in action:
            target = action["target"]
            found = self.controller.find_element(**target)
            expected_state = action.get("state", "visible")
            if not found["found"]:
                raise RuntimeError("Elemento não encontrado durante a verificação")
            state_checks: dict[str, Callable[..., bool]] = {
                "visible": self.controller.element_visible,
                "enabled": self.controller.element_enabled,
                "selected": self.controller.element_selected,
                "filled": self.controller.element_filled,
            }
            if expected_state not in state_checks:
                raise ValueError("state deve ser visible, enabled, selected ou filled")
            verified = state_checks[expected_state](**target)
            if not verified:
                raise RuntimeError(f"Verificação do elemento falhou: {expected_state}")
            return {"verified": True, "state": expected_state, "element": found["element"]}
        if "text" in action:
            self.controller.wait_for_text(action["text"], timeout_ms=timeout)
            return {"verified": True, "text": action["text"]}
        if "url" in action:
            url = self.controller.wait_for_url(action["url"], timeout_ms=timeout)
            return {"verified": True, "url": self.controller._safe_page_url(url)}
        if action.get("state") == "page_changed":
            changed = self.controller.verify_page_changed(before["state"], timeout)
            if not changed:
                raise PlaywrightTimeoutError(f"A página não mudou em {timeout} ms")
            return {"verified": True, "state": "page_changed"}
        raise ValueError("state deve ser page_changed quando target, text e url não forem informados")

    @staticmethod
    def _require_target(target: Any) -> dict[str, Any]:
        if not isinstance(target, dict) or not target:
            raise ValueError("target deve ser um objeto não vazio")
        return target

    @staticmethod
    def _fill_input_target(target: dict[str, Any]) -> dict[str, Any] | None:
        if target.get("role") not in (None, "textbox") or "tag_name" in target:
            return None
        selector_groups = sum((
            bool(target.get("accessible_name")),
            bool(target.get("text")),
            bool(target.get("aria_label")),
            bool(target.get("label")),
            bool(target.get("placeholder")),
            bool(target.get("name")),
            bool(target.get("element_id")),
            bool(target.get("data_attributes")),
        ))
        if selector_groups != 1:
            return None
        supported_fields = {
            "aria_label",
            "label",
            "placeholder",
            "name",
            "element_id",
            "data_attributes",
        }
        primitive_target = {
            key: value for key, value in target.items() if key in supported_fields
        }
        if target.get("accessible_name") is not None:
            primitive_target["label"] = target["accessible_name"]
        elif target.get("text") is not None:
            primitive_target["label"] = target["text"]
        return primitive_target

    @staticmethod
    def _dropdown_target(target: dict[str, Any]) -> dict[str, Any]:
        supported_fields = {
            "role",
            "aria_label",
            "label",
            "placeholder",
            "name",
            "element_id",
            "data_attributes",
        }
        dropdown_target = {
            key: value for key, value in target.items() if key in supported_fields
        }
        trigger = target.get("accessible_name") or target.get("text")
        if trigger is not None:
            dropdown_target["trigger"] = trigger
        return dropdown_target

    @staticmethod
    def _sanitize_result(result: Any) -> Any:
        return BrowserController._sanitize_records(result)

    @staticmethod
    def _safe_error(error: Exception, action: dict[str, Any]) -> str:
        message = f"{type(error).__name__}: {error}"
        for field in ("value", "option"):
            value = action.get(field)
            if isinstance(value, str) and value:
                message = message.replace(value, "[REDACTED]")
        for pattern in _SAFE_ERROR_PATTERNS:
            message = pattern.sub("[REDACTED]", message)
        return message

    @staticmethod
    def _assert_action_text_allowed(text: str) -> None:
        if _PROHIBITED_TARGET_PATTERN.search(text):
            raise ValueError("Interações com pagamentos e mensagens são bloqueadas")

    @staticmethod
    def _safe_target(target: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in target.items():
            if isinstance(value, str) and re.search(
                r"password|token|cookie|csrf|auth|secret|credential|session",
                value,
                re.IGNORECASE,
            ):
                safe[key] = "[REDACTED]"
            elif key == "data_attributes" and isinstance(value, dict):
                safe[key] = {
                    name: "[REDACTED]"
                    if re.search(
                        r"password|token|cookie|csrf|auth|secret|credential|session",
                        name,
                        re.IGNORECASE,
                    ) or re.search(
                        r"password|token|cookie|csrf|auth|secret|credential|session",
                        item,
                        re.IGNORECASE,
                    )
                    else item
                    for name, item in value.items()
                }
            else:
                safe[key] = value
        return safe


class BrowserOperatorExecutor:
    def __init__(
        self,
        controller_factory: Callable[[], V2BrowserController] = V2BrowserController,
        state_store: V2StateStore | None = None,
    ) -> None:
        self._controller_factory = controller_factory
        self._lock = threading.RLock()
        self._results: dict[str, dict[str, Any]] = {}
        self._memory: dict[str, dict[str, Any]] = {}
        self._execution_lock = threading.Lock()
        self.state_store = state_store or V2StateStore()
        for task_id, record in self.state_store.load_operator_tasks().items():
            result = record.get("result")
            if not isinstance(result, dict):
                result = {
                    "task_id": task_id,
                    "status": record.get("status", "failed"),
                    "action": record.get("action"),
                    "result": record.get("results"),
                    "evidence": record.get("evidence", []),
                    "operations": record.get("operations", []),
                    "progress": record.get("progress", {}),
                }
            if record.get("status") in {"queued", "running"} and not self._process_is_alive(
                record.get("worker_pid")
            ):
                result = {
                    **result,
                    "status": "failed",
                    "operations": deepcopy(record.get("operations", [])),
                    "evidence": deepcopy(record.get("evidence", [])),
                    "started_at": record.get("started_at"),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": {
                        "code": "task_interrupted",
                        "message": "Tarefa interrompida; não foi repetida automaticamente",
                    },
                }
                if record.get("research") is not None:
                    result["research"] = deepcopy(record["research"])
                    action_results = [
                        item.get("result") for item in result["operations"]
                        if item.get("status") == "completed"
                    ]
                    result["result"] = action_results
                    self._finalize_research(result, result["research"])
                package_task = {
                    "task_id": task_id,
                    "goal": record.get("goal", ""),
                    "actions": record.get("actions", []),
                    **({"research": record["research"]} if record.get("research") is not None else {}),
                }
                package = build_operator_result_package(package_task, result)
                self.state_store.save_result_package(task_id, package)
                result["result_package"] = package
                result["data"] = {"package_id": package["package_id"]}
                record.update({
                    "status": "failed",
                    "result": result,
                    "result_package": package,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                self.state_store.save_operator_task(task_id, record)
            self._results[task_id] = deepcopy(result)

    def execute(self, task: Any) -> dict[str, Any]:
        try:
            validated = BrowserOperator.validate_task(task)
        except ValueError as error:
            return {
                "task_id": None,
                "status": "failed",
                "action": None,
                "result": None,
                "evidence": [],
                "error": {"code": "invalid_task", "message": str(error)},
            }
        task_id = validated["task_id"]
        with self._execution_lock, _browser_execution_lock(V2_CDP_ENDPOINT):
            with self._lock:
                now = datetime.now(timezone.utc).isoformat()
                record = {
                    "task_id": task_id,
                    "type": "BROWSER_OPERATOR",
                    "goal": validated.get("goal", ""),
                    "research": deepcopy(validated.get("research")),
                    "status": "queued",
                    "actions": deepcopy(validated.get("actions", [])),
                    "operations": [],
                    "evidence": [],
                    "progress": {"completed_actions": 0, "total_actions": len(validated.get("actions", []))},
                    "result": None,
                    "worker_pid": os.getpid(),
                    "created_at": now,
                    "started_at": None,
                    "finished_at": None,
                }
                if task_id in self._results or not self.state_store.create_operator_task(
                    task_id, record
                ):
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "action": None,
                        "result": None,
                        "evidence": [],
                        "error": {
                            "code": "duplicate_task",
                            "message": "task_id já foi executado",
                        },
                    }
                memory = validated.get("memory")
                if memory and "reuse" in memory:
                    memory_name = memory["reuse"]
                    remembered = self._memory.get(memory_name)
                    if remembered is None:
                        result = {
                            "task_id": task_id,
                            "status": "failed",
                            "action": None,
                            "result": None,
                            "evidence": [],
                            "operations": [],
                            "error": {
                                "code": "memory_not_found",
                                "message": f"Não existe procedimento operacional salvo: {memory_name}",
                            },
                        }
                        result["started_at"] = record["created_at"]
                        result["finished_at"] = datetime.now(timezone.utc).isoformat()
                        result["result_package"] = build_operator_result_package(
                            {**validated, "actions": []}, result,
                        )
                        result["data"] = {"package_id": result["result_package"]["package_id"]}
                        self.state_store.save_result_package(task_id, result["result_package"])
                        record.update({
                            "status": "failed",
                            "result": deepcopy(result),
                            "finished_at": result["finished_at"],
                        })
                        self.state_store.save_operator_task(task_id, record)
                        self._results[task_id] = deepcopy(result)
                        return result
                    actions = deepcopy(remembered["actions"])
                else:
                    actions = deepcopy(validated["actions"])
                    record["actions"] = deepcopy(actions)
                record.update({
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                })
                self.state_store.save_operator_task(task_id, record)

            def persist_action(action_record: dict[str, Any]) -> None:
                record["operations"].append(deepcopy(action_record))
                if isinstance(action_record.get("evidence"), dict):
                    record["evidence"].append(deepcopy(action_record["evidence"]))
                record["action"] = {
                    "index": action_record.get("index"),
                    "name": action_record.get("action"),
                }
                record["progress"] = {
                    "completed_actions": sum(
                        item.get("status") == "completed" for item in record["operations"]
                    ),
                    "total_actions": len(actions),
                }
                self.state_store.save_operator_task(task_id, record)

            try:
                result = BrowserOperator(
                    self._controller_factory(),
                    on_action=persist_action,
                ).execute({
                    "task_id": task_id,
                    "goal": validated.get("goal", ""),
                    "actions": actions,
                })
            except Exception as error:
                result = {
                    "task_id": task_id,
                    "status": "failed",
                    "action": record.get("action"),
                    "result": [],
                    "evidence": deepcopy(record.get("evidence", [])),
                    "operations": deepcopy(record.get("operations", [])),
                    "error": {
                        "code": "operation_failed",
                        "message": BrowserOperator._safe_error(error, {}),
                    },
                }
            if validated.get("research") is not None:
                self._finalize_research(result, validated["research"])
            if (
                result.get("status") == "completed"
                and memory
                and "save_as" in memory
            ):
                memory_name = memory["save_as"]
                with self._lock:
                    self._memory[memory_name] = {
                        "actions": deepcopy(actions),
                        "result": deepcopy(result),
                    }
                result["memory"] = {"saved_as": memory_name}
            elif result.get("status") == "completed" and memory and "reuse" in memory:
                result["memory"] = {"reused": memory["reuse"]}
            result["started_at"] = record["started_at"]
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            package_task = {**validated, "actions": deepcopy(actions)}
            result["result_package"] = build_operator_result_package(package_task, result)
            result["data"] = {"package_id": result["result_package"]["package_id"]}
            self.state_store.save_result_package(task_id, result["result_package"])
            with self._lock:
                self._results[task_id] = deepcopy(result)
                record.update({
                    "status": result.get("status", "failed"),
                    "result": deepcopy(result),
                    "action": deepcopy(result.get("action")),
                    "operations": deepcopy(result.get("operations", [])),
                    "evidence": deepcopy(result.get("evidence", [])),
                    "progress": {
                        "completed_actions": sum(
                            item.get("status") == "completed"
                            for item in result.get("operations", [])
                        ),
                        "total_actions": len(actions),
                    },
                    "started_at": record.get("started_at") or record["created_at"],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                result["progress"] = deepcopy(record["progress"])
                record["result"] = deepcopy(result)
                self._results[task_id] = deepcopy(result)
                self.state_store.save_operator_task(task_id, record)
        return result

    def get_result(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            persisted = self.state_store.get_operator_task(task_id)
            result = persisted.get("result") if isinstance(persisted, dict) else None
            if not isinstance(result, dict):
                result = self._results.get(task_id)
            return deepcopy(result) if result is not None else None

    def _finalize_research(self, result: dict[str, Any], research: dict[str, Any]) -> None:
        action_results = result.get("result", [])
        records: list[dict[str, Any]] = []
        for item in action_results if isinstance(action_results, list) else []:
            if isinstance(item, dict) and isinstance(item.get("records"), list):
                records = [record for record in item["records"] if isinstance(record, dict)]
                break
        leads: list[V2Lead] = []
        excluded: list[dict[str, Any]] = []
        warnings: list[str] = []
        for record in records:
            source_url = record.get("source_url")
            raw_links = record.get("links", [])
            links = [
                {"title": item.get("text", ""), "url": item.get("url")}
                for item in raw_links if isinstance(item, dict) and isinstance(item.get("url"), str)
            ] if isinstance(raw_links, list) else []
            source_host = urlsplit(source_url).hostname if isinstance(source_url, str) else None
            company_link = next((
                item["url"] for item in links
                if source_host and urlsplit(item["url"]).hostname == source_host
            ), None)
            lead = extract_lead({
                "title": record.get("title"),
                "text": record.get("text", ""),
                "url": company_link,
                "links": links,
            }, category=research.get("niche") or None)
            lead.source = source_host or "browser"
            lead.source_url = source_url if isinstance(source_url, str) else None
            lead.company_url = company_link
            criteria = evaluate_lead_criteria(
                " ".join((lead.company_name, record.get("text", ""))),
                research["inclusion_criteria"],
                research["exclusion_criteria"],
            )
            if criteria["status"] == "excluded":
                excluded.append(lead.to_dict())
            else:
                leads.append(lead)
                if criteria["criteria_unverified"]:
                    warnings.append(
                        f"Critérios de inclusão sem evidência em: {lead.company_name or 'resultado sem nome'}"
                    )
        known_leads = [
            V2Lead.from_dict(item) for item in self.state_store.load_leads()
        ]
        known_ids = {lead.lead_id for lead in known_leads}
        deduplicated = deduplicate_leads(known_leads + leads)
        duplicate_count = max(0, len(known_leads) + len(leads) - len(deduplicated))
        self.state_store.replace_leads([lead.to_dict() for lead in deduplicated])
        selected = [
            lead for lead in deduplicated if lead.lead_id not in known_ids
        ][:research["quantity"]]
        duplicate_records = []
        for lead in deduplicated:
            for duplicate in lead.raw_data.get("duplicates", []):
                if isinstance(duplicate, dict):
                    search_result = duplicate.get("search_result", {})
                    if not isinstance(search_result, dict):
                        search_result = {}
                    duplicate_records.append({
                        "company_name": search_result.get("title"),
                        "source_url": search_result.get("source_url"),
                        "deduplicated_to": lead.lead_id,
                    })
        if excluded:
            warnings.append(f"{len(excluded)} resultado(s) separados por critérios de exclusão.")
        if duplicate_count:
            warnings.append(f"{duplicate_count} duplicata(s) foram eliminadas.")
        if len(selected) < research["quantity"]:
            result["status"] = "partial"
        result["leads"] = [lead.to_dict() for lead in selected]
        result["research"] = {
            **research,
            "found_count": len(selected),
            "excluded_results": excluded,
            "duplicates_skipped": duplicate_count,
            "duplicates": duplicate_records,
            "unique_total": len(deduplicated),
            "warnings": warnings,
        }
        if warnings:
            result.setdefault("warnings", []).extend(warnings)

    @staticmethod
    def _process_is_alive(process_id: Any) -> bool:
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id < 1:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
