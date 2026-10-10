from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scout.v2_models import V2Lead

PHONE_PATTERN = re.compile(
    r"(?<!\w)(?:\+?55[\s.-]*)?(?:\(?\d{2}\)?[\s.-]*)?(?:9?\d{4})[\s.-]?\d{4}(?!\w)"
)
SOCIAL_OR_DIRECTORY_HOSTS = (
    "facebook.com", "fb.com", "linkedin.com", "tiktok.com", "youtube.com", "youtu.be",
    "x.com", "twitter.com", "maps.google.", "google.com",
)
WEBSITE_LABEL = re.compile(r"\b(site|website|web|p[aá]gina oficial|homepage|www\.|oficial)\b", re.I)
_SENSITIVE_URL_PARAMETER = re.compile(
    r"token|cookie|csrf|auth|password|secret|credential|session|api[_-]?key",
    re.I,
)
_SENSITIVE_RAW_KEY = re.compile(
    r"password|token|cookie|csrf|auth|secret|credential|session|api[_-]?key",
    re.I,
)
_RAW_SECRET_PATTERNS = (
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
    re.compile(
        r"""(?i)(?:access[_ -]?token|refresh[_ -]?token|csrf(?:[_ -]?token)?|"""
        r"""authorization|cookie|password|secret|api[_ -]?key)\s*[:=]\s*"""
        r"""["']?[^\s,;"'}]+"""
    ),
)


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized or None


def normalize_company_name(value: str | None) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    decomposed = unicodedata.normalize("NFKD", normalized.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", without_marks, flags=re.UNICODE).strip()


def normalize_phone(value: str | None) -> str | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    digits = re.sub(r"\D", "", normalized)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 8:
        return None
    if normalized.lstrip().startswith("+") and digits.startswith("55"):
        return f"+{digits}"
    if digits.startswith("55") and len(digits) in {12, 13}:
        return f"+{digits}"
    return digits


def normalize_url(value: str | None) -> str | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    candidate = normalized.strip("<>()[]{}.,;")
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        if parts.scheme.casefold() not in {"http", "https"} or not hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
    except ValueError:
        return None
    if "." not in hostname and ":" not in hostname:
        return None
    netloc = hostname.casefold()
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        default_port = (parts.scheme.casefold() == "http" and port == 80) or (
            parts.scheme.casefold() == "https" and port == 443
        )
        if not default_port:
            netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/") or "/"
    query = urlencode([
        (key, parameter)
        for key, parameter in parse_qsl(parts.query, keep_blank_values=True)
        if not _SENSITIVE_URL_PARAMETER.search(key)
        and not any(pattern.search(parameter) for pattern in _RAW_SECRET_PATTERNS)
    ])
    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def _sanitize_raw_data(value: Any, key: str = "") -> Any:
    if _SENSITIVE_RAW_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            child_key: _sanitize_raw_data(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_raw_data(item) for item in value]
    if not isinstance(value, str):
        return value
    if key.casefold() in {"url", "href", "source_url", "company_url", "website"}:
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            parts = None
            hostname = None
            port = None
        if parts is not None and parts.scheme.casefold() in {"http", "https"} and hostname:
            query = urlencode([
                (parameter, parameter_value)
                for parameter, parameter_value in parse_qsl(parts.query, keep_blank_values=True)
                if not _SENSITIVE_URL_PARAMETER.search(parameter)
                and not any(pattern.search(parameter_value) for pattern in _RAW_SECRET_PATTERNS)
            ])
            host = hostname.casefold()
            if port is not None:
                host = f"{host}:{port}"
            value = urlunsplit((parts.scheme.casefold(), host, parts.path, query, ""))
    for pattern in _RAW_SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _extract_labeled(text: str, labels: str) -> str | None:
    expression = re.compile(rf"^\s*(?:{labels})\s*[:\-]\s*(.*?)\s*$", re.IGNORECASE)
    for line in text.splitlines():
        match = expression.match(line)
        if match:
            return normalize_text(match.group(1))
    return None


def _clean_company_name(value: Any) -> str | None:
    normalized = normalize_text(value if isinstance(value, str) else None)
    if not normalized:
        return None
    return normalize_text(normalized.splitlines()[0])


def _links(raw: dict[str, Any]) -> list[dict[str, Any]]:
    result = raw.get("links")
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def extract_lead(
    result: dict[str, Any],
    *,
    city: str | None = None,
    state: str | None = None,
    category: str | None = None,
    company_details: dict[str, Any] | None = None,
) -> V2Lead:
    details = company_details or {}
    text = "\n".join(
        value for value in (result.get("text"), details.get("text"))
        if isinstance(value, str) and value
    )
    links = _links(result) + _links(details)
    page_url = normalize_url(result.get("url") if isinstance(result.get("url"), str) else None)
    if not page_url:
        page_url = normalize_url(details.get("url") if isinstance(details.get("url"), str) else None)

    website = None
    instagram = None
    link_sources: list[dict[str, str]] = []
    aivio_host = urlsplit(page_url).hostname if page_url else None
    for link in links:
        raw_url = link.get("url")
        url = normalize_url(raw_url if isinstance(raw_url, str) else None)
        if not url:
            continue
        host = urlsplit(url).hostname or ""
        title = normalize_text(link.get("title") if isinstance(link.get("title"), str) else None) or ""
        link_sources.append({"title": title, "url": url})
        if host.casefold() == "instagram.com" or host.casefold().endswith(".instagram.com"):
            instagram = instagram or url
        elif aivio_host and host.casefold() == aivio_host.casefold():
            continue
        elif any(host.casefold() == blocked or host.casefold().endswith(f".{blocked}")
                 or host.casefold().startswith(blocked) for blocked in SOCIAL_OR_DIRECTORY_HOSTS):
            continue
        elif not website and (
            WEBSITE_LABEL.search(title)
            or title.casefold().strip() in {host.casefold(), f"www.{host.casefold()}"}
        ):
            website = url

    explicit_phone = _extract_labeled(text, r"telefone|tel\.?|celular|fone")
    phone = normalize_phone(explicit_phone)
    if not phone:
        phone_match = PHONE_PATTERN.search(text)
        phone = normalize_phone(phone_match.group(0)) if phone_match else None

    address = _extract_labeled(text, r"endere[cç]o|address")
    labeled_website = _extract_labeled(text, r"site|website|p[aá]gina oficial")
    website = website or normalize_url(labeled_website)
    labeled_instagram = _extract_labeled(text, r"instagram")
    instagram = instagram or normalize_url(labeled_instagram) or labeled_instagram
    name = _clean_company_name(details.get("title") or result.get("title"))
    if name and "|" in name:
        name = normalize_text(name.split("|", 1)[0])
    if not name:
        name = normalize_text(" ".join(text.splitlines()[:1])) or ""

    actual_city = _extract_labeled(text, r"cidade|city|munic[ií]pio") or normalize_text(city)
    actual_state = _extract_labeled(text, r"estado|state") or normalize_text(state)
    actual_category = _extract_labeled(text, r"categoria|category") or normalize_text(category)
    explicit_no_website = bool(re.search(r"\b(?:sem site|sem website|não possui site|nao possui site)\b", text, re.I))
    raw_data = _sanitize_raw_data({
        "search_result": result,
        "company_details": details,
        "links": link_sources,
    })
    return V2Lead(
        lead_id=str(uuid.uuid4()),
        company_name=name,
        category=actual_category,
        phone=phone,
        address=address,
        city=actual_city,
        state=actual_state,
        website=website,
        instagram=instagram,
        source="AIVIO",
        source_url=page_url,
        company_url=page_url,
        has_website=True if website else False if explicit_no_website else None,
        raw_data=raw_data,
    )


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).hostname
    if not host:
        return None
    return host.casefold().removeprefix("www.")


