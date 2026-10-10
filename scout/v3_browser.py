from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright

V3_CDP_ENDPOINT = "http://127.0.0.1:9224"
_WHATSAPP_HOST = "web.whatsapp.com"
_READ_CONVERSATION_SCRIPT = r"""() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none';
  };
  const header = document.querySelector('[data-testid="conversation-header"]') ||
    document.querySelector('main header');
  const messageElements = Array.from(document.querySelectorAll('[data-pre-plain-text]'))
    .filter(visible);
  return {
    title: header ? (header.innerText || header.textContent || '').trim() : '',
    messages: messageElements.map(element => ({
      text: Array.from(element.querySelectorAll('.selectable-text'))
        .map(item => (item.innerText || item.textContent || '').trim())
        .filter(Boolean).join('\n') || (element.innerText || element.textContent || '').trim(),
      outgoing: Boolean(element.closest('.message-out')),
      timestamp: element.getAttribute('data-pre-plain-text') || ''
    })).filter(item => item.text)
  };
}"""


class HumanInterventionRequired(RuntimeError):
    pass


class WhatsAppWebAdapter:
    """Bounded Playwright adapter for the already-open WhatsApp Web conversation."""

    def __init__(self, cdp_endpoint: str = V3_CDP_ENDPOINT) -> None:
        if cdp_endpoint != V3_CDP_ENDPOINT:
            raise ValueError(f"A V3 aceita somente o CDP dedicado {V3_CDP_ENDPOINT}")
        self.cdp_endpoint = cdp_endpoint
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def connect(self, timeout_ms: int = 3_000) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(
                self.cdp_endpoint,
                timeout=timeout_ms,
            )
            session = browser.new_browser_cdp_session()
            try:
                product = session.send("Browser.getVersion").get("product", "")
            finally:
                session.detach()
            if not isinstance(product, str) or (
                "edg/" not in product.casefold()
                and "microsoft edge" not in product.casefold()
            ):
                raise RuntimeError("O endpoint V3 não pertence ao Microsoft Edge")
        except Exception:
            playwright.stop()
            raise
        self._playwright = playwright
        self._browser = browser

    def disconnect(self) -> None:
        playwright = self._playwright
        self._playwright = None
        self._browser = None
        if playwright is not None:
            playwright.stop()

    def health_check(self) -> dict[str, Any]:
        try:
            self.connect()
            page = self._active_whatsapp_page()
            return {
                "browser_connected": True,
                "whatsapp_available": True,
                "cdp_endpoint": self.cdp_endpoint,
                "active_tab": {"title": page.title(), "url": self._safe_url(page.url)},
                "error": None,
            }
        except Exception as error:
            return {
                "browser_connected": self._browser is not None and self._browser.is_connected(),
                "whatsapp_available": False,
                "cdp_endpoint": self.cdp_endpoint,
                "active_tab": None,
                "error": f"{type(error).__name__}: {error}",
            }
        finally:
            self.disconnect()

    def _active_whatsapp_page(self) -> Any:
        if self._browser is None or not self._browser.is_connected():
            raise RuntimeError("O Edge V3 não está conectado")
        pages = [
            page
            for context in self._browser.contexts
            for page in context.pages
            if not page.is_closed() and urlsplit(page.url).hostname == _WHATSAPP_HOST
        ]
        focused = [
            page for page in pages
            if page.evaluate("document.visibilityState === 'visible' && document.hasFocus()")
        ]
        if len(focused) == 1:
            return focused[0]
        if not focused and len(pages) == 1:
            return pages[0]
        if not pages:
            raise HumanInterventionRequired(
                "Abra e autentique manualmente o WhatsApp Web no perfil V3 antes da tarefa"
            )
        raise HumanInterventionRequired("Há mais de uma aba WhatsApp elegível; selecione uma manualmente")

    def read_conversation(self, recipient_id: str, limit: int = 20) -> dict[str, Any]:
        page = self._active_whatsapp_page()
        state = page.evaluate(_READ_CONVERSATION_SCRIPT)
        if not isinstance(state, dict) or not isinstance(state.get("messages"), list):
            raise RuntimeError("Não foi possível ler a conversa ativa")
        title = self._normalize_label(state.get("title"))
        if title != self._normalize_label(recipient_id):
            raise HumanInterventionRequired(
                "A conversa ativa não corresponde exatamente ao recipient_id informado"
            )
        messages = [
            item for item in state["messages"]
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return {
            "recipient_id": recipient_id,
            "conversation_title": state["title"],
            "source_url": self._safe_url(page.url),
            "messages": messages[-limit:],
            "total_visible": len(messages),
        }

    def send_message(self, recipient_id: str, body: str, timeout_ms: int = 15_000) -> dict[str, Any]:
        page = self._active_whatsapp_page()
        initial = page.evaluate(_READ_CONVERSATION_SCRIPT)
        title = self._normalize_label(initial.get("title")) if isinstance(initial, dict) else ""
        if title != self._normalize_label(recipient_id):
            raise HumanInterventionRequired(
                "A conversa ativa não corresponde exatamente ao recipient_id informado"
            )
        composers = page.locator('[contenteditable="true"][role="textbox"]')
        visible_composers = [
            composers.nth(index) for index in range(composers.count())
            if composers.nth(index).is_visible()
        ]
        if len(visible_composers) != 1:
            raise HumanInterventionRequired("O campo de composição está ausente ou ambíguo")
        composer = visible_composers[0]
        current_text = (composer.inner_text() or "").strip()
        if current_text:
            raise HumanInterventionRequired(
                "O campo de composição já contém texto; revise-o manualmente antes do envio"
            )
        before_count = sum(
            item.get("outgoing") is True
            for item in initial.get("messages", [])
            if isinstance(item, dict)
        )
        composer.fill(body, timeout=timeout_ms)
        if (composer.inner_text() or "").strip() != body.strip():
            raise RuntimeError("O texto não foi confirmado no campo de composição")
        send_buttons = page.get_by_role("button", name=re.compile(r"^(send|enviar)$", re.I))
        visible_buttons = [
            send_buttons.nth(index) for index in range(send_buttons.count())
            if send_buttons.nth(index).is_visible() and send_buttons.nth(index).is_enabled()
        ]
        if len(visible_buttons) != 1:
            raise HumanInterventionRequired("O botão de envio está ausente ou ambíguo")
        visible_buttons[0].click(timeout=timeout_ms)
        try:
            page.wait_for_function(
                """({body, beforeCount}) => {
                  const visible = element => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                      style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const outgoing = Array.from(document.querySelectorAll('[data-pre-plain-text]'))
                    .filter(element => visible(element) && element.closest('.message-out'));
                  return outgoing.length > beforeCount && outgoing.some(element =>
                    Array.from(element.querySelectorAll('.selectable-text')).some(text =>
                      (text.innerText || text.textContent || '').trim() === body
                    )
                  );
                }""",
                arg={"body": body.strip(), "beforeCount": before_count},
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                "O envio foi acionado, mas não há confirmação observável; estado incerto, não repetir automaticamente"
            ) from error
        return {
            "status": "sent_confirmed",
            "recipient_id": recipient_id,
            "body": body,
            "source_url": self._safe_url(page.url),
        }

    @staticmethod
    def _normalize_label(value: Any) -> str:
        return " ".join(value.split()).casefold() if isinstance(value, str) else ""

    @staticmethod
    def _safe_url(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"