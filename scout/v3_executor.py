from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scout.task_executor import _browser_execution_lock, _safe_error_message
from scout.v3_browser import V3_CDP_ENDPOINT, WhatsAppWebAdapter
from scout.v3_models import V3Task, V3TaskType
from scout.v3_storage import V3StateStore

_OPT_OUT_PATTERN = re.compile(
    r"\b(?:stop|unsubscribe|sair|pare|parar|remova(?:-me)?|"
    r"n[aã]o\s+(?:me\s+)?(?:envie|mande|contate|contacte)|"
    r"n[aã]o\s+quero\s+(?:mais\s+)?(?:mensagens|contato|contacto))\b",
    re.IGNORECASE,
)


class V3TaskExecutor:
    def __init__(
        self,
        adapter: Any | None = None,
        state_store: V3StateStore | None = None,
        minimum_send_interval_seconds: int = 60,
        max_sends_per_hour: int = 10,
    ) -> None:
        if not 1 <= minimum_send_interval_seconds <= 86_400:
            raise ValueError("minimum_send_interval_seconds fora do intervalo permitido")
        if not 1 <= max_sends_per_hour <= 100:
            raise ValueError("max_sends_per_hour fora do intervalo permitido")
        self.adapter = adapter or WhatsAppWebAdapter()
        self.state_store = state_store or V3StateStore()
        self.minimum_send_interval_seconds = minimum_send_interval_seconds
        self.max_sends_per_hour = max_sends_per_hour
        self._lock = RLock()

    def execute(self, value: V3Task | dict[str, Any]) -> dict[str, Any]:
        task = value if isinstance(value, V3Task) else V3Task.from_dict(value)
        record: dict[str, Any] = {
            "task_id": task.task_id,
            "type": task.type.value,
            "status": "queued",
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "worker_pid": os.getpid(),
        }
        if not self.state_store.create_task(task.task_id, record):
            raise ValueError(f"task_id V3 já foi executado: {task.task_id}")
        with self._lock, _browser_execution_lock(V3_CDP_ENDPOINT):
            record["status"] = "running"
            self.state_store.save_task(task.task_id, record)
            try:
                result = self._run(task)
            except Exception as error:
                record["status"] = "failed"
                record["error"] = {
                    "code": "task_timeout" if isinstance(error, (TimeoutError, PlaywrightTimeoutError)) else "task_failed",
                    "message": _safe_error_message(error),
                }
            else:
                record["status"] = "completed"
                record["result"] = result
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.state_store.save_task(task.task_id, record)
            return self._copy(record)

    def _run(self, task: V3Task) -> dict[str, Any]:
        payload = task.payload
        if task.type == V3TaskType.RECORD_OPT_OUT:
            recipient = payload["recipient_id"].strip()
            reason = payload.get("reason", "Solicitou não receber contato")
            opt_out = self.state_store.record_opt_out(recipient, reason.strip())
            return {"recipient_id": recipient, "opted_out": True, "record": opt_out}

        recipient = payload.get("recipient_id")
        if recipient is not None and self.state_store.is_opted_out(recipient.strip()):
            raise PermissionError("Destinatário registrado em opt-out; operação bloqueada")

        if task.type == V3TaskType.READ_CONVERSATION:
            self._connect()
            conversation = self.adapter.read_conversation(
                payload["recipient_id"].strip(),
                payload.get("limit", 20),
            )
            opt_out_message = next((
                item for item in conversation.get("messages", [])
                if isinstance(item, dict)
                and item.get("outgoing") is False
                and isinstance(item.get("text"), str)
                and _OPT_OUT_PATTERN.search(item["text"])
            ), None)
            if opt_out_message is not None:
                self.state_store.record_opt_out(
                    payload["recipient_id"].strip(),
                    "Solicitação de não contato detectada na conversa",
                )
                conversation["opt_out_detected"] = True
            else:
                conversation["opt_out_detected"] = False
            return conversation
        if task.type == V3TaskType.PREPARE_MESSAGE:
            recipient = payload["recipient_id"].strip()
            body = payload["body"].strip()
            if self.state_store.has_duplicate(recipient, body):
                raise ValueError("Mensagem duplicada para este destinatário; requer revisão humana")
            message_id = str(uuid.uuid4())
            approval_id = str(uuid.uuid4())
            draft = {
                "message_id": message_id,
                "approval_id": approval_id,
                "recipient_id": recipient,
                "body": body,
                "consent_source": payload["consent_source"].strip(),
                "status": "draft",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sent_at": None,
            }
            if not self.state_store.create_message(message_id, draft):
                raise RuntimeError("Não foi possível reservar o rascunho V3")
            return draft
        if task.type == V3TaskType.SEND_MESSAGE:
            return self._send(payload)
        raise ValueError(f"Tipo de tarefa V3 não suportado: {task.type.value}")

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        message_id = payload["message_id"]
        draft = self.state_store.get_message(message_id)
        if not draft or draft.get("status") != "draft":
            raise ValueError("Rascunho ausente, já utilizado ou não está aguardando aprovação")
        if payload["approval_id"] != draft.get("approval_id"):
            raise PermissionError("A aprovação não corresponde ao rascunho")
        recipient = draft["recipient_id"]
        if self.state_store.is_opted_out(recipient):
            raise PermissionError("Destinatário registrado em opt-out; envio bloqueado")
        history = self.state_store.message_history(recipient)
        duplicate_sent = any(
            item.get("message_id") != message_id
            and item.get("body") == draft["body"]
            and item.get("status") in {"sending", "sent", "send_uncertain"}
            for item in history
        )
        if duplicate_sent:
            raise ValueError("Conteúdo já enviado ou em estado incerto para este destinatário")
        recent_sends = self.state_store.sent_since(3_600)
        if len(recent_sends) >= self.max_sends_per_hour:
            raise PermissionError("Limite horário de envio atingido; requer intervenção humana")
        latest = self.state_store.latest_send_time()
        if latest:
            sent_at = datetime.fromisoformat(latest)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - sent_at).total_seconds()
            if elapsed < self.minimum_send_interval_seconds:
                raise PermissionError("Intervalo mínimo entre envios ainda não foi atingido")

        self._connect()
        self.state_store.update_message(message_id, {
            "status": "sending",
            "send_started_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            sent = self.adapter.send_message(recipient, draft["body"])
        except Exception as error:
            self.state_store.update_message(message_id, {
                "status": "send_uncertain",
                "send_error": _safe_error_message(error),
            })
            raise RuntimeError(
                "O envio pode ter sido acionado, mas não foi confirmado; não repetir automaticamente"
            ) from error
        if not isinstance(sent, dict) or sent.get("status") != "sent_confirmed":
            self.state_store.update_message(message_id, {"status": "send_uncertain"})
            raise RuntimeError("Envio não confirmado; não repetir automaticamente")
        updated = self.state_store.update_message(message_id, {
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "confirmation": sent,
        })
        return updated

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        record = self.state_store.get_task(task_id)
        if (
            record is not None
            and record.get("status") in {"queued", "running"}
            and not self._process_is_alive(record.get("worker_pid"))
        ):
            record["status"] = "failed"
            record["error"] = {
                "code": "task_interrupted",
                "message": "Tarefa interrompida; não foi repetida automaticamente",
            }
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.state_store.save_task(task_id, record)
        return record

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self.state_store.get_message(message_id)

    def message_history(self, recipient_id: str) -> list[dict[str, Any]]:
        return self.state_store.message_history(recipient_id)

    def recent_messages(self) -> list[dict[str, Any]]:
        return self.state_store.message_history_for_all()

    def health_check(self) -> dict[str, Any]:
        health_reader = getattr(self.adapter, "health_check", None)
        if health_reader is None:
            return {
                "browser_connected": False,
                "whatsapp_available": False,
                "cdp_endpoint": V3_CDP_ENDPOINT,
                "error": "O adaptador não oferece health check de navegador",
            }
        health = health_reader()
        return health if isinstance(health, dict) else {
            "browser_connected": False,
            "whatsapp_available": False,
            "cdp_endpoint": V3_CDP_ENDPOINT,
            "error": "O health check retornou formato inválido",
        }

    def _connect(self) -> None:
        connect = getattr(self.adapter, "connect", None)
        if connect is not None:
            connect()

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

    @staticmethod
    def _copy(value: dict[str, Any]) -> dict[str, Any]:
        import json

        return json.loads(json.dumps(value, ensure_ascii=False))