import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

logger = logging.getLogger("scout.browser")

V1_CDP_ENDPOINT = "http://127.0.0.1:9222"
V2_CDP_ENDPOINT = "http://127.0.0.1:9223"
_ALLOWED_CDP_PORTS = {9222, 9223}
INSPECT_PAGE_SCRIPT = """() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const safeValue = element => {
    const descriptor = [
      element.name,
      element.id,
      element.getAttribute('autocomplete'),
      element.getAttribute('aria-label'),
      element.getAttribute('placeholder'),
      Array.from(element.labels || []).map(label => label.innerText || label.textContent || '').join(' ')
    ].filter(Boolean).join(' ').toLowerCase();
    const sensitive = /password|token|cookie|csrf|auth|secret|credential|session/;
    if (sensitive.test(descriptor)) return null;
    return 'value' in element ? redact(element.value) : null;
  };
  const redact = value => (value || '').replace(
    /Bearer\\s+[a-z0-9._~+/=-]+|\\beyJ[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\b/gi,
    '[REDACTED]'
  ).slice(0, 500);
  const dataAttributes = element => Object.fromEntries(
    Array.from(element.attributes)
      .filter(attribute => attribute.name.toLowerCase().startsWith('data-') &&
        !/password|token|cookie|csrf|auth|secret|credential|session/i.test(attribute.name))
      .map(attribute => [attribute.name, redact(attribute.value)])
  );
  const root = document.querySelector('main,[role="main"]') || document.body;
  const text = (root && root.innerText || '').trim();
  const safeHref = href => {
    try {
      const url = new URL(href, document.baseURI);
      if (url.username || url.password) return '';
      url.username = '';
      url.password = '';
      url.search = '';
      url.hash = '';
      return url.href;
    } catch {
      return '';
    }
  };
  return {
    title: document.title,
    url: (() => {
      const url = new URL(window.location.href);
      url.search = '';
      url.hash = '';
      return url.href;
    })(),
    text: text.slice(0, 12000),
    inputs: Array.from(document.querySelectorAll('input,textarea,select'))
      .filter(element => visible(element) &&
        !(element instanceof HTMLInputElement && element.type.toLowerCase() === 'password') &&
        !(element instanceof HTMLInputElement && element.type.toLowerCase() === 'hidden'))
      .map(element => ({
        tag: element.tagName.toLowerCase(),
        type: element instanceof HTMLInputElement ? element.type : element.tagName.toLowerCase(),
        name: element.getAttribute('name') || '',
        id: element.id || '',
        role: element.getAttribute('role') || '',
        placeholder: element.getAttribute('placeholder') || '',
        aria_label: element.getAttribute('aria-label') || '',
        autocomplete: element.getAttribute('autocomplete') || '',
        labels: Array.from(element.labels || [])
          .map(label => redact(label.innerText || label.textContent || '').trim())
          .filter(Boolean),
        data_attributes: dataAttributes(element),
        value: safeValue(element)
      })),
    buttons: Array.from(document.querySelectorAll('button,[role="button"]'))
      .filter(visible)
      .map(element => ({
        text: (element.innerText || element.textContent || '').trim(),
        aria_label: element.getAttribute('aria-label') || '',
        type: element.getAttribute('type') || '',
        role: element.getAttribute('role') || '',
        name: element.getAttribute('name') || '',
        id: element.id || '',
        disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
        data_attributes: dataAttributes(element)
      })),
    links: Array.from(document.querySelectorAll('a[href]'))
      .filter(visible)
      .map(element => ({
        text: (element.innerText || element.textContent || '').trim(),
        href: safeHref(element.href)
      }))
  };
}"""
INSPECT_CATEGORY_DOM_SCRIPT = """() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const sensitiveName = /password|token|cookie|csrf|auth|secret|credential|session/i;
  const safeText = value => (value || '').replace(
    /Bearer\\s+[a-z0-9._~+/=-]+|\\beyJ[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\b/gi,
    '[REDACTED]'
  ).slice(0, 180);
  const safeAttributes = element => {
    const data = {};
    const aria = {};
    for (const attribute of element.attributes) {
      const name = attribute.name.toLowerCase();
      if (sensitiveName.test(name)) continue;
      const value = safeText(attribute.value);
      if (name.startsWith('data-')) data[name] = value;
      if (name.startsWith('aria-')) aria[name] = value;
    }
    return {data_attributes: data, aria_attributes: aria};
  };
  const inputInfo = element => {
    if (element instanceof HTMLInputElement &&
        ['password', 'hidden'].includes(element.type.toLowerCase())) return null;
    const labels = Array.from(element.labels || [])
      .map(label => safeText(label.innerText || label.textContent || '').trim())
      .filter(Boolean);
    return {
      tag: element.tagName.toLowerCase(),
      type: element instanceof HTMLInputElement ? element.type : element.tagName.toLowerCase(),
      name: element.getAttribute('name') || '',
      id: element.id || '',
      placeholder: element.getAttribute('placeholder') || '',
      aria_label: element.getAttribute('aria-label') || '',
      role: element.getAttribute('role') || '',
      autocomplete: element.getAttribute('autocomplete') || '',
      labels,
      class: element.className && typeof element.className === 'string' ? element.className : '',
      ...safeAttributes(element)
    };
  };
  const elements = Array.from(document.body.querySelectorAll('*')).filter(element => {
    if (!visible(element)) return false;
    if (element instanceof HTMLInputElement && ['password', 'hidden'].includes(element.type.toLowerCase())) {
      return false;
    }
    const text = (element.innerText || element.textContent || '').trim();
    const hasRelevantAttribute = Array.from(element.attributes).some(attribute => {
      const name = attribute.name.toLowerCase();
      return name === 'role' || name.startsWith('aria-') || name.startsWith('data-');
    });
    const semanticTag = /^(button|option|li|select)$/.test(element.tagName.toLowerCase());
    const compactText = text.length > 0 && text.length <= 120 &&
      element.querySelectorAll('*').length <= 4;
    return hasRelevantAttribute || semanticTag || compactText;
  }).slice(0, 800);
  const records = elements.map(element => {
    const text = safeText(element.innerText || element.textContent || '').trim();
    const attributes = safeAttributes(element);
    const signature = JSON.stringify([
      element.tagName.toLowerCase(),
      element.getAttribute('role') || '',
      element.id || '',
      element.getAttribute('aria-label') || '',
      text,
      attributes.data_attributes
    ]);
    return {
      signature,
      tag: element.tagName.toLowerCase(),
      text,
      role: element.getAttribute('role') || '',
      aria_label: element.getAttribute('aria-label') || '',
      class: element.className && typeof element.className === 'string' ? element.className : '',
      ...attributes
    };
  });
  const categoryButton = records.find(item =>
    item.tag === 'button' && item.text.toLocaleLowerCase() === 'escolha o ramo'
  ) || null;
  return {
    title: document.title,
    url: (() => {
      const url = new URL(window.location.href);
      url.search = '';
      url.hash = '';
      return url.href;
    })(),
    inputs: Array.from(document.querySelectorAll('input,textarea,select'))
      .filter(element => visible(element))
      .map(inputInfo)
      .filter(Boolean),
    category_button: categoryButton,
    elements: records
  };
}"""
SEARCH_RESULT_STATE_SCRIPT = """() => {
    const visible = element => {
        const rect = element.getBoundingClientRect();
        const style = window.getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 &&
            style.visibility !== 'hidden' && style.display !== 'none';
    };
    const records = Array.from(document.querySelectorAll(
        'article,[role="article"],[role="listitem"]'
    )).filter(visible).map(element =>
        (element.innerText || element.textContent || '').trim()
    ).filter(Boolean);
    const text = (document.querySelector('main,[role="main"]') || document.body)
        ?.innerText || '';
    const noResults = records.length === 0 &&
        /nenhum(?:a)? resultado|sem resultados|não encontramos resultados|nao encontramos resultados|no results|nothing found/i.test(text);
    const loading = Boolean(document.querySelector(
        '[aria-busy="true"],[role="progressbar"],[data-loading="true"]'
    )) || /carregando|loading\\.\\.\\.|buscando resultados/i.test(text);
    return {records, no_results: noResults, loading};
}"""

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
    re.compile(
        r"""(?i)(?:access[_ -]?token|refresh[_ -]?token|csrf(?:[_ -]?token)?|"""
        r"""authorization|cookie|password|secret|api[_ -]?key)\s*[:=]\s*"""
        r"""["']?[^\s,;"'}]+"""
    ),
)
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"password|token|cookie|csrf|auth|secret|credential|session",
    re.IGNORECASE,
)
_PROTECTED_ACTION_PATTERN = re.compile(
    r"payment|pay\b|checkout|billing|transaction|pagamento|pagar|cobran[çc]a|"
    r"whats\s*app|whatsapp|wa\.me",
    re.IGNORECASE,
)


def _validate_http_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and parts.port != 0
        )
    except ValueError as error:
        raise ValueError("URL inválida; use uma URL HTTP(S) sem credenciais") from error
    if not valid:
        raise ValueError("URL inválida; use uma URL HTTP(S) sem credenciais")


