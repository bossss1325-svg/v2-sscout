from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any


class V3TaskType(str, Enum):
    READ_CONVERSATION = "READ_CONVERSATION"
    PREPARE_MESSAGE = "PREPARE_MESSAGE"
    SEND_MESSAGE = "SEND_MESSAGE"
    RECORD_OPT_OUT = "RECORD_OPT_OUT"


@dataclass(slots=True)
class V3Task:
    task_id: str
    type: V3TaskType
    payload: dict[str, Any]
    authorization: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> V3Task:
        if not isinstance(value, dict):
            raise ValueError("task deve ser um objeto JSON")
        task_id = value.get("task_id")
        try:
            uuid.UUID(task_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("task_id deve ser um UUID válido") from error
        try:
            task_type = V3TaskType(value.get("type"))
        except (ValueError, TypeError) as error:
            raise ValueError("type V3 inválido") from error
        payload = value.get("payload", {})
        authorization = value.get("authorization", {})
        if not isinstance(payload, dict) or not isinstance(authorization, dict):
            raise ValueError("payload e authorization devem ser objetos")
        if authorization.get("authorized_by") != "auren":
            raise ValueError("autorização explícita do Auren necessária")
        allowed = {
            "authorized_by", "consent_verified", "allow_external_effects", "human_approved",
        }
        if set(authorization) - allowed:
            raise ValueError("authorization contém campos desconhecidos")
        if any(
            not isinstance(item, bool)
            for name, item in authorization.items() if name != "authorized_by"
        ):
            raise ValueError("permissões V3 devem ser booleanas")
        payload_fields = {
            V3TaskType.READ_CONVERSATION: {"recipient_id", "limit"},
            V3TaskType.PREPARE_MESSAGE: {"recipient_id", "body", "consent_source"},
            V3TaskType.SEND_MESSAGE: {"message_id", "approval_id"},
            V3TaskType.RECORD_OPT_OUT: {"recipient_id", "reason"},
        }[task_type]
        if set(payload) - payload_fields:
            raise ValueError("payload contém campos desconhecidos para esta tarefa V3")
        cls._validate_payload(task_type, payload, authorization)
        return cls(str(uuid.UUID(task_id)), task_type, dict(payload), dict(authorization))

    @staticmethod
    def _validate_payload(
        task_type: V3TaskType,
        payload: dict[str, Any],
        authorization: dict[str, Any],
    ) -> None:
        if task_type in {
            V3TaskType.READ_CONVERSATION,
            V3TaskType.PREPARE_MESSAGE,
            V3TaskType.RECORD_OPT_OUT,
        }:
            recipient_id = payload.get("recipient_id")
            if not isinstance(recipient_id, str) or not recipient_id.strip():
                raise ValueError("payload.recipient_id deve ser texto não vazio")
        if task_type == V3TaskType.READ_CONVERSATION:
            limit = payload.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise ValueError("payload.limit deve ser um inteiro entre 1 e 50")
        elif task_type == V3TaskType.PREPARE_MESSAGE:
            body = payload.get("body")
            source = payload.get("consent_source")
            if not isinstance(body, str) or not body.strip() or len(body) > 2_000:
                raise ValueError("payload.body deve conter de 1 a 2000 caracteres")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("payload.consent_source deve descrever a origem do consentimento")
            if len(source) > 500:
                raise ValueError("payload.consent_source excede 500 caracteres")
            if authorization.get("consent_verified") is not True:
                raise ValueError("consentimento verificável necessário para preparar mensagem")
        elif task_type == V3TaskType.SEND_MESSAGE:
            for name in ("message_id", "approval_id"):
                item = payload.get(name)
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"payload.{name} deve ser texto não vazio")
            if authorization.get("allow_external_effects") is not True:
                raise ValueError("allow_external_effects explícito necessário")
            if authorization.get("human_approved") is not True:
                raise ValueError("confirmação humana explícita necessária para envio")
        elif task_type == V3TaskType.RECORD_OPT_OUT:
            reason = payload.get("reason", "Solicitou não receber contato")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                raise ValueError("payload.reason deve ser texto não vazio")