def deduplicate_leads(leads: list[V2Lead]) -> list[V2Lead]:
    retained: list[V2Lead] = []
    index: dict[tuple[str, str], int] = {}
    mergeable_fields = (
        "category", "phone", "address", "city", "state", "website", "instagram",
        "source_url", "company_url", "has_website", "notes",
    )
    for lead in leads:
        keys: list[tuple[str, str]] = []
        if lead.phone:
            keys.append(("phone", lead.phone))
        domain = _domain(lead.website)
        if domain:
            keys.append(("domain", domain))
        name = normalize_company_name(lead.company_name)
        city = normalize_company_name(lead.city)
        if name and city:
            keys.append(("name_city", f"{name}|{city}"))
        if lead.company_url:
            keys.append(("company_url", lead.company_url.casefold()))

        duplicate_index = next((index[key] for key in keys if key in index), None)
        if duplicate_index is None:
            duplicate_index = len(retained)
            retained.append(lead)
        else:
            existing = retained[duplicate_index]
            for field_name in mergeable_fields:
                current_value = getattr(existing, field_name)
                incoming_value = getattr(lead, field_name)
                if current_value is None or current_value == "":
                    if incoming_value is not None and incoming_value != "":
                        setattr(existing, field_name, incoming_value)
            existing.raw_data.setdefault("duplicates", []).append(lead.raw_data)
        for key in keys:
            index.setdefault(key, duplicate_index)
    return retained