class BrowserController:
    def __init__(self, cdp_endpoint: str | None = None) -> None:
        self.cdp_endpoint = (
            cdp_endpoint
            if cdp_endpoint is not None
            else os.getenv("SCOUT_CDP_ENDPOINT", V1_CDP_ENDPOINT)
        )
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_version: str | None = None
        self._open_dropdown: Locator | None = None
        self._open_dropdown_kind: str | None = None
        self._dropdown_initial_state: dict[str, str | int | None] | None = None
        self._active_page_override: Page | None = None

    def connect(self, timeout_ms: int = 5_000) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms deve ser um inteiro positivo")
        if self._playwright is not None:
            stale_playwright = self._playwright
            self._browser = None
            self._playwright = None
            self._browser_version = None
            stale_playwright.stop()
        if not self.cdp_endpoint:
            raise RuntimeError("Configure SCOUT_CDP_ENDPOINT com o endpoint CDP do Edge")
        try:
            parts = urlsplit(self.cdp_endpoint)
            valid_endpoint = (
                parts.scheme == "http"
                and parts.hostname == "127.0.0.1"
                and parts.username is None
                and parts.password is None
                and not parts.query
                and not parts.fragment
                and parts.port in _ALLOWED_CDP_PORTS
            )
        except ValueError as error:
            raise ValueError(
                "SCOUT_CDP_ENDPOINT deve ser http://127.0.0.1:9222 (V1) "
                "ou http://127.0.0.1:9223 (V2)"
            ) from error
        if not valid_endpoint:
            raise ValueError(
                "SCOUT_CDP_ENDPOINT deve ser http://127.0.0.1:9222 (V1) "
                "ou http://127.0.0.1:9223 (V2)"
            )

        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(self.cdp_endpoint, timeout=timeout_ms)
            session = browser.new_browser_cdp_session()
            try:
                version_info = session.send("Browser.getVersion")
                product = version_info["product"]
            finally:
                session.detach()
            if not isinstance(product, str):
                raise RuntimeError("O Edge não informou uma versão de produto válida")
            if "edg/" not in product.casefold() and "microsoft edge" not in product.casefold():
                raise RuntimeError(f"O endpoint CDP não pertence ao Microsoft Edge ({product})")
        except Exception:
            playwright.stop()
            raise

        self._playwright = playwright
        self._browser = browser
        self._browser_version = product

    def disconnect(self) -> None:
        self._browser = None
        self._browser_version = None
        self._active_page_override = None
        playwright = self._playwright
        self._playwright = None
        if playwright is not None:
            playwright.stop()

    def _require_browser(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            raise RuntimeError("Browser Controller não está conectado ao Edge")
        return self._browser

    def _pages(self) -> list[Page]:
        browser = self._require_browser()
        return [page for context in browser.contexts for page in context.pages if not page.is_closed()]

    @staticmethod
    def _tab_info(page: Page) -> dict[str, str]:
        return {
            "title": BrowserController._sanitize_records(page.title()),
            "url": BrowserController._safe_page_url(page.url),
        }

    def listTabs(self) -> list[dict[str, str]]:
        return [self._tab_info(page) for page in self._pages()]

    def getActiveTab(self) -> dict[str, str] | None:
        page = self.getActivePage()
        return self._tab_info(page) if page is not None else None

    def getActivePage(self) -> Page | None:
        if self._active_page_override is not None:
            if not self._active_page_override.is_closed():
                return self._active_page_override
            self._active_page_override = None
        for page in self._pages():
            if page.evaluate("document.visibilityState === 'visible' && document.hasFocus()"):
                return page
        return None

    def getStatus(self) -> dict:
        browser = self._require_browser()
        tabs = self.listTabs()
        active_tab = self.getActiveTab()
        return {
            "connected": browser.is_connected(),
            "browser": "Microsoft Edge",
            "browser_version": self._browser_version,
            "cdp_endpoint": self.cdp_endpoint,
            "tabs": tabs,
            "activeTab": active_tab,
        }

    def navigate(self, url: str, timeout_ms: int = 30_000) -> dict[str, str]:
        _validate_http_url(url)
        self._validate_timeout(timeout_ms)
        logger.info("Navegação solicitada para host=%s", urlsplit(url).hostname)
        active_page = next(
            (page for page in self._pages()
             if page.evaluate("document.visibilityState === 'visible' && document.hasFocus()")),
            None,
        )
        if active_page is None:
            raise RuntimeError("Não há uma aba ativa disponível para navegar")
        active_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return self._tab_info(active_page)

    def readPageInfo(self) -> dict[str, str] | None:
        page = self._active_page()
        return self._tab_info(page) if page is not None else None

    def inspectPage(self) -> dict:
        browser = self._require_browser()
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível para inspecionar")
        inspection = page.evaluate(INSPECT_PAGE_SCRIPT)
        if not isinstance(inspection, dict):
            raise RuntimeError("A inspeção da página retornou um formato inválido")
        url = inspection.get("url")
        if isinstance(url, str):
            inspection["url"] = self._safe_page_url(url)
        text = inspection.get("text")
        if isinstance(text, str):
            for pattern in _SENSITIVE_TEXT_PATTERNS:
                text = pattern.sub("[REDACTED]", text)
            inspection["text"] = text[:12_000]
        inputs = inspection.get("inputs")
        if isinstance(inputs, list):
            safe_inputs = []
            for item in inputs:
                if not isinstance(item, dict):
                    continue
                field_type = item.get("type", "")
                if isinstance(field_type, str) and field_type.casefold() in {"password", "hidden"}:
                    continue
                descriptor = " ".join(
                    str(item.get(key, ""))
                    for key in ("name", "id", "autocomplete", "placeholder", "aria_label")
                )
                labels = item.get("labels")
                if isinstance(labels, list):
                    descriptor += " " + " ".join(str(label) for label in labels)
                safe_value = None if _SENSITIVE_FIELD_PATTERN.search(descriptor) else item.get("value")
                sanitized = self._sanitize_records(item)
                if not isinstance(sanitized, dict):
                    continue
                safe_inputs.append({
                    key: sanitized.get(key)
                    for key in (
                        "tag", "type", "name", "id", "role", "placeholder",
                        "aria_label", "autocomplete", "labels", "data_attributes",
                    )
                } | {"value": safe_value})
            inspection["inputs"] = safe_inputs
        inspection = self._sanitize_records(inspection)
        links = inspection.get("links")
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and isinstance(link.get("href"), str):
                    link["href"] = self._safe_page_url(link["href"])
        inspection["connected"] = browser.is_connected()
        return inspection

    def inspectCategoryDropdown(self) -> dict:
        browser = self._require_browser()
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível para inspecionar")
        hostname = urlsplit(page.url or "").hostname or ""
        if "aivio" not in hostname.casefold() and not re.search(r"\baivio\b", page.title(), re.I):
            raise RuntimeError("A aba ativa não foi reconhecida como AIVIO")

        before = page.evaluate(INSPECT_CATEGORY_DOM_SCRIPT)
        if not isinstance(before, dict):
            raise RuntimeError("A inspeção inicial do dropdown retornou um formato inválido")
        category_button = page.get_by_role(
            "button",
            name=re.compile(r"^\s*Escolha o ramo\s*$", re.I),
        )
        button_count = category_button.count()
        if button_count != 1:
            raise RuntimeError(
                f"Era esperado exatamente um botão 'Escolha o ramo'; encontrados: {button_count}"
            )
        category_button.click()
        page.wait_for_timeout(300)
        after = page.evaluate(INSPECT_CATEGORY_DOM_SCRIPT)
        if not isinstance(after, dict):
            raise RuntimeError("A inspeção do dropdown aberto retornou um formato inválido")

        before_counts: dict[str, int] = {}
        for item in before.get("elements", []):
            if isinstance(item, dict) and isinstance(item.get("signature"), str):
                signature = item["signature"]
                before_counts[signature] = before_counts.get(signature, 0) + 1

        new_elements = []
        after_counts: dict[str, int] = {}
        for item in after.get("elements", []):
            if not isinstance(item, dict) or not isinstance(item.get("signature"), str):
                continue
            signature = item["signature"]
            after_counts[signature] = after_counts.get(signature, 0) + 1
            if after_counts[signature] > before_counts.get(signature, 0):
                new_elements.append({key: value for key, value in item.items() if key != "signature"})

        after_url = after.get("url")
        if isinstance(after_url, str):
            parts = urlsplit(after_url)
            after["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        before_category_button = before.get("category_button")
        if isinstance(before_category_button, dict):
            before_category_button = {
                key: value for key, value in before_category_button.items() if key != "signature"
            }

        return {
            "connected": browser.is_connected(),
            "title": after.get("title", ""),
            "url": after.get("url", ""),
            "inputs_before_open": before.get("inputs", []),
            "category_button": before_category_button,
            "dropdown_opened": True,
            "new_elements": new_elements[:200],
            "element_limit_reached": len(new_elements) > 200,
        }

    @staticmethod
    def _css_attribute_selector(attribute: str, value: str) -> str:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\a ")
            .replace("\r", "\\d ")
            .replace("\f", "\\c ")
        )
        return f'[{attribute}="{escaped}"]'

    @staticmethod
    def _validate_timeout(timeout_ms: int) -> None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms deve ser um inteiro positivo")

    @staticmethod
    def _stable_data_selector(tags: tuple[str, ...], attributes: dict[str, str]) -> str:
        if not isinstance(attributes, dict) or not attributes or not all(
            isinstance(attribute, str)
            and attribute.startswith("data-")
            and isinstance(value, str)
            for attribute, value in attributes.items()
        ):
            raise ValueError("data_attributes aceita atributos data-* de texto")
        filters = "".join(
            BrowserController._css_attribute_selector(attribute, value)
            for attribute, value in attributes.items()
        )
        return ",".join(f"{tag}{filters}" for tag in tags)

    def _find_target(
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
    ) -> Locator:
        return self._resolve_target(
            "elemento",
            self._target_strategies(
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
            ),
        )

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
    ) -> list[tuple[str, Locator]]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        if tag_name is not None and not re.fullmatch(r"[a-z][a-z0-9-]*", tag_name):
            raise ValueError("tag_name deve ser um nome de tag HTML válido")
        strategies: list[tuple[str, Locator]] = []
        if role and accessible_name:
            strategies.append((
                "role+nome acessível",
                page.get_by_role(cast(Any, role), name=accessible_name, exact=True),
            ))
        if text:
            strategies.append(("texto exato", page.get_by_text(text, exact=True)))
        if aria_label:
            prefix = f"{tag_name}" if tag_name else "*"
            strategies.append((
                "aria-label",
                page.locator(
                    f"{prefix}{self._css_attribute_selector('aria-label', aria_label)}"
                ),
            ))
        if label:
            strategies.append(("label", page.get_by_label(label, exact=True)))
        if placeholder:
            strategies.append((
                "placeholder",
                page.get_by_placeholder(placeholder, exact=True),
            ))
        if name:
            prefix = f"{tag_name}" if tag_name else "*"
            strategies.append((
                "name",
                page.locator(f"{prefix}{self._css_attribute_selector('name', name)}"),
            ))
        if element_id:
            prefix = f"{tag_name}" if tag_name else "*"
            strategies.append((
                "id",
                page.locator(f"{prefix}{self._css_attribute_selector('id', element_id)}"),
            ))
        if data_attributes:
            prefix = f"{tag_name}" if tag_name else "*"
            selector = self._stable_data_selector((prefix,), data_attributes)
            strategies.append(("atributos data-*", page.locator(selector)))
        if not strategies:
            raise ValueError("Informe critérios explícitos para identificar o elemento")
        return strategies

    @staticmethod
    def _unique_visible(locator: Locator, description: str) -> Locator | None:
        visible = [
            locator.nth(index)
            for index in range(locator.count())
            if locator.nth(index).is_visible()
        ]
        if len(visible) > 1:
            raise ValueError(f"Alvo ambíguo para {description}: {len(visible)} elementos visíveis")
        return visible[0] if visible else None

    def _resolve_target(
        self,
        description: str,
        strategies: list[tuple[str, Locator]],
    ) -> Locator:
        for _, locator in strategies:
            match = self._unique_visible(locator, description)
            if match is not None:
                return match
        raise RuntimeError(f"Elemento não encontrado: {description}")

    @staticmethod
    def _snapshot_page(page: Page) -> dict[str, str]:
        return page.evaluate(
            """() => ({
              url: window.location.href,
              text_hash: (() => {
                const text = [
                  (document.body && document.body.innerText || '').trim(),
                  ...Array.from(document.querySelectorAll('a[href]'))
                    .filter(link => {
                      const rect = link.getBoundingClientRect();
                      const style = window.getComputedStyle(link);
                      return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                    })
                    .map(link => link.href)
                ].join('\\n');
                let hash = 2166136261;
                for (let index = 0; index < text.length; index += 1) {
                  hash ^= text.charCodeAt(index);
                  hash = Math.imul(hash, 16777619);
                }
                return `${text.length}:${hash >>> 0}`;
              })()
            })"""
        )

    def wait_for_stable_search_results(
        self,
        before: dict[str, str],
        timeout_ms: int = 20_000,
        stable_ms: int = 750,
    ) -> dict[str, Any]:
        self._validate_timeout(timeout_ms)
        if isinstance(stable_ms, bool) or not isinstance(stable_ms, int) or stable_ms < 1:
            raise ValueError("stable_ms deve ser um inteiro positivo")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        deadline = time.monotonic() + timeout_ms / 1_000
        previous_signature: str | None = None
        stable_since: float | None = None
        loading_seen = False
        while True:
            state = page.evaluate(SEARCH_RESULT_STATE_SCRIPT)
            if not isinstance(state, dict) or not isinstance(state.get("records"), list):
                raise RuntimeError("O estado dos resultados da página é inválido")
            loading_seen = loading_seen or state.get("loading") is True
            records = [item for item in state["records"] if isinstance(item, str) and item.strip()]
            no_results = state.get("no_results") is True and not records
            signature = json.dumps(
                {"records": records, "no_results": no_results},
                ensure_ascii=False,
                sort_keys=True,
            )
            fresh = signature != before.get("results_hash") or loading_seen
            if (records or no_results) and not state.get("loading") and fresh:
                now = time.monotonic()
                if signature != previous_signature:
                    previous_signature = signature
                    stable_since = now
                elif stable_since is not None and (now - stable_since) * 1_000 >= stable_ms:
                    return {
                        "record_count": len(records),
                        "no_results": no_results,
                        "signature": signature,
                    }
            else:
                previous_signature = None
                stable_since = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaywrightTimeoutError(
                    f"Resultados reais e estáveis não apareceram em {timeout_ms} ms"
                )
            page.wait_for_timeout(min(100, max(1, int(remaining * 1_000))))

    def capture_page_state(self) -> dict[str, str]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        return self._snapshot_page(page)

    def capture_search_results_state(self) -> dict[str, str]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        state = page.evaluate(SEARCH_RESULT_STATE_SCRIPT)
        if not isinstance(state, dict) or not isinstance(state.get("records"), list):
            raise RuntimeError("O estado dos resultados da página é inválido")
        records = [item for item in state["records"] if isinstance(item, str) and item.strip()]
        no_results = state.get("no_results") is True and not records
        return {
            "url": self._safe_page_url(page.url),
            "results_hash": json.dumps(
                {"records": records, "no_results": no_results},
                ensure_ascii=False,
                sort_keys=True,
            ),
        }

    def verify_page_changed(self, before: dict[str, str], timeout_ms: int = 1_000) -> bool:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ValueError("timeout_ms deve ser um inteiro positivo")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        try:
            page.wait_for_function(
                """before => window.location.href !== before.url ||
                  (() => {
                    const text = [
                      (document.body && document.body.innerText || '').trim(),
                      ...Array.from(document.querySelectorAll('a[href]'))
                        .filter(link => {
                          const rect = link.getBoundingClientRect();
                          const style = window.getComputedStyle(link);
                          return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                        })
                        .map(link => link.href)
                    ].join('\\n');
                    let hash = 2166136261;
                    for (let index = 0; index < text.length; index += 1) {
                      hash ^= text.charCodeAt(index);
                      hash = Math.imul(hash, 16777619);
                    }
                    return `${text.length}:${hash >>> 0}` !== before.text_hash;
                  })()""",
                arg=before,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            return False
        return True

    def _click_explicit_target(
        self,
        locator: Locator,
        timeout_ms: int,
    ) -> dict[str, bool]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        descriptor = locator.evaluate(
            """element => [
              element.innerText || element.textContent || '',
              element.getAttribute('aria-label') || '',
              element.getAttribute('href') || '',
              element.getAttribute('name') || '',
              element.id || ''
            ].join(' ')"""
        )
        self._reject_protected_action(descriptor)
        before = self._snapshot_page(page)
        locator.click(timeout=timeout_ms)
        return {
            "clicked": True,
            "page_changed": self.verify_page_changed(before, min(timeout_ms, 1_000)),
        }

    @staticmethod
    def _reject_protected_action(*descriptors: str | None) -> None:
        target = " ".join(value for value in descriptors if value)
        if _PROTECTED_ACTION_PATTERN.search(target):
            raise ValueError("Interações com pagamentos e WhatsApp não são permitidas")

    def _guard_target_action(self, locator: Locator) -> None:
        descriptor = locator.evaluate(
            """element => [
              element.innerText || element.textContent || '',
              element.getAttribute('aria-label') || '',
              element.getAttribute('placeholder') || '',
              element.getAttribute('name') || '',
              element.getAttribute('href') || '',
              element.id || '',
              Array.from(element.labels || [])
                .map(label => label.innerText || label.textContent || '').join(' ')
            ].join(' ')"""
        )
        self._reject_protected_action(descriptor)

    @staticmethod
    def _is_sensitive_field(locator: Locator) -> bool:
        descriptors = " ".join(
            str(locator.get_attribute(attribute) or "")
            for attribute in ("type", "name", "id", "autocomplete", "aria-label", "placeholder")
        )
        labels = locator.evaluate(
            """element => Array.from(element.labels || [])
              .map(label => label.innerText || label.textContent || '').join(' ')"""
        )
        return bool(_SENSITIVE_FIELD_PATTERN.search(f"{descriptors} {labels}"))

    def click_button(
        self,
        *,
        text: str | None = None,
        aria_label: str | None = None,
        name: str | None = None,
        element_id: str | None = None,
        data_attributes: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        self._reject_protected_action(text, aria_label, name, element_id)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        accessible_name = text or aria_label
        strategies: list[tuple[str, Locator]] = []
        if accessible_name:
            strategies.append((
                "role+nome acessível",
                page.get_by_role("button", name=accessible_name, exact=True),
            ))
        if aria_label:
            strategies.append((
                "aria-label",
                page.locator(
                    f'button{self._css_attribute_selector("aria-label", aria_label)},'
                    f'[role="button"]{self._css_attribute_selector("aria-label", aria_label)}'
                ),
            ))
        if name:
            strategies.append((
                "name",
                page.locator(
                    f'button{self._css_attribute_selector("name", name)},'
                    f'[role="button"]{self._css_attribute_selector("name", name)}'
                ),
            ))
        if element_id:
            strategies.append((
                "id",
                page.locator(
                    f'button{self._css_attribute_selector("id", element_id)},'
                    f'[role="button"]{self._css_attribute_selector("id", element_id)}'
                ),
            ))
        if data_attributes:
            strategies.append((
                "atributos data-*",
                page.locator(self._stable_data_selector(
                    ("button", '[role="button"]'),
                    data_attributes,
                )),
            ))
        if not strategies:
            raise ValueError("Informe text, aria_label, name ou element_id para identificar o botão")
        target = self._resolve_target("botão", strategies)
        return self._click_explicit_target(target, timeout_ms)

    def click_link(
        self,
        *,
        text: str | None = None,
        aria_label: str | None = None,
        href: str | None = None,
        name: str | None = None,
        element_id: str | None = None,
        data_attributes: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        self._reject_protected_action(text, aria_label, href, name, element_id)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        accessible_name = text or aria_label
        strategies: list[tuple[str, Locator]] = []
        if accessible_name:
            strategies.append((
                "role+nome acessível",
                page.get_by_role("link", name=accessible_name, exact=True),
            ))
        if aria_label:
            strategies.append((
                "aria-label",
                page.locator(f'a[href]{self._css_attribute_selector("aria-label", aria_label)}'),
            ))
        if href:
            parsed = urlsplit(href)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("href deve ser um link HTTP(S) absoluto")
            strategies.append((
                "href",
                page.locator(f'a[href]{self._css_attribute_selector("href", href)}'),
            ))
        if name:
            strategies.append((
                "name",
                page.locator(f'a[href]{self._css_attribute_selector("name", name)}'),
            ))
        if element_id:
            strategies.append((
                "id",
                page.locator(f'a[href]{self._css_attribute_selector("id", element_id)}'),
            ))
        if data_attributes:
            strategies.append((
                "atributos data-*",
                page.locator(self._stable_data_selector(("a[href]",), data_attributes)),
            ))
        if not strategies:
            raise ValueError("Informe text, aria_label, href, name ou element_id para identificar o link")
        target = self._resolve_target("link", strategies)
        return self._click_explicit_target(target, timeout_ms)

    def click_text(self, text: str, *, timeout_ms: int = 5_000) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text deve ser um texto não vazio")
        self._reject_protected_action(text)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        target = self._resolve_target(
            "texto visível",
            [("texto exato", page.get_by_text(text, exact=True))],
        )
        return self._click_explicit_target(target, timeout_ms)

    def fill_input(
        self,
        value: str,
        *,
        label: str | None = None,
        placeholder: str | None = None,
        aria_label: str | None = None,
        name: str | None = None,
        element_id: str | None = None,
        data_attributes: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        accessible_name = aria_label or label
        strategies: list[tuple[str, Locator]] = []
        if accessible_name:
            strategies.append((
                "role+nome acessível",
                page.get_by_role("textbox", name=accessible_name, exact=True),
            ))
        if aria_label:
            strategies.append((
                "aria-label",
                page.locator(
                    f'input:not([type="password"]):not([type="hidden"])'
                    f'{self._css_attribute_selector("aria-label", aria_label)},'
                    f'textarea{self._css_attribute_selector("aria-label", aria_label)},'
                    f'select{self._css_attribute_selector("aria-label", aria_label)}'
                ),
            ))
        if label:
            strategies.append(("label", page.get_by_label(label, exact=True)))
        if placeholder:
            strategies.append((
                "placeholder",
                page.get_by_placeholder(placeholder, exact=True),
            ))
        if name:
            strategies.append((
                "name",
                page.locator(
                    f'input:not([type="password"]):not([type="hidden"])'
                    f'{self._css_attribute_selector("name", name)},'
                    f'textarea{self._css_attribute_selector("name", name)},'
                    f'select{self._css_attribute_selector("name", name)}'
                ),
            ))
        if element_id:
            strategies.append((
                "id",
                page.locator(
                    f'input:not([type="password"]):not([type="hidden"])'
                    f'{self._css_attribute_selector("id", element_id)},'
                    f'textarea{self._css_attribute_selector("id", element_id)},'
                    f'select{self._css_attribute_selector("id", element_id)}'
                ),
            ))
        if data_attributes:
            strategies.append((
                "atributos data-*",
                page.locator(self._stable_data_selector(
                    ('input:not([type="password"]):not([type="hidden"])', "textarea", "select"),
                    data_attributes,
                )),
            ))
        if not strategies:
            raise ValueError("Informe label, placeholder, aria_label, name ou element_id para identificar o input")
        target = self._resolve_target("input", strategies)
        if self._is_sensitive_field(target):
            raise ValueError("Preenchimento de campos sensíveis está bloqueado")
        self._guard_target_action(target)
        before_value = target.input_value()
        target.fill(value, timeout=timeout_ms)
        after_value = target.input_value()
        if after_value != value:
            raise RuntimeError("O valor preenchido não foi confirmado pelo controle")
        return {"filled": True, "value_changed": before_value != after_value}

    def open_dropdown(
        self,
        trigger: str | None = None,
        *,
        role: str | None = None,
        aria_label: str | None = None,
        label: str | None = None,
        placeholder: str | None = None,
        name: str | None = None,
        element_id: str | None = None,
        data_attributes: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> dict[str, str]:
        self._validate_timeout(timeout_ms)
        self._reject_protected_action(trigger, aria_label, label, placeholder, name, element_id)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        strategies: list[tuple[str, Locator]] = []
        if data_attributes:
            strategies.append((
                "atributos data-*",
                page.locator(self._stable_data_selector(
                    ("button", '[role="button"]', "select", "[role='combobox']"),
                    data_attributes,
                )),
            ))
        if role:
            strategies.append((
                "role",
                page.get_by_role(cast(Any, role), name=trigger, exact=True)
                if trigger
                else page.get_by_role(cast(Any, role)),
            ))
        if trigger:
            strategies.extend((
                ("role combobox+nome", page.get_by_role("combobox", name=trigger, exact=True)),
                ("role button+nome", page.get_by_role("button", name=trigger, exact=True)),
                ("texto exato", page.get_by_text(trigger, exact=True)),
            ))
        if aria_label:
            strategies.append((
                "aria-label",
                page.locator(self._css_attribute_selector("aria-label", aria_label)),
            ))
        if label:
            strategies.append(("label", page.get_by_label(label, exact=True)))
        if placeholder:
            strategies.append((
                "placeholder",
                page.get_by_placeholder(placeholder, exact=True),
            ))
        if name:
            strategies.append(("name", page.locator(self._css_attribute_selector("name", name))))
        if element_id:
            strategies.append(("id", page.locator(self._css_attribute_selector("id", element_id))))
        if not strategies:
            raise ValueError("Forneça um role, nome ou atributo estável para identificar o dropdown")
        target = self._resolve_target("dropdown trigger", strategies)
        self._guard_target_action(target)
        tag_name = str(target.evaluate("(element) => element.tagName.toLowerCase()"))
        if tag_name == "select":
            target.click(timeout=timeout_ms)
            self._open_dropdown = target
            self._open_dropdown_kind = "native"
            return {"opened": True, "kind": "native"}
        self._open_dropdown = target
        self._open_dropdown_kind = "custom"
        self._dropdown_initial_state = target.evaluate(
            """element => ({
              expanded: element.getAttribute('aria-expanded'),
              state: element.getAttribute('data-state'),
              controls: element.getAttribute('aria-controls'),
              owns: element.getAttribute('aria-owns'),
              childCount: element.childElementCount,
              text: (element.innerText || element.textContent || '').trim()
            })"""
        )
        try:
            initial_menu_state = self._read_dropdown_menu()
            if initial_menu_state.get("ambiguous"):
                raise ValueError("Mais de um menu de opções está visível antes do clique")
            if initial_menu_state.get("opened"):
                return {"opened": True, "kind": "custom"}
        except (TypeError, ValueError):
            self._open_dropdown = None
            self._open_dropdown_kind = None
            self._dropdown_initial_state = None
            raise
        target.click(timeout=timeout_ms)
        return {"opened": True, "kind": "custom"}

    def _read_dropdown_menu(self, *, should_scroll: bool = False) -> dict[str, Any]:
        if self._open_dropdown is None or self._open_dropdown_kind != "custom":
            raise RuntimeError("Abra explicitamente um dropdown customizado antes de inspecioná-lo")
        return self._open_dropdown.evaluate(
            """(trigger, args) => {
              const {shouldScroll, initialState} = args;
              const visible = element => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return rect.width > 0 && rect.height > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              };
              const controlled = (trigger.getAttribute('aria-controls') || '')
                .concat(' ', trigger.getAttribute('aria-owns') || '')
                .split(/\\s+/)
                .filter(Boolean)
                .map(id => document.getElementById(id))
                .filter(element => element && visible(element));
              const listboxes = Array.from(document.querySelectorAll(
                '[role="listbox"],[role="menu"]'
              ))
                .filter(element => visible(element));
              const contents = Array.from(document.querySelectorAll(
                '[data-slot="select-content"]'
              )).filter(element => visible(element));
              const menus = controlled.length
                ? controlled
                : listboxes.length ? listboxes : contents;
              const candidates = menus.filter(menu =>
                menu.matches('[role="listbox"],[role="menu"],[data-slot="select-content"]') ||
                menu.querySelector('[role="option"],[role="menuitem"],[data-slot="select-item"]')
              );
              if (candidates.length > 1) {
                return {opened: true, ambiguous: true, options: [], evidence: 'ambiguous-menus'};
              }
              const menu = candidates[0];
              const expanded = trigger.getAttribute('aria-expanded');
              const triggerState = trigger.getAttribute('data-state');
              const triggerNow = {
                expanded,
                state: triggerState,
                controls: trigger.getAttribute('aria-controls'),
                owns: trigger.getAttribute('aria-owns'),
                childCount: trigger.childElementCount,
                text: (trigger.innerText || trigger.textContent || '').trim()
              };
              const triggerChanged = Boolean(initialState) && Object.keys(triggerNow)
                .some(key => triggerNow[key] !== initialState[key]);
              const isVisibleOption = element => {
                if (!visible(element)) return false;
                const rect = element.getBoundingClientRect();
                if (rect.bottom <= 0 || rect.top >= window.innerHeight ||
                    rect.right <= 0 || rect.left >= window.innerWidth) return false;
                for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                  const style = window.getComputedStyle(parent);
                  if (/auto|scroll|overlay|hidden|clip/.test(
                    `${style.overflowY} ${style.overflowX}`
                  )) {
                    const bounds = parent.getBoundingClientRect();
                    if (rect.bottom <= bounds.top || rect.top >= bounds.bottom ||
                        rect.right <= bounds.left || rect.left >= bounds.right) return false;
                  }
                  if (parent === menu) break;
                }
                return true;
              };
              const optionSelector = '[role="option"],[role="menuitem"],[data-slot="select-item"]';
              const optionScope = menu || document;
              const optionElements = Array.from(optionScope.querySelectorAll(optionSelector))
                .filter(isVisibleOption);
              const options = optionElements.map(option =>
                (option.innerText || option.textContent || '').trim()
              ).filter(Boolean);
              const stateOpen = expanded === 'true' ||
                ['open', 'opened', 'expanded'].includes((triggerState || '').toLowerCase());
              const opened = Boolean(menu || options.length || stateOpen || triggerChanged);
              const evidence = menu ? 'visible-menu'
                : options.length ? 'visible-options'
                : stateOpen ? 'combobox-expanded'
                : triggerChanged ? 'combobox-dom-changed' : 'none';
              if (!opened) {
                return {
                  opened: false,
                  ambiguous: false,
                  options: [],
                  evidence,
                  expanded,
                  state: triggerState,
                  trigger_changed: triggerChanged
                };
              }
              if (!menu && !options.length && shouldScroll) {
                return {
                  opened: true,
                  ambiguous: false,
                  options,
                  evidence,
                  expanded,
                  state: triggerState,
                  trigger_changed: triggerChanged,
                  scrollable: false
                };
              }
              const scrollable = element => {
                const style = window.getComputedStyle(element);
                return element.scrollHeight > element.clientHeight + 1 &&
                  /auto|scroll|overlay|hidden/.test(style.overflowY);
              };
              const scrollRoot = menu || optionElements[0]?.parentElement;
              const descendants = scrollRoot ? Array.from(scrollRoot.querySelectorAll('*')).reverse() : [];
              const nestedScroller = descendants.find(scrollable);
              let scroller = nestedScroller || (scrollRoot && scrollable(scrollRoot) ? scrollRoot : null);
              if (!scroller) {
                for (let parent = (menu || optionElements[0])?.parentElement;
                  parent && parent !== document.body && parent !== document.documentElement;
                  parent = parent.parentElement
                ) {
                  if (scrollable(parent)) {
                    scroller = parent;
                    break;
                  }
                }
              }
              if (shouldScroll) {
                if (!scroller) {
                  return {
                    opened: true,
                    ambiguous: false,
                    options,
                    evidence,
                    expanded,
                    state: triggerState,
                    trigger_changed: triggerChanged,
                    scrollable: false
                  };
                }
                const before = scroller.scrollTop;
                const maxScroll = scroller.scrollHeight - scroller.clientHeight;
                const after = Math.min(
                  maxScroll,
                  before + Math.max(1, scroller.clientHeight * 0.8)
                );
                scroller.scrollTop = after;
                return {
                  opened: true,
                  ambiguous: false,
                  options,
                  evidence,
                  scrollable: true,
                  scrolled: scroller.scrollTop > before,
                  at_end: scroller.scrollTop >= maxScroll - 1,
                  scroll_top: scroller.scrollTop,
                  scroll_height: scroller.scrollHeight
                };
              }
              return {
                opened: true,
                ambiguous: false,
                options,
                evidence,
                expanded,
                state: triggerState,
                trigger_changed: triggerChanged,
                scrollable: Boolean(scroller),
                scrollTop: scroller ? scroller.scrollTop : null,
                scrollHeight: scroller ? scroller.scrollHeight : null,
                clientHeight: scroller ? scroller.clientHeight : null
              };
            }""",
            cast(Any, {"shouldScroll": should_scroll, "initialState": self._dropdown_initial_state}),
        )

    def wait_for_dropdown_open(self, *, timeout_ms: int = 5_000) -> bool:
        self._validate_timeout(timeout_ms)
        if self._open_dropdown is None:
            raise RuntimeError("Abra explicitamente um dropdown antes de aguardar sua abertura")
        if self._open_dropdown_kind == "native":
            return True
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            state = self._read_dropdown_menu()
            if state.get("ambiguous"):
                raise ValueError("Mais de um menu de opções está visível")
            if state.get("opened"):
                return True
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError(
                    "O dropdown não abriu após o clique: "
                    f"aria-expanded={state.get('expanded')!r}, "
                    f"data-state={state.get('state')!r}, "
                    f"evidência={state.get('evidence')!r}, "
                    f"opções visíveis={len(state.get('options', []))}"
                )
            page.wait_for_timeout(min(50, remaining_ms))

    def read_dropdown_options(self) -> list[str]:
        state = self._read_dropdown_menu()
        if state.get("ambiguous"):
            raise ValueError("Mais de um menu de opções está visível")
        if not state.get("opened"):
            raise RuntimeError("O dropdown está fechado")
        options = state.get("options")
        if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
            raise RuntimeError("As opções do dropdown retornaram um formato inválido")
        return options

    def scroll_dropdown(self) -> dict[str, int | bool]:
        state = self._read_dropdown_menu(should_scroll=True)
        if state.get("ambiguous"):
            raise ValueError("Mais de um menu de opções está visível")
        if not state.get("opened"):
            raise RuntimeError("O dropdown está fechado")
        if not state.get("scrollable"):
            raise RuntimeError("O dropdown não possui um container rolável")
        if not all(
            isinstance(state.get(key), expected_type)
            for key, expected_type in (
                ("scrolled", bool),
                ("at_end", bool),
                ("scroll_top", (int, float)),
                ("scroll_height", (int, float)),
            )
        ):
            raise RuntimeError("A rolagem do dropdown retornou um formato inválido")
        return {
            "scrolled": state["scrolled"],
            "at_end": state["at_end"],
            "scroll_top": int(state["scroll_top"]),
            "scroll_height": int(state["scroll_height"]),
        }

    def select_option(self, text: str, *, timeout_ms: int = 5_000) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        self._reject_protected_action(text)
        if self._open_dropdown is None or self._open_dropdown_kind is None:
            raise RuntimeError("Abra explicitamente um dropdown antes de selecionar uma opção")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text deve ser um texto não vazio")
        dropdown = self._open_dropdown
        if self._open_dropdown_kind == "native":
            options = dropdown.evaluate(
                """element => Array.from(element.options, option => ({
                  text: (option.label || option.textContent || '').trim(),
                  value: option.value
                }))"""
            )
            matches = [option for option in options if option["text"] == text]
            if not matches:
                raise RuntimeError(f"Opção inexistente: {text}")
            if len(matches) > 1:
                raise ValueError(f"Opção ambígua: {text}")
            previous_value = dropdown.input_value()
            dropdown.select_option(value=matches[0]["value"], timeout=timeout_ms)
            selected = dropdown.evaluate(
                """element => {
                  const option = element.selectedOptions[0];
                  return option ? (option.label || option.textContent || '').trim() : '';
                }"""
            )
            if selected != text:
                raise RuntimeError(f"A seleção da opção não foi confirmada: {text}")
            self._open_dropdown = None
            self._open_dropdown_kind = None
            return {"selected": True, "value_changed": dropdown.input_value() != previous_value}

        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        before_trigger = dropdown.evaluate(
            """element => ({
              text: (element.innerText || element.textContent || '').trim(),
              expanded: element.getAttribute('aria-expanded'),
              value: 'value' in element ? element.value : null,
              valueText: element.getAttribute('aria-valuetext')
            })"""
        )
        option_strategies = [
            ("role option+nome", page.get_by_role("option", name=text, exact=True)),
            ("texto exato", page.get_by_text(text, exact=True)),
        ]
        expected = " ".join(text.split()).casefold()
        max_scrolls = 40
        for scroll_count in range(max_scrolls + 1):
            options = self.read_dropdown_options()
            matches = [
                option for option in options
                if " ".join(option.split()).casefold() == expected
            ]
            if len(matches) > 1:
                raise ValueError(f"Opção ambígua no dropdown: {text}")
            if matches:
                break
            if scroll_count == max_scrolls:
                raise RuntimeError(f"Opção não encontrada após {max_scrolls} rolagens: {text}")
            scroll_result = self.scroll_dropdown()
            if scroll_result["at_end"] and not scroll_result["scrolled"]:
                raise RuntimeError(f"Opção não encontrada até o fim do dropdown: {text}")
            if not scroll_result["scrolled"]:
                raise RuntimeError("O dropdown não avançou durante a rolagem")
            page.wait_for_timeout(min(100, timeout_ms))
        option = self._resolve_target(f"opção {text!r}", option_strategies)
        option.click(timeout=timeout_ms)
        after_trigger = dropdown.evaluate(
            """element => ({
              text: (element.innerText || element.textContent || '').trim(),
              expanded: element.getAttribute('aria-expanded'),
              value: 'value' in element ? element.value : null,
              valueText: element.getAttribute('aria-valuetext')
            })"""
        )
        selected_values = (
            after_trigger.get("text"),
            after_trigger.get("value"),
            after_trigger.get("valueText"),
        )
        if not any(
            isinstance(value, str) and " ".join(value.split()).casefold() == expected
            for value in selected_values
        ):
            raise RuntimeError(f"A seleção da opção não foi confirmada no controle: {text}")
        trigger_changed = before_trigger != after_trigger
        self._open_dropdown = None
        self._open_dropdown_kind = None
        self._dropdown_initial_state = None
        return {"selected": True, "value_changed": trigger_changed}

    def read_dropdown_state(self) -> dict[str, str | None]:
        if self._open_dropdown is None or self._open_dropdown_kind is None:
            raise RuntimeError("Abra explicitamente um dropdown antes de ler seu estado")
        state = self._open_dropdown.evaluate(
            """element => ({
              text: (element.innerText || element.textContent || '').trim(),
              expanded: element.getAttribute('aria-expanded'),
              value: 'value' in element ? element.value : null,
              valueText: element.getAttribute('aria-valuetext')
            })"""
        )
        return state

    def wait_for_element(
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
        timeout_ms: int = 5_000,
    ) -> bool:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        strategies: list[tuple[str, Locator]] = []
        if role and accessible_name:
            strategies.append((
                "role+nome acessível",
                page.get_by_role(cast(Any, role), name=accessible_name, exact=True),
            ))
        if text:
            strategies.append(("texto exato", page.get_by_text(text, exact=True)))
        if aria_label:
            strategies.append((
                "aria-label",
                page.locator(self._css_attribute_selector("aria-label", aria_label)),
            ))
        if label:
            strategies.append(("label", page.get_by_label(label, exact=True)))
        if placeholder:
            strategies.append((
                "placeholder",
                page.get_by_placeholder(placeholder, exact=True),
            ))
        if name:
            strategies.append(("name", page.locator(self._css_attribute_selector("name", name))))
        if element_id:
            strategies.append(("id", page.locator(self._css_attribute_selector("id", element_id))))
        if data_attributes:
            strategies.append((
                "atributos data-*",
                page.locator(self._stable_data_selector(("*",), data_attributes)),
            ))
        if not strategies:
            raise ValueError("Forneça critérios explícitos para localizar o elemento")

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

    def read_visible_text(self, max_chars: int = 12_000) -> str:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
            raise ValueError("max_chars deve ser um inteiro não negativo")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        text = page.evaluate(
            "() => (document.body && document.body.innerText || '').trim()"
        )
        if not isinstance(text, str):
            raise RuntimeError("A leitura do texto visível retornou um formato inválido")
        for pattern in _SENSITIVE_TEXT_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text[:max_chars]

    def find_text(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text deve ser um texto não vazio")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        locator = page.get_by_text(text, exact=True)
        match = self._unique_visible(locator, "texto exato")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        return {
            "found": match is not None,
            "text": self._sanitize_records(text),
            "source_url": self._safe_page_url(page.url),
            "source_title": self._sanitize_records(page.title()),
        }

    def click_element(self, *, timeout_ms: int = 5_000, **target: Any) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(**target)
        return self._click_explicit_target(locator, timeout_ms)

    def double_click(
        self,
        *,
        timeout_ms: int = 5_000,
        **target: Any,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(**target)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        descriptor = locator.evaluate(
            """element => [
              element.innerText || element.textContent || '',
              element.getAttribute('aria-label') || '',
              element.getAttribute('href') || '',
              element.getAttribute('name') || '',
              element.id || ''
            ].join(' ')"""
        )
        self._reject_protected_action(descriptor)
        before = self._snapshot_page(page)
        locator.dblclick(timeout=timeout_ms)
        return {
            "clicked": True,
            "page_changed": self.verify_page_changed(before, min(timeout_ms, 1_000)),
        }

    def clear_input(self, *, timeout_ms: int = 5_000, **target: Any) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(**target)
        if locator.evaluate("(element) => element.tagName.toLowerCase()") not in {"input", "textarea"}:
            raise ValueError("O alvo identificado não é um campo de texto")
        if self._is_sensitive_field(locator):
            raise ValueError("Limpeza de campos sensíveis está bloqueada")
        self._guard_target_action(locator)
        before = locator.input_value()
        locator.fill("", timeout=timeout_ms)
        if locator.input_value() != "":
            raise RuntimeError("A limpeza do campo não foi confirmada")
        return {"cleared": True, "value_changed": before != ""}

    def set_checkbox(
        self,
        checked: bool = True,
        *,
        timeout_ms: int = 5_000,
        **target: Any,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        if not isinstance(checked, bool):
            raise ValueError("checked deve ser booleano")
        locator = self._find_target(role="checkbox", **target)
        self._guard_target_action(locator)
        previous = locator.is_checked()
        if checked:
            locator.check(timeout=timeout_ms)
        else:
            locator.uncheck(timeout=timeout_ms)
        if locator.is_checked() is not checked:
            raise RuntimeError("O estado do checkbox não foi confirmado")
        return {"checked": checked, "state_changed": previous is not checked}

    def set_radio(self, *, timeout_ms: int = 5_000, **target: Any) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(role="radio", **target)
        self._guard_target_action(locator)
        previous = locator.is_checked()
        locator.check(timeout=timeout_ms)
        if not locator.is_checked():
            raise RuntimeError("O estado do radio não foi confirmado")
        return {"selected": True, "state_changed": not previous}

    def select_native_option(
        self,
        option_text: str,
        *,
        timeout_ms: int = 5_000,
        **target: Any,
    ) -> dict[str, bool | str]:
        self._validate_timeout(timeout_ms)
        if not isinstance(option_text, str) or not option_text.strip():
            raise ValueError("option_text deve ser um texto não vazio")
        locator = self._find_target(role="combobox", **target)
        self._guard_target_action(locator)
        tag_name = locator.evaluate("(element) => element.tagName.toLowerCase()")
        if tag_name != "select":
            raise ValueError("O alvo identificado não é um select nativo")
        options = locator.evaluate(
            """element => Array.from(element.options, option => ({
              text: (option.label || option.textContent || '').trim(),
              value: option.value
            }))"""
        )
        matches = [option for option in options if option["text"] == option_text]
        if not matches:
            raise RuntimeError(f"Opção inexistente: {option_text}")
        if len(matches) > 1:
            raise ValueError(f"Opção ambígua: {option_text}")
        previous = locator.input_value()
        locator.select_option(value=matches[0]["value"], timeout=timeout_ms)
        selected = locator.evaluate(
            """element => {
              const option = element.selectedOptions[0];
              return option ? (option.label || option.textContent || '').trim() : '';
            }"""
        )
        if selected != option_text:
            raise RuntimeError(f"A seleção não foi confirmada: {option_text}")
        return {
            "selected": True,
            "value": selected,
            "state_changed": locator.input_value() != previous,
        }

    def element_exists(self, **target: Any) -> bool:
        strategies = self._target_strategies(**target)
        return any(locator.count() > 0 for _, locator in strategies)

    def element_visible(self, **target: Any) -> bool:
        try:
            self._find_target(**target)
        except RuntimeError as error:
            if str(error).startswith("Elemento não encontrado:"):
                return False
            raise
        return True

    def element_enabled(self, **target: Any) -> bool:
        return self._find_target(**target).is_enabled()

    def element_selected(self, **target: Any) -> bool:
        locator = self._find_target(**target)
        tag_name = locator.evaluate("(element) => element.tagName.toLowerCase()")
        aria_selected = locator.get_attribute("aria-selected")
        if aria_selected is not None:
            return aria_selected.casefold() == "true"
        aria_checked = locator.get_attribute("aria-checked")
        if aria_checked is not None:
            return aria_checked.casefold() == "true"
        if tag_name == "option":
            return bool(locator.evaluate("(element) => element.selected"))
        if tag_name in {"input"}:
            input_type = (locator.get_attribute("type") or "").casefold()
            if input_type in {"checkbox", "radio"}:
                return locator.is_checked()
        if tag_name == "select":
            return bool(locator.evaluate("(element) => element.selectedOptions.length > 0"))
        raise ValueError("O elemento não oferece estado selecionado")

    def element_filled(self, **target: Any) -> bool:
        locator = self._find_target(**target)
        return bool(locator.input_value())

    def dropdown_open(self, **target: Any) -> bool | None:
        locator = self._find_target(**target)
        expanded = locator.get_attribute("aria-expanded")
        if expanded is None:
            return None
        return expanded.casefold() == "true"

    def page_changed(self, before: dict[str, str], timeout_ms: int = 1_000) -> bool:
        return self.verify_page_changed(before, timeout_ms)

    def url_changed(self, before_url: str) -> bool:
        if not isinstance(before_url, str):
            raise ValueError("before_url deve ser texto")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        return page.url != before_url

    def wait_for_text(self, text: str, timeout_ms: int = 5_000) -> bool:
        self._validate_timeout(timeout_ms)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text deve ser um texto não vazio")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.get_by_text(text, exact=True).first.wait_for(
            state="visible",
            timeout=timeout_ms,
        )
        return True

    def wait_for_url(self, expected_url: str, timeout_ms: int = 10_000) -> str:
        self._validate_timeout(timeout_ms)
        if not isinstance(expected_url, str) or not expected_url.strip():
            raise ValueError("expected_url deve ser texto não vazio")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.wait_for_url(expected_url, timeout=timeout_ms)
        return page.url

    def wait_for_page_change(
        self,
        before: dict[str, str],
        timeout_ms: int = 5_000,
    ) -> bool:
        self._validate_timeout(timeout_ms)
        if not self.verify_page_changed(before, timeout_ms):
            raise PlaywrightTimeoutError(f"A página não mudou em {timeout_ms} ms")
        return True

    def wait_for_stable_content(
        self,
        before: dict[str, str],
        timeout_ms: int = 20_000,
        stable_ms: int = 750,
    ) -> dict[str, Any]:
        self._validate_timeout(timeout_ms)
        if isinstance(stable_ms, bool) or not isinstance(stable_ms, int) or stable_ms < 1:
            raise ValueError("stable_ms deve ser um inteiro positivo")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        deadline = time.monotonic() + timeout_ms / 1_000
        previous_signature: tuple[str, str] | None = None
        stable_since: float | None = None
        while True:
            snapshot = self._snapshot_page(page)
            state = page.evaluate(
                """() => {
                  const root = document.querySelector('main,[role="main"]') || document.body;
                  const text = (root && root.innerText || '').trim();
                  const loading = Boolean(document.querySelector(
                    '[aria-busy="true"],[role="progressbar"],[data-loading="true"]'
                  )) || /carregando|loading\\.\\.\\.|buscando resultados/i.test(text);
                  return {text_length: text.length, loading};
                }"""
            )
            if not isinstance(state, dict):
                raise RuntimeError("O estado de estabilidade do DOM é inválido")
            signature = (snapshot.get("url", ""), snapshot.get("text_hash", ""))
            fresh = snapshot.get("text_hash") != before.get("text_hash")
            if fresh and state.get("text_length", 0) > 0 and not state.get("loading"):
                now = time.monotonic()
                if signature != previous_signature:
                    previous_signature = signature
                    stable_since = now
                elif stable_since is not None and (now - stable_since) * 1_000 >= stable_ms:
                    return {
                        "url": self._safe_page_url(snapshot["url"]),
                        "text_hash": snapshot["text_hash"],
                        "text_length": state["text_length"],
                        "stable_ms": stable_ms,
                    }
            else:
                previous_signature = None
                stable_since = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlaywrightTimeoutError(
                    f"Conteúdo novo e estável não apareceu em {timeout_ms} ms"
                )
            page.wait_for_timeout(min(100, max(1, int(remaining * 1_000))))

    def press_key(self, key: str) -> None:
        normalized = key.strip().casefold() if isinstance(key, str) else ""
        allowed = {
            "enter": "Enter",
            "escape": "Escape",
            "tab": "Tab",
            "backspace": "Backspace",
        }
        if normalized not in allowed:
            raise ValueError("Tecla não permitida por esta primitiva")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.keyboard.press(allowed[normalized])

    def keyboard_shortcut(self, shortcut: str) -> None:
        aliases = {
            "ctrl+a": "Control+A",
            "control+a": "Control+A",
            "ctrl+c": "Control+C",
            "control+c": "Control+C",
            "ctrl+v": "Control+V",
            "control+v": "Control+V",
        }
        normalized = shortcut.strip().casefold() if isinstance(shortcut, str) else ""
        if normalized not in aliases:
            raise ValueError("Atalho não permitido por esta primitiva")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.keyboard.press(aliases[normalized])

    def select_text(self, *, timeout_ms: int = 5_000, **target: Any) -> bool:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(**target)
        locator.select_text(timeout=timeout_ms)
        return True

    def copy_text(self, *, timeout_ms: int = 5_000, **target: Any) -> str:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        locator = self._find_target(**target)
        if self._is_sensitive_field(locator):
            raise ValueError("Cópia de campos sensíveis está bloqueada")
        self._guard_target_action(locator)
        self.select_text(timeout_ms=timeout_ms, **target)
        page.keyboard.press("Control+C")
        copied = page.evaluate("() => navigator.clipboard.readText()")
        if not isinstance(copied, str):
            raise RuntimeError("O clipboard não retornou texto")
        for pattern in _SENSITIVE_TEXT_PATTERNS:
            copied = pattern.sub("[REDACTED]", copied)
        return copied

    def paste_text(
        self,
        *,
        text: str | None = None,
        timeout_ms: int = 5_000,
        **target: Any,
    ) -> dict[str, bool]:
        self._validate_timeout(timeout_ms)
        locator = self._find_target(**target)
        if locator.evaluate("(element) => element.tagName.toLowerCase()") not in {"input", "textarea"}:
            raise ValueError("O alvo identificado não é um campo de texto")
        if self._is_sensitive_field(locator):
            raise ValueError("Colagem em campos sensíveis está bloqueada")
        self._guard_target_action(locator)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        before = locator.input_value()
        locator.focus()
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("text deve ser texto")
            page.evaluate("(value) => navigator.clipboard.writeText(value)", text)
        page.keyboard.press("Control+V")
        if locator.input_value() == before:
            raise RuntimeError("A colagem não alterou o campo de destino")
        return {"pasted": True, "value_changed": True}

    def back(self, timeout_ms: int = 30_000) -> dict[str, str] | None:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
        return self._tab_info(page)

    def forward(self, timeout_ms: int = 30_000) -> dict[str, str] | None:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.go_forward(wait_until="domcontentloaded", timeout=timeout_ms)
        return self._tab_info(page)

    def reload(self, timeout_ms: int = 30_000) -> dict[str, str]:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        return self._tab_info(page)

    def open_link(self, *, timeout_ms: int = 5_000, **target: Any) -> dict[str, bool]:
        return self.click_link(timeout_ms=timeout_ms, **target)

    def open_new_tab(self, url: str, timeout_ms: int = 30_000) -> dict[str, str]:
        _validate_http_url(url)
        self._validate_timeout(timeout_ms)
        browser = self._require_browser()
        if not browser.contexts:
            raise RuntimeError("Nenhum contexto de navegador disponível")
        page = browser.contexts[0].new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            page.close()
            raise
        page.bring_to_front()
        self._active_page_override = page
        return self._tab_info(page)

    def switch_tab(self, *, title: str | None = None, url: str | None = None) -> dict[str, str]:
        if (title is None) == (url is None):
            raise ValueError("Informe exatamente um dos critérios title ou url")
        pages = self._pages()
        matches = [
            page for page in pages
            if (title is not None and page.title() == title)
            or (url is not None and page.url == url)
        ]
        if not matches:
            raise RuntimeError("Aba não encontrada")
        if len(matches) > 1:
            raise ValueError("Aba ambígua; informe um título ou URL único")
        page = matches[0]
        page.bring_to_front()
        self._active_page_override = page
        return self._tab_info(page)

    def close_tab(self, *, title: str | None = None, url: str | None = None) -> None:
        if (title is None) != (url is None):
            raise ValueError("title e url devem ser informados juntos ou omitidos")
        pages = self._pages()
        if title is None and url is None:
            page = self.getActivePage()
            if page is None:
                raise RuntimeError("Não há uma aba ativa disponível")
        else:
            matches = [item for item in pages if item.title() == title and item.url == url]
            if not matches:
                raise RuntimeError("Aba não encontrada")
            if len(matches) > 1:
                raise ValueError("Aba ambígua")
            page = matches[0]
        if len(pages) <= 1:
            raise ValueError("A última aba do Edge não pode ser fechada pelo Scout")
        page.close()
        self._active_page_override = None

    def list_tabs(self) -> list[dict[str, str]]:
        return self.listTabs()

    def active_tab(self) -> dict[str, str] | None:
        return self.getActiveTab()

    def scroll_by(self, delta_x: int = 0, delta_y: int = 0) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (delta_x, delta_y)):
            raise ValueError("delta_x e delta_y devem ser inteiros")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.mouse.wheel(delta_x, delta_y)

    def scroll_to_element(self, **target: Any) -> bool:
        locator = self._find_target(**target)
        locator.scroll_into_view_if_needed()
        return locator.is_visible()

    def scroll_top(self) -> None:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")

    def scroll_bottom(self) -> None:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        page.evaluate(
            "() => window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'})"
        )

    def read_attributes(self, attributes: list[str], **target: Any) -> dict[str, str | None]:
        if not isinstance(attributes, list) or not attributes:
            raise ValueError("Informe uma lista não vazia de atributos")
        if any(
            not isinstance(attribute, str)
            or not re.fullmatch(r"(?:aria-[a-z-]+|data-[a-z0-9_-]+|name|id|type|role|href|title|placeholder|value|autocomplete)",
                               attribute)
            for attribute in attributes
        ):
            raise ValueError("Atributo inválido para leitura")
        locator = self._find_target(**target)
        values: dict[str, str | None] = {}
        sensitive_field = self._is_sensitive_field(locator)
        for attribute in attributes:
            if _SENSITIVE_FIELD_PATTERN.search(attribute):
                values[attribute] = None
                continue
            value = locator.get_attribute(attribute)
            if attribute == "value" and sensitive_field:
                values[attribute] = None
            elif attribute == "href" and value is not None:
                parts = urlsplit(value)
                if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                    values[attribute] = None
                else:
                    values[attribute] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            elif value is not None:
                for pattern in _SENSITIVE_TEXT_PATTERNS:
                    value = pattern.sub("[REDACTED]", value)
                values[attribute] = value
            else:
                values[attribute] = None
        return values

    def find_element(self, **target: Any) -> dict[str, Any]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        source_url = self._safe_page_url(page.url)
        try:
            locator = self._find_target(**target)
        except RuntimeError as error:
            if str(error).startswith("Elemento não encontrado:"):
                return {"found": False, "source_url": source_url}
            raise
        details = locator.evaluate(
            """element => ({
              tag: element.tagName.toLowerCase(),
              role: element.getAttribute('role') || '',
              text: (element.innerText || element.textContent || '').trim(),
              aria_label: element.getAttribute('aria-label') || '',
              name: element.getAttribute('name') || '',
              id: element.id || '',
              type: element.getAttribute('type') || ''
            })"""
        )
        return {
            "found": True,
            "source_url": source_url,
            "source_title": self._sanitize_records(page.title()),
            "element": self._sanitize_records(details),
        }

    @staticmethod
    def _safe_page_url(url: str) -> str:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            return ""
        if parts.scheme in {"http", "https"} and parts.hostname is None:
            return ""
        if parts.scheme not in {"http", "https", "chrome", "edge", "about"}:
            return ""
        path_parts = parts.path.split("/")
        for index, segment in enumerate(path_parts):
            if re.search(r"password|token|auth|session|credential|secret", segment, re.I):
                path_parts = path_parts[:index + 1] + ["[REDACTED]"]
                break
        safe_path = "/".join(path_parts)
        for pattern in _SENSITIVE_TEXT_PATTERNS:
            safe_path = pattern.sub("[REDACTED]", safe_path)
        return urlunsplit((parts.scheme, parts.netloc, safe_path, "", ""))

    def read_links(self) -> list[dict[str, str]]:
        return self.inspectPage().get("links", [])

    def read_buttons(self) -> list[dict[str, str]]:
        return self.inspectPage().get("buttons", [])

    def read_inputs(self) -> list[dict[str, Any]]:
        return self.inspectPage().get("inputs", [])

    def read_table(self, **target: Any) -> dict[str, Any]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        locator = self._find_target(role="table", **target)
        rows = locator.evaluate(
            """table => {
              const rowElements = Array.from(table.querySelectorAll('tr'));
              const hasHeader = Boolean(rowElements[0]?.querySelector('th'));
              const headers = Array.from(rowElements[0]?.querySelectorAll('th,td') || [])
                .map(cell => (cell.innerText || cell.textContent || '').trim());
              return rowElements.slice(hasHeader ? 1 : 0).map(row => {
                const cells = Array.from(row.querySelectorAll('th,td'))
                  .map(cell => (cell.innerText || cell.textContent || '').trim());
                return Object.fromEntries(cells.map((value, index) =>
                  [headers[index] || `column_${index + 1}`, value]));
              });
            }"""
        )
        return {
            "source_url": self._safe_page_url(page.url),
            "source_title": self._sanitize_records(page.title()),
            "rows": self._sanitize_records(rows),
        }

    def read_list(self, **target: Any) -> dict[str, Any]:
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        locator = self._find_target(role="list", **target)
        items = locator.evaluate(
            """list => Array.from(list.querySelectorAll(':scope > li,[role="listitem"]'))
              .map(item => (item.innerText || item.textContent || '').trim())
              .filter(Boolean)"""
        )
        return {
            "source_url": self._safe_page_url(page.url),
            "source_title": self._sanitize_records(page.title()),
            "items": [
                {"text": text, "source_url": self._safe_page_url(page.url)}
                for text in self._sanitize_records(items)
            ],
        }

    def read_semantic_records(self, max_items: int = 200) -> list[dict[str, Any]]:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 1_000:
            raise ValueError("max_items deve ser um inteiro entre 1 e 1000")
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")

        script = """elements => elements.map(element => {
          const visible = item => {
            const rect = item.getBoundingClientRect();
            const style = window.getComputedStyle(item);
            return rect.width > 0 && rect.height > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          };
          const text = (element.innerText || element.textContent || '').trim();
          const heading = element.querySelector('h1,h2,h3,h4,[role="heading"]');
          const links = Array.from(element.querySelectorAll('a[href]'))
            .filter(visible)
            .map(link => ({
              text: (link.innerText || link.getAttribute('aria-label') ||
                link.getAttribute('title') || '').trim(),
              href: link.href
            }));
          return {
            title: (heading && (heading.innerText || heading.textContent) || '').trim(),
            text,
            links
          };
        }).filter(item => item.text)"""

        records: list[dict[str, Any]] = []
        for role in ("article", "listitem"):
            candidates = page.get_by_role(role)
            if candidates.count() == 0:
                continue
            records = candidates.evaluate_all(script)[:max_items]
            if records:
                break

        safe_records = self._sanitize_records(records)
        if not isinstance(safe_records, list):
            raise RuntimeError("A leitura dos elementos semânticos retornou formato inválido")
        for record in safe_records:
            if not isinstance(record, dict):
                continue
            safe_links = []
            for link in record.get("links", []):
                if not isinstance(link, dict) or not isinstance(link.get("href"), str):
                    continue
                safe_url = self._safe_page_url(link["href"])
                if safe_url:
                    safe_links.append({"text": link.get("text", ""), "url": safe_url})
            record["links"] = safe_links
            record["source_url"] = self._safe_page_url(page.url)
        return [item for item in safe_records if isinstance(item, dict)]

    @staticmethod
    def _sanitize_records(value: Any) -> Any:
        if isinstance(value, str):
            for pattern in _SENSITIVE_TEXT_PATTERNS:
                value = pattern.sub("[REDACTED]", value)
            return value
        if isinstance(value, list):
            return [BrowserController._sanitize_records(item) for item in value]
        if isinstance(value, dict):
            return {
                key: BrowserController._sanitize_records(item)
                for key, item in value.items()
            }
        return value

    def wait_for_download(
        self,
        *,
        timeout_ms: int = 30_000,
        **target: Any,
    ) -> dict[str, str | None]:
        self._validate_timeout(timeout_ms)
        page = self.getActivePage()
        if page is None:
            raise RuntimeError("Não há uma aba ativa disponível")
        locator = self._find_target(**target)
        descriptor = locator.evaluate(
            """element => [
              element.innerText || element.textContent || '',
              element.getAttribute('aria-label') || '',
              element.getAttribute('href') || '',
              element.getAttribute('name') || '',
              element.id || ''
            ].join(' ')"""
        )
        self._reject_protected_action(descriptor)
        with page.expect_download(timeout=timeout_ms) as download_info:
            locator.click(timeout=timeout_ms)
        download = download_info.value
        failure = download.failure()
        if failure:
            raise RuntimeError(f"Download falhou: {failure}")
        parts = urlsplit(download.url)
        safe_url = (
            urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            if parts.scheme in {"http", "https"} and parts.hostname
            and parts.username is None and parts.password is None
            else ""
        )
        return {
            "suggested_filename": download.suggested_filename,
            "path": str(download.path()),
            "url": safe_url,
        }

    def upload_file(
        self,
        file_path: str,
        *,
        timeout_ms: int = 5_000,
        **target: Any,
    ) -> dict[str, str]:
        self._validate_timeout(timeout_ms)
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ValueError("file_path deve apontar para um arquivo existente")
        target.setdefault("tag_name", "input")
        locator = self._find_target(**target)
        if (locator.get_attribute("type") or "").casefold() != "file":
            raise ValueError("O alvo identificado não é um input de arquivo")
        self._guard_target_action(locator)
        locator.set_input_files(str(path.resolve()), timeout=timeout_ms)
        return {"uploaded": True, "filename": path.name}

    def screenshot(self) -> bytes | None:
        page = self._active_page()
        return page.screenshot() if page is not None else None

    def _active_page(self) -> Page | None:
        return self.getActivePage()
