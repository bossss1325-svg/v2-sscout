from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class BossInterfaceError(RuntimeError):
    def __init__(self, status: int | None, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def validate_secret(secret: str | bytes) -> bytes:
    encoded = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not isinstance(encoded, bytes) or len(encoded) < 32:
        raise ValueError("A chave compartilhada do AUREN BOSS deve ter ao menos 32 bytes")
    return encoded


def normalize_origin(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("allowed_origins deve conter origens HTTP(S) não vazias")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as error:
        raise ValueError("Origem inválida em allowed_origins") from error
    hostname = parts.hostname
    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise ValueError("allowed_origins aceita somente origens HTTP(S), sem caminho ou credenciais")
    hostname = hostname.casefold()
    allow_local = os.getenv("SCOUT_ALLOW_LOCAL_TEST_ORIGINS") == "1"
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    is_local_name = hostname == "localhost" or hostname.endswith((".localhost", ".local"))
    if not allow_local and (is_local_name or (address is not None and not address.is_global)):
        raise ValueError("Origem local ou não pública não permitida para tarefas do operador")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parts.scheme == "https" else 80
    authority = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return f"{parts.scheme}://{authority}"


def validate_allowed_origins(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("authorization.allowed_origins deve listar de 1 a 20 origens")
    normalized = [normalize_origin(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError("authorization.allowed_origins não pode conter duplicatas")
    return normalized


def origin_for_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if not parts.hostname:
            raise ValueError
        port = parts.port
    except ValueError as error:
        raise ValueError("URL HTTP(S) inválida") from error
    return normalize_origin(
        f"{parts.scheme}://{parts.hostname}{f':{port}' if port is not None else ''}"
    )


def _signature_payload(
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}".encode("utf-8")


def signed_request_headers(
    secret: str | bytes,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    key = validate_secret(secret)
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce_text = nonce or str(uuid.uuid4())
    signature = hmac.new(
        key,
        _signature_payload(method, path, body, timestamp_text, nonce_text),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Auren-Timestamp": timestamp_text,
        "X-Auren-Nonce": nonce_text,
        "X-Auren-Signature": signature,
    }


def verify_request_signature(
    secret: str | bytes,
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    try:
        key = validate_secret(secret)
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_time = int(time.time()) if now is None else now
    if abs(current_time - issued_at) > tolerance_seconds or not 16 <= len(nonce) <= 128:
        return False
    expected = hmac.new(
        key,
        _signature_payload(method, path, body, timestamp, nonce),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class AurenBossV2Interface:
    """Authenticated Python client for the V2 operator and result-package API."""

    def __init__(
        self,
        shared_secret: str | bytes,
        base_url: str = "http://127.0.0.1:8080",
        timeout_seconds: float = 30,
    ) -> None:
        self.shared_secret = validate_secret(shared_secret)
        parts = urlsplit(base_url)
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.port is None
        ):
            raise ValueError("A interface do Boss aceita somente uma API HTTP em 127.0.0.1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds deve ser positivo")
        self.base_url = f"http://127.0.0.1:{parts.port}"
        self.timeout_seconds = timeout_seconds

    def submit_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/operator/tasks", task)

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/operator/tasks/{quote(self._uuid(task_id), safe='')}")

    def list_pending_results(self) -> dict[str, Any]:
        return self._request("GET", "/v2/results/pending")

    def get_result(self, package_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/results/{quote(self._uuid(package_id), safe='')}")

    def acknowledge_result(
        self,
        package_id: str,
        status: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"delivered", "failed"}:
            raise ValueError("status deve ser delivered ou failed")
        payload: dict[str, Any] = {"status": status}
        if detail is not None:
            payload["detail"] = detail
        return self._request(
            "POST",
            f"/v2/results/{quote(self._uuid(package_id), safe='')}/delivery",
            payload,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else b""
        request = Request(
            self.base_url + path,
            data=body if method == "POST" else None,
            headers={
                "Content-Type": "application/json",
                **signed_request_headers(self.shared_secret, method, path, body),
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                response = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = {}
            code = response.get("error", "http_error") if isinstance(response, dict) else "http_error"
            raise BossInterfaceError(error.code, str(code), f"A API V2 retornou HTTP {error.code}: {code}") from None
        except (URLError, TimeoutError) as error:
            raise BossInterfaceError(None, "connection_failed", f"Falha de comunicação com a API V2: {type(error).__name__}") from None
        if not isinstance(result, dict):
            raise BossInterfaceError(None, "invalid_response", "A API V2 retornou resposta não estruturada")
        return result

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Identificador deve ser um UUID válido") from error