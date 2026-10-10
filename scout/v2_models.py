from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskType(str, Enum):
    SEARCH_LEADS = "SEARCH_LEADS"
    OPEN_COMPANY = "OPEN_COMPANY"
    GENERATE_SITE = "GENERATE_SITE"
    HEALTH_CHECK = "HEALTH_CHECK"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} deve ser um texto não vazio")
    return value.strip()


@dataclass(slots=True)
class V2Lead:
    company_name: str = ""
    category: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    website: str | None = None
    instagram: str | None = None
    source: str = "AIVIO"
    source_url: str | None = None
    company_url: str | None = None
    has_website: bool | None = None
    notes: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=utc_now)
    lead_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "category": self.category,
            "phone": self.phone,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "website": self.website,
            "instagram": self.instagram,
            "source": self.source,
            "source_url": self.source_url,
            "company_url": self.company_url,
            "has_website": self.has_website,
            "notes": self.notes,
            "raw_data": self.raw_data,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> V2Lead:
        company_name = data.get("company_name", "")
        if not isinstance(company_name, str):
            raise ValueError("lead.company_name deve ser texto")
        company_name = " ".join(company_name.split())
        text_fields = (
            "category", "phone", "address", "city", "state", "website", "instagram",
            "source", "source_url", "company_url", "notes", "collected_at", "lead_id",
        )
        values: dict[str, Any] = {}
        for name in text_fields:
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"lead.{name} deve ser texto ou null")
            values[name] = value
        has_website = data.get("has_website")
        if has_website is not None and not isinstance(has_website, bool):
            raise ValueError("lead.has_website deve ser booleano ou null")
        raw_data = data.get("raw_data", {})
        if not isinstance(raw_data, dict):
            raise ValueError("lead.raw_data deve ser um objeto")
        values["raw_data"] = raw_data
        values["has_website"] = has_website
        if values.get("lead_id") is None:
            values.pop("lead_id")
        elif values["lead_id"]:
            try:
                values["lead_id"] = str(uuid.UUID(values["lead_id"]))
            except ValueError as error:
                raise ValueError("lead.lead_id deve ser um UUID válido") from error
        if values.get("collected_at") is None:
            values.pop("collected_at")
        if values.get("source") is None:
            values["source"] = "AIVIO"
        return cls(company_name=company_name, **values)


@dataclass(slots=True)
class SearchLeadsPayload:
    city: str
    state: str
    category: str
    quantity: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    objective: str = ""
    inclusion_criteria: list[str] = field(default_factory=list)
    exclusion_criteria: list[str] = field(default_factory=list)
    fields_of_interest: list[str] = field(default_factory=lambda: [
        "company_name", "phone", "address", "website", "instagram", "source_url",
    ])


@dataclass(slots=True)
class LeadTaskPayload:
    lead: V2Lead


@dataclass(slots=True)
class HealthCheckPayload:
    pass


TaskPayload = SearchLeadsPayload | LeadTaskPayload | HealthCheckPayload


