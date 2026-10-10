import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from scout.models import WebsiteAnalysis

USER_AGENT = "AurenScout/1.0"
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(slots=True)
class FetchResult:
    status: int
    headers: object
    body: bytes
    elapsed_ms: int


def _validate_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("URL deve ser HTTP(S) público, sem credenciais")
    hostname = parts.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("host local ou interno não permitido")
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                               type=socket.SOCK_STREAM)
            }
        except OSError as error:
            raise ValueError("host não pôde ser validado") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("host não público não permitido")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        _validate_public_url(new_url)
        return super().redirect_request(request, file, code, message, headers, new_url)


class PublicHttpClient:
    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _fetch(self, url: str) -> FetchResult:
        _validate_public_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
                return FetchResult(response.status, response.headers, body,
                                   int((time.monotonic() - started) * 1000))
        except urllib.error.HTTPError as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
            return FetchResult(response.code, response.headers, body,
                               int((time.monotonic() - started) * 1000))

    def _robots_parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                response = self._fetch(robots_url)
                if response.status in (401, 403):
                    parser.parse(["User-agent: *", "Disallow: /"])
                elif response.status == 404:
                    parser.parse(["User-agent: *", "Disallow:"])
                elif 200 <= response.status < 300:
                    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                else:
                    parser = None
            except (OSError, ValueError, urllib.error.URLError):
                parser = None
            self._robots[origin] = parser
        return self._robots[origin]

    def can_fetch(self, url: str) -> bool:
        _validate_public_url(url)
        parser = self._robots_parser(url)
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> FetchResult:
        return self._fetch(url)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.title = False
        self.title_text = ""
        self.has_viewport = False
        self.has_form = False
        self.has_h1 = False
        self.has_heading = False
        self.has_main = False
        self.has_nav = False
        self.has_cta = False
        self.has_services = False
        self.phone_number: str | None = None
        self.whatsapp_url: str | None = None
        self.has_email = False
        self.outdated_signals: list[str] = []
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        tag = tag.casefold()
        if tag == "title":
            self.title = True
        if tag == "meta" and values.get("name", "").casefold() == "viewport":
            self.has_viewport = True
        if tag == "form":
            self.has_form = True
        if tag == "h1":
            self.has_h1 = True
        if tag in {"h1", "h2", "h3"}:
            self.has_heading = True
        if tag == "main":
            self.has_main = True
        if tag == "nav":
            self.has_nav = True
        if tag in {"font", "marquee", "center"} and tag not in self.outdated_signals:
            self.outdated_signals.append(f"Elemento HTML legado: <{tag}>")
        if tag == "a":
            href = values.get("href", "")
            if href.casefold().startswith("tel:") and self.phone_number is None:
                self.phone_number = urllib.parse.unquote(href[4:]).strip() or None
            if re.search(r"(?:wa\.me/|api\.whatsapp\.com/send|whatsapp\.com/send)", href, re.I):
                self.whatsapp_url = href
            if href.casefold().startswith("mailto:"):
                self.has_email = True
            if re.search(r"contato|agend|or[cç]amento|fale conosco|reserv", href, re.I):
                self.has_cta = True
        if tag in {"button", "input"}:
            text = " ".join((values.get("value", ""), values.get("aria-label", ""), values.get("title", "")))
            submit = tag == "input" and values.get("type", "").casefold() == "submit"
            if submit or re.search(r"contato|agend|or[cç]amento|reserv|comprar|enviar|fale conosco", text, re.I):
                self.has_cta = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.title = False

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.text.append(cleaned)
            if self.title:
                self.title_text += cleaned
            if re.search(r"servi[cç]|tratament|especialidade|produto|card[aá]pio|menu", cleaned, re.I):
                self.has_services = True
            if re.search(r"contato|agend|or[cç]amento|fale conosco|reserv", cleaned, re.I):
                self.has_cta = True


class WebsiteAnalyzer:
    def __init__(self, client: PublicHttpClient | None = None) -> None:
        self.client = client or PublicHttpClient()

    def analyze(self, url: str) -> WebsiteAnalysis:
        result = WebsiteAnalysis(url=url)
        try:
            if not self.client.can_fetch(url):
                result.status = "blocked_by_robots"
                result.findings.append("Análise não realizada: acesso não permitido ou robots.txt indisponível")
                return result
            response = self.client.fetch(url)
        except Exception as error:
            result.status = "broken"
            result.findings.append(f"Site inacessível: {type(error).__name__}")
            return result

        result.performance_ms = response.elapsed_ms
        result.https = urlsplit(url).scheme.casefold() == "https"
        if not 200 <= response.status < 400:
            result.status = "broken"
            result.findings.append(f"Resposta HTTP {response.status}")
            return result
        result.status = "working"
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.casefold():
            result.findings.append("Conteúdo não HTML; análise de conteúdo não verificada")
            return result

        parser = _PageParser()
        try:
            charset = response.headers.get_content_charset() or "utf-8"
            parser.feed(response.body.decode(charset, errors="replace"))
        except (LookupError, ValueError):
            result.findings.append("HTML não pôde ser interpretado")
            return result

        page_text = " ".join(parser.text)
        result.mobile_friendly = parser.has_viewport
        result.phone_number = parser.phone_number
        result.has_phone = parser.phone_number is not None
        result.whatsapp_url = parser.whatsapp_url
        result.has_whatsapp = parser.whatsapp_url is not None
        result.has_form = parser.has_form
        result.clear_services = parser.has_services
        result.has_contact_info = result.has_phone or result.has_whatsapp or parser.has_email
        result.has_call_to_action = parser.has_cta
        result.outdated_signals = parser.outdated_signals
        year_match = re.search(r"(?:©|&copy;|copyright)\s*(20\d{2})", page_text, re.I)
        if year_match and int(year_match.group(1)) < datetime.now().year - 5:
            result.outdated_signals.append(f"Ano de copyright antigo: {year_match.group(1)}")
        result.findings.extend([
            "Auditoria limitada ao HTML da página inicial; conteúdo dinâmico pode não ter sido observado",
            "Aparência visual e velocidade percebida não foram verificadas sem navegador",
        ])
        if not result.mobile_friendly:
            result.findings.append("Meta viewport não identificada no HTML")
        result.appearance_score = None
        return self._with_website_score(result)

    @staticmethod
    def _with_website_score(result: WebsiteAnalysis) -> WebsiteAnalysis:
        weights = {
            "https": (result.https, 10),
            "mobile": (result.mobile_friendly, 25),
            "phone": (result.has_phone, 10),
            "whatsapp": (result.has_whatsapp, 6),
            "form": (result.has_form, 8),
            "services": (result.clear_services, 12),
            "contact": (result.has_contact_info, 12),
            "cta": (result.has_call_to_action, 17),
        }
        deductions = sum(weight for value, weight in weights.values() if value is False)
        result.quality_score = max(0, 100 - deductions)
        return result