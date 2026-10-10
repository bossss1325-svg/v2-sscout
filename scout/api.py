import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

from scout.browser_operator import BrowserOperator, BrowserOperatorExecutor
from scout.boss_interface import validate_secret, verify_request_signature
from scout.service import ScoutService
from scout.task_executor import TaskExecutor, _safe_error_message
from scout.v2_models import Task, TaskStatus

logger = logging.getLogger("scout.api")
MAX_REQUEST_BYTES = 64_000


def create_server(
    service: ScoutService,
    host: str = "127.0.0.1",
    port: int = 8080,
    task_executor: TaskExecutor | None = None,
    operator_executor: BrowserOperatorExecutor | None = None,
    boss_shared_secret: str | bytes | None = None,
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("a API só pode escutar em 127.0.0.1")
    executor_holder = [task_executor]
    executor_lock = Lock()
    operator_holder = [operator_executor]
    operator_lock = Lock()
    configured_secret = boss_shared_secret if boss_shared_secret is not None else os.getenv(
        "SCOUT_BOSS_SHARED_SECRET"
    )
    shared_secret = validate_secret(configured_secret) if configured_secret else None
    nonce_lock = Lock()
    used_nonces: dict[str, int] = {}
    def get_task_executor() -> TaskExecutor:
        with executor_lock:
            if executor_holder[0] is None:
                executor_holder[0] = TaskExecutor()
            return executor_holder[0]

    def get_operator_executor() -> BrowserOperatorExecutor:
        with operator_lock:
            if operator_holder[0] is None:
                operator_holder[0] = BrowserOperatorExecutor()
            return operator_holder[0]

    class Handler(BaseHTTPRequestHandler):
        def _authenticate_boss(self, body: bytes = b"") -> bool:
            if shared_secret is None:
                self._json(503, {"error": "boss_auth_not_configured"})
                return False
            timestamp = self.headers.get("X-Auren-Timestamp", "")
            nonce = self.headers.get("X-Auren-Nonce", "")
            signature = self.headers.get("X-Auren-Signature", "")
            valid = verify_request_signature(
                shared_secret,
                self.command,
                self.path,
                body,
                timestamp,
                nonce,
                signature,
            )
            if not valid:
                self._json(401, {"error": "invalid_boss_signature"})
                return False
            issued_at = int(timestamp)
            with nonce_lock:
                expired = [key for key, created in used_nonces.items() if created < int(time.time()) - 300]
                for key in expired:
                    used_nonces.pop(key, None)
                if nonce in used_nonces:
                    self._json(401, {"error": "replayed_boss_request"})
                    return False
                used_nonces[nonce] = issued_at
            return True

        def _read_authenticated_body(self) -> bytes | None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError
            except ValueError:
                self._json(400, {"error": "invalid_request_size"})
                return None
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                self._json(400, {"error": "incomplete_request_body"})
                return None
            return body

        def _request_body(self, content_length: int) -> bytes:
            authenticated_body = getattr(self, "_authenticated_body", None)
            return authenticated_body if authenticated_body is not None else self.rfile.read(content_length)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if (
                path == "/v2/results"
                or path.startswith("/v2/results/")
                or path.startswith("/tasks/")
                or path.startswith("/operator/tasks/")
            ) and not self._authenticate_boss():
                return
            if path == "/v2/results" or path == "/v2/results/pending":
                delivery_status = "pending" if path.endswith("/pending") else None
                self._json(200, {
                    "results": get_task_executor().list_result_packages(delivery_status),
                })
                return
            v2_result_prefix = "/v2/results/"
            v2_result_id = path[len(v2_result_prefix):] if path.startswith(v2_result_prefix) else ""
            if v2_result_id and "/" not in v2_result_id:
                package = get_task_executor().get_result_package(v2_result_id)
                if package is not None:
                    self._json(200, package)
                else:
                    self._json(404, {"error": "result_package_not_found"})
                return
            if path == "/health":
                health = {
                    "status": "ok",
                    "api_operational": True,
                    "operational": False,
                    "provider": service.provider.__class__.__name__,
                }
                try:
                    health.update(get_task_executor().health_check())
                except Exception as error:
                    logger.exception("Falha ao consultar estado do browser para health check")
                    health.update({
                        "browser_connected": False,
                        "edge_connected": False,
                        "edge_product": None,
                        "browser_version": None,
                        "active_tab": None,
                        "aivio_available": False,
                        "aivio_hostname": None,
                        "aivio_url": None,
                        "cdp_endpoint": None,
                        "expected_edge_profile": "EdgeProfile-V2",
                        "browser_error": _safe_error_message(error),
                    })
                self._json(200, health)
                return
            task_prefix = "/tasks/"
            task_id = path[len(task_prefix):] if path.startswith(task_prefix) else ""
            if task_id and "/" not in task_id:
                result = get_task_executor().get_task_result(task_id)
                if result is not None:
                    self._json(200, result)
                else:
                    self._json(404, {"error": "task_not_found"})
                return
            operator_prefix = "/operator/tasks/"
            operator_task_id = (
                path[len(operator_prefix):] if path.startswith(operator_prefix) else ""
            )
            if operator_task_id and "/" not in operator_task_id:
                result = get_operator_executor().get_result(operator_task_id)
                if result is not None:
                    self._json(200, result)
                else:
                    self._json(404, {"error": "task_not_found"})
                return
            prefix = "/scout/lead/"
            if path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
                lead = service.store.get(path[len(prefix):])
                if lead:
                    self._json(200, lead.to_dict())
                else:
                    self._json(404, {"error": "lead_not_found"})
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            protected_request = (
                path in {"/tasks", "/operator/tasks"}
                or (path.startswith("/v2/results/") and path.endswith("/delivery"))
            )
            if protected_request:
                body = self._read_authenticated_body()
                if body is None:
                    return
                if not self._authenticate_boss(body):
                    return
                self._authenticated_body = body
            v2_delivery_prefix = "/v2/results/"
            if path.startswith(v2_delivery_prefix) and path.endswith("/delivery"):
                package_id = path[len(v2_delivery_prefix):-len("/delivery")].strip("/")
                self._handle_v2_result_delivery(package_id)
                return
            if path == "/tasks":
                self._handle_task()
                return
            if path == "/operator/tasks":
                self._handle_operator_task()
                return
            if path != "/scout/search":
                self._json(404, {"error": "not_found"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError("tamanho de requisição inválido")
                payload = json.loads(self._request_body(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("corpo deve ser um objeto JSON")
                query, city, state = (payload.get(key) for key in ("query", "city", "state"))
                if not all(isinstance(value, str) and value.strip() for value in (query, city, state)):
                    raise ValueError("query, city e state são obrigatórios")
                limit = payload.get("limit", 20)
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                    raise ValueError("limit deve ser um inteiro entre 1 e 100")
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                self._json(400, {"error": "invalid_request", "message": str(error)})
                return

            try:
                result = service.search(query.strip(), city.strip(), state.strip(), limit)
            except Exception:
                logger.exception("Falha na pesquisa solicitada pela API")
                self._json(500, {"error": "search_failed"})
                return
            self._json(200, {
                "results": [lead.to_dict() for lead in result.results],
                "total": result.total,
                "errors": result.errors,
            })

        def _handle_task(self) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError("tamanho de requisição inválido")
                payload = json.loads(self._request_body(content_length))
                task = Task.from_dict(payload)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                self._json(400, {"error": "invalid_task", "message": str(error)})
                return
            try:
                result = get_task_executor().execute(task)
            except ValueError as error:
                self._json(409, {"error": "duplicate_task", "message": str(error)})
                return
            except Exception as error:
                logger.exception("Falha ao iniciar tarefa recebida pela API")
                self._json(503, {"error": "task_service_unavailable", "message": str(error)})
                return
            status_code = 504 if result.status == TaskStatus.TIMEOUT else (
                500 if result.status == TaskStatus.FAILED else 200
            )
            self._json(status_code, result.to_dict())

        def _handle_v2_result_delivery(self, package_id: str) -> None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError("tamanho de requisição inválido")
                payload = json.loads(self._request_body(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("corpo deve ser um objeto JSON")
                status = payload.get("status")
                if status not in {"delivered", "failed"}:
                    raise ValueError("status deve ser delivered ou failed")
                detail = payload.get("detail")
                if detail is not None and (not isinstance(detail, str) or len(detail) > 1_000):
                    raise ValueError("detail deve ser texto de até 1000 caracteres")
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                self._json(400, {"error": "invalid_delivery", "message": str(error)})
                return
            package = get_task_executor().record_result_delivery(
                package_id,
                status,
                detail=detail,
            )
            if package is None:
                self._json(404, {"error": "result_package_not_found"})
                return
            self._json(200, package["delivery"] | {"package_id": package["package_id"]})

        def _handle_operator_task(self) -> None:
            payload: Any = None
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                    raise ValueError("tamanho de requisição inválido")
                payload = json.loads(self._request_body(content_length))
                BrowserOperator.validate_task(payload, require_scope=True)
                task = payload
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                self._json(400, {
                    "task_id": payload.get("task_id") if isinstance(payload, dict) else None,
                    "status": "failed",
                    "action": None,
                    "result": None,
                    "evidence": [],
                    "error": {"code": "invalid_task", "message": str(error)},
                })
                return
            try:
                result = get_operator_executor().execute(task)
            except Exception:
                logger.exception("Falha ao executar tarefa do operador web V2")
                self._json(503, {
                    "task_id": task["task_id"],
                    "status": "failed",
                    "action": None,
                    "result": None,
                    "evidence": [],
                    "error": {
                        "code": "operator_unavailable",
                        "message": "O operador web V2 não conseguiu iniciar a tarefa",
                    },
                })
                return
            error_code = result.get("error", {}).get("code")
            status_code = (
                504 if error_code == "operation_timeout"
                else 409 if error_code == "duplicate_task"
                else 202 if result["status"] == "needs_human"
                else 500 if result["status"] == "failed"
                else 200
            )
            self._json(status_code, result)

        def log_message(self, format: str, *args: object) -> None:
            logger.info("API " + format, *args)

    return ThreadingHTTPServer((host, port), Handler)