@dataclass(slots=True)
class Task:
    task_id: str
    type: TaskType
    payload: TaskPayload
    created_at: str = field(default_factory=utc_now)
    authorization: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> Task:
        if not isinstance(data, dict):
            raise ValueError("task deve ser um objeto JSON")
        task_id = _required_text(data.get("task_id"), "task_id")
        try:
            uuid.UUID(task_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("task_id deve ser um UUID válido") from error
        try:
            task_type = TaskType(data.get("type"))
        except (ValueError, TypeError) as error:
            raise ValueError("type deve ser SEARCH_LEADS, OPEN_COMPANY, GENERATE_SITE ou HEALTH_CHECK") from error
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload deve ser um objeto")
        created_at = data.get("created_at", utc_now())
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValueError("created_at deve ser uma data ISO-8601")
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("created_at deve ser uma data ISO-8601") from error
        authorization = data.get("authorization", {})
        if not isinstance(authorization, dict):
            raise ValueError("authorization deve ser um objeto")
        allowed_authorizations = {
            "allow_credit_consumption", "allow_external_effects", "authorized_by",
        }
        if set(authorization) - allowed_authorizations:
            raise ValueError("authorization contém permissões desconhecidas")
        if any(
            not isinstance(value, bool)
            for name, value in authorization.items() if name != "authorized_by"
        ):
            raise ValueError("permissões authorization devem ser booleanas")
        authorized_by = authorization.get("authorized_by")
        if authorized_by is not None and (
            not isinstance(authorized_by, str) or authorized_by.strip().casefold() != "auren"
        ):
            raise ValueError("authorized_by deve identificar Auren")
        if isinstance(authorized_by, str):
            authorization["authorized_by"] = "auren"
        typed_payload = cls._validate_payload(task_type, payload)
        cls._validate_authorization(task_type, payload, authorization)
        return cls(
            task_id=task_id,
            type=task_type,
            payload=typed_payload,
            created_at=created_at,
            authorization=dict(authorization),
        )

    @staticmethod
    def _validate_authorization(
        task_type: TaskType,
        payload: dict[str, Any],
        authorization: dict[str, bool],
    ) -> None:
        required: set[str] = set()
        if task_type == TaskType.SEARCH_LEADS:
            required.add("allow_credit_consumption")
        elif task_type == TaskType.GENERATE_SITE:
            required.update({"allow_credit_consumption", "allow_external_effects"})
        elif task_type == TaskType.OPEN_COMPANY:
            lead = payload.get("lead")
            if isinstance(lead, dict) and not (lead.get("company_url") or lead.get("source_url")):
                required.add("allow_credit_consumption")
        missing = sorted(name for name in required if authorization.get(name) is not True)
        if missing:
            permissions = ", ".join(missing)
            raise ValueError(f"authorization explícita necessária: {permissions}")
        if task_type == TaskType.SEARCH_LEADS and authorization.get("authorized_by") != "auren":
            raise ValueError("authorization explícita do Auren necessária para iniciar um lote")

    @staticmethod
    def _validate_payload(task_type: TaskType, payload: dict[str, Any]) -> TaskPayload:
        if task_type == TaskType.SEARCH_LEADS:
            for name in ("city", "state", "category"):
                validated = _required_text(payload.get(name), f"payload.{name}")
                payload[name] = payload[name] if name == "city" else validated
            quantity = payload.get("quantity", 10)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10:
                raise ValueError("payload.quantity deve ser um inteiro entre 1 e 10")
            filters = payload.get("filters", {})
            if not isinstance(filters, dict):
                raise ValueError("payload.filters deve ser um objeto")
            if "has_website" in filters and not isinstance(filters["has_website"], bool):
                raise ValueError("payload.filters.has_website deve ser booleano")
            objective = payload.get("objective", "")
            if not isinstance(objective, str) or len(objective) > 500:
                raise ValueError("payload.objective deve ser texto com até 500 caracteres")
            criteria = {}
            for name in ("inclusion_criteria", "exclusion_criteria"):
                values = payload.get(name, [])
                if (
                    not isinstance(values, list)
                    or len(values) > 20
                    or any(
                        not isinstance(item, str) or not item.strip() or len(item) > 300
                        for item in values
                    )
                ):
                    raise ValueError(f"payload.{name} deve ser uma lista de até 20 textos não vazios")
                criteria[name] = list(dict.fromkeys(item.strip() for item in values))
            allowed_fields = {
                "company_name", "phone", "address", "website", "instagram", "source_url",
                "company_url", "city", "state", "category", "has_website",
            }
            fields_of_interest = payload.get(
                "fields_of_interest",
                ["company_name", "phone", "address", "website", "instagram", "source_url"],
            )
            if (
                not isinstance(fields_of_interest, list)
                or not fields_of_interest
                or len(fields_of_interest) > len(allowed_fields)
                or any(item not in allowed_fields for item in fields_of_interest)
            ):
                raise ValueError("payload.fields_of_interest contém campos inválidos")
            return SearchLeadsPayload(
                city=payload["city"],
                state=payload["state"],
                category=payload["category"],
                quantity=quantity,
                filters=filters,
                objective=objective.strip(),
                inclusion_criteria=criteria["inclusion_criteria"],
                exclusion_criteria=criteria["exclusion_criteria"],
                fields_of_interest=list(dict.fromkeys(fields_of_interest)),
            )
        elif task_type in {TaskType.OPEN_COMPANY, TaskType.GENERATE_SITE}:
            if not isinstance(payload.get("lead"), dict):
                raise ValueError("payload.lead deve ser um objeto")
            _required_text(payload["lead"].get("company_name"), "payload.lead.company_name")
            return LeadTaskPayload(lead=V2Lead.from_dict(payload["lead"]))
        elif payload:
            raise ValueError("HEALTH_CHECK aceita payload vazio")
        return HealthCheckPayload()


@dataclass(slots=True)
class TaskResult:
    task_id: str
    type: TaskType
    status: TaskStatus
    data: dict[str, Any] = field(default_factory=dict)
    leads: list[V2Lead] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    result_package: dict[str, Any] | None = None
    worker_pid: int | None = None
    error: dict[str, str] | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        leads = [lead.to_dict() for lead in self.leads]
        result = {
            "data": self.data,
            "leads": leads,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
            "progress": self.progress,
            "progress_events": self.progress_events,
            "result_package": self.result_package,
            "worker_pid": self.worker_pid,
        }
        return {
            "task_id": self.task_id,
            "type": self.type.value,
            "status": self.status.value,
            "result": result,
            "error": self.error,
            "data": self.data,
            "leads": leads,
            "artifacts": self.artifacts,
            "errors": self.errors,
            "warnings": self.warnings,
            "progress": self.progress,
            "progress_events": self.progress_events,
            "result_package": self.result_package,
            "worker_pid": self.worker_pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
