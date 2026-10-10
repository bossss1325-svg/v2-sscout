from __future__ import annotations

import uuid
import re
from datetime import datetime, timezone
from typing import Any

from scout.v2_models import SearchLeadsPayload, Task, TaskResult, TaskType, V2Lead

PACKAGE_SCHEMA_VERSION = 1
_TRACKED_LEAD_FIELDS = (
    "company_name", "phone", "address", "website", "instagram", "city", "state",
    "category", "source_url", "company_url", "has_website",
)


def build_result_package(task: Task, result: TaskResult) -> dict[str, Any]:
    search_payload: SearchLeadsPayload | None = None
    package_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"auren-scout-v2:result:{task.task_id}",
    ))
    if task.type == TaskType.SEARCH_LEADS:
        if not isinstance(task.payload, SearchLeadsPayload):
            raise TypeError("SEARCH_LEADS exige SearchLeadsPayload")
        search_payload = task.payload
        objective = search_payload.objective or (
            f"Pesquisar {search_payload.category} em {search_payload.city}, {search_payload.state}"
        )
        criteria = {
            "include": list(search_payload.inclusion_criteria),
            "exclude": list(search_payload.exclusion_criteria),
        }
        niche = search_payload.category
        locality = {"city": search_payload.city, "state": search_payload.state}
        requested_quantity = search_payload.quantity
        fields_of_interest = list(search_payload.fields_of_interest)
    else:
        objective = task.type.value
        criteria = {"include": [], "exclude": []}
        niche = None
        locality = None
        requested_quantity = len(result.leads)
        fields_of_interest = list(_TRACKED_LEAD_FIELDS)

    include_criteria = search_payload.inclusion_criteria if search_payload is not None else []
    exclude_criteria = search_payload.exclusion_criteria if search_payload is not None else []
    lead_records = [
        _lead_record(lead, fields_of_interest, include_criteria, exclude_criteria)
        for lead in result.leads
    ]
    excluded_records = [
        _lead_record(V2Lead.from_dict(item), fields_of_interest, include_criteria, exclude_criteria)
        for item in result.data.get("excluded_results", [])
        if isinstance(item, dict)
    ]
    duplicates = []
    for lead in result.leads:
        duplicate_sources = lead.raw_data.get("duplicates", [])
        if not isinstance(duplicate_sources, list):
            continue
        for duplicate in duplicate_sources:
            if isinstance(duplicate, dict):
                duplicate_search = duplicate.get("search_result", {})
                if not isinstance(duplicate_search, dict):
                    duplicate_search = {}
                duplicates.append({
                    "company_name": duplicate_search.get("title"),
                    "source_url": duplicate_search.get("source_url") or duplicate_search.get("url"),
                    "deduplicated_to": lead.lead_id,
                })
    duplicates.extend(result.data.get("duplicates", []))
    package = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_id,
        "task_id": task.task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution": {
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "status": result.status.value,
        },
        "objective": objective,
        "niche": niche,
        "locality": locality,
        "criteria": criteria,
        "fields_of_interest": fields_of_interest,
        "requested_quantity": requested_quantity,
        "found_count": len(lead_records),
        "results": lead_records,
        "excluded_results": excluded_records,
        "duplicates": duplicates,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "limitations": list(result.data.get("limitations", [])),
        "summary": _human_summary(
            task_id=task.task_id,
            objective=objective,
            status=result.status.value,
            found_count=len(lead_records),
            requested_quantity=requested_quantity,
            results=lead_records,
            warnings=result.warnings,
            errors=result.errors,
        ),
        "delivery": {
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "delivered_at": None,
            "error": None,
        },
    }
    validate_result_package(package)
    return package


def build_operator_result_package(
    task: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    task_id = task["task_id"]
    package_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"auren-scout-v2:result:{task_id}"))
    actions = task.get("actions", [])
    objective = task.get("goal") or "Operação de navegador solicitada pelo Auren"
    errors = []
    if isinstance(result.get("error"), dict):
        errors.append(result["error"].get("message", "Falha sem detalhe"))
    research = result.get("research")
    lead_records: list[dict[str, Any]] = []
    excluded_records: list[dict[str, Any]] = []
    if isinstance(research, dict):
        leads = [V2Lead.from_dict(item) for item in result.get("leads", []) if isinstance(item, dict)]
        lead_records = [
            _lead_record(
                lead,
                research["fields_of_interest"],
                research["inclusion_criteria"],
                research["exclusion_criteria"],
            )
            for lead in leads
        ]
        excluded_records = [
            _lead_record(
                V2Lead.from_dict(item),
                research["fields_of_interest"],
                research["inclusion_criteria"],
                research["exclusion_criteria"],
            )
            for item in research.get("excluded_results", [])
            if isinstance(item, dict)
        ]
    requested_quantity = research.get("quantity", 0) if isinstance(research, dict) else 0
    result_data = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": package_id,
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution": {
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "status": result.get("status", "failed"),
        },
        "objective": objective,
        "niche": research.get("niche") if isinstance(research, dict) else None,
        "locality": research.get("locality") if isinstance(research, dict) else None,
        "criteria": {
            "include": research.get("inclusion_criteria", []) if isinstance(research, dict) else [],
            "exclude": research.get("exclusion_criteria", []) if isinstance(research, dict) else [],
        },
        "fields_of_interest": research.get("fields_of_interest", []) if isinstance(research, dict) else [],
        "requested_quantity": requested_quantity,
        "found_count": len(lead_records),
        "results": lead_records,
        "excluded_results": excluded_records,
        "duplicates": research.get("duplicates", []) if isinstance(research, dict) else [],
        "errors": errors,
        "warnings": result.get("warnings", []),
        "limitations": [
            "A pesquisa genérica só extrai records semânticos article/listitem observados na página atual.",
            "Resultados são limitados à página observada; a paginação exige ações explícitas adicionais.",
        ] if isinstance(research, dict) else [],
        "operator_result": {
            "requested_actions": actions,
            "operations": result.get("operations", []),
            "evidence": result.get("evidence", []),
            "results": result.get("result", []),
        },
        "summary": _human_summary(
            task_id=task_id,
            objective=objective,
            status=result.get("status", "failed"),
            found_count=len(lead_records),
            requested_quantity=requested_quantity,
            results=lead_records,
            warnings=result.get("warnings", []),
            errors=errors,
        ) if isinstance(research, dict) else _operator_summary(objective, result, actions),
        "delivery": {
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
            "delivered_at": None,
            "error": None,
        },
    }
    validate_result_package(result_data)
    return result_data


def validate_result_package(package: Any) -> dict[str, Any]:
    if not isinstance(package, dict):
        raise ValueError("result_package deve ser um objeto")
    required = {
        "schema_version", "package_id", "task_id", "created_at", "execution",
        "objective", "criteria", "requested_quantity", "found_count", "results",
        "duplicates", "errors", "warnings", "limitations", "summary", "delivery",
    }
    missing = required - set(package)
    if missing:
        raise ValueError(f"result_package ausente: {', '.join(sorted(missing))}")
    if package["schema_version"] != PACKAGE_SCHEMA_VERSION:
        raise ValueError("schema_version de result_package não suportado")
    for name in ("package_id", "task_id"):
        try:
            uuid.UUID(package[name])
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError(f"result_package.{name} deve ser UUID") from error
    if not isinstance(package["results"], list) or package["found_count"] != len(package["results"]):
        raise ValueError("found_count deve corresponder à lista results")
    if isinstance(package["requested_quantity"], bool) or not isinstance(package["requested_quantity"], int):
        raise ValueError("requested_quantity deve ser inteiro")
    if package["requested_quantity"] < 0 or package["requested_quantity"] > 10:
        raise ValueError("requested_quantity deve estar entre 0 e 10")
    if package["found_count"] > package["requested_quantity"]:
        raise ValueError("found_count não pode superar requested_quantity")
    if not isinstance(package["summary"], str) or not package["summary"].strip():
        raise ValueError("summary deve ser texto não vazio")
    delivery = package["delivery"]
    if not isinstance(delivery, dict) or delivery.get("status") not in {"pending", "delivered", "failed"}:
        raise ValueError("delivery.status inválido")
    return package


def _lead_record(
    lead: V2Lead,
    fields_of_interest: list[str],
    inclusion_criteria: list[str],
    exclusion_criteria: list[str],
) -> dict[str, Any]:
    source_url = lead.source_url or lead.company_url
    raw_search = lead.raw_data.get("search_result", {})
    if not isinstance(raw_search, dict):
        raw_search = {}
    raw_details = lead.raw_data.get("company_details", {})
    if not isinstance(raw_details, dict):
        raw_details = {}
    observed_text = raw_details.get("text") or raw_search.get("text")
    evidence = [{
        "source": lead.source,
        "source_url": source_url,
        "observed_at": lead.collected_at,
        "observed_text": observed_text if isinstance(observed_text, str) else None,
        "links": raw_search.get("links", []) if isinstance(raw_search.get("links", []), list) else [],
    }]
    observed_fields = {
        "company_name": lead.company_name,
        "phone": lead.phone,
        "address": lead.address,
        "website": lead.website,
        "instagram": lead.instagram,
        "city": lead.city,
        "state": lead.state,
        "category": lead.category,
        "source_url": source_url,
        "company_url": lead.company_url,
        "has_website": lead.has_website,
    }
    field_evidence = {
        field_name: [{
            "source": lead.source,
            "source_url": source_url,
            "observed_at": lead.collected_at,
            "observed_text": observed_text if isinstance(observed_text, str) else None,
        }] if observed_fields.get(field_name) is not None else []
        for field_name in fields_of_interest
    }
    absent = [
        field_name for field_name in fields_of_interest
        if field_name in _TRACKED_LEAD_FIELDS and not getattr(lead, field_name, None)
    ]
    searchable_text = " ".join(filter(None, [
        lead.company_name,
        lead.address,
        lead.city,
        lead.state,
        lead.category,
        observed_text if isinstance(observed_text, str) else None,
    ])).casefold()
    criteria_result = evaluate_lead_criteria(
        searchable_text,
        inclusion_criteria,
        exclusion_criteria,
    )
    return {
        **lead.to_dict(),
        "source_url": source_url,
        "evidence": evidence,
        "field_evidence": field_evidence,
        "missing_or_unverified": absent,
        "qualification": {
            "status": criteria_result["status"],
            "reason": "Critérios avaliados somente contra conteúdo observado; ausência de correspondência não prova a negativa.",
            "criteria_unverified": criteria_result["criteria_unverified"],
            "inclusion_evidence": criteria_result["inclusion_evidence"],
            "exclusion_evidence": criteria_result["exclusion_evidence"],
        },
    }


def evaluate_lead_criteria(
    searchable_text: str,
    inclusion_criteria: list[str],
    exclusion_criteria: list[str],
) -> dict[str, Any]:
    normalized = searchable_text.casefold()
    inclusion_evidence = [_criterion_evidence(normalized, item) for item in inclusion_criteria]
    exclusion_evidence = [_criterion_evidence(normalized, item) for item in exclusion_criteria]
    excluded = any(item["matched"] for item in exclusion_evidence)
    qualified = bool(inclusion_evidence) and all(
        item["matched"] for item in inclusion_evidence
    )
    return {
        "status": "excluded" if excluded else (
            "qualified" if qualified else "source_returned" if not inclusion_evidence else "unverified"
        ),
        "criteria_unverified": any(not item["matched"] for item in inclusion_evidence),
        "inclusion_evidence": inclusion_evidence,
        "exclusion_evidence": exclusion_evidence,
    }


def _criterion_evidence(normalized_text: str, criterion: str) -> dict[str, Any]:
    normalized_criterion = criterion.casefold()
    position = normalized_text.find(normalized_criterion)
    observed = position >= 0
    prefix = normalized_text[max(0, position - 48):position] if observed else ""
    negated = bool(re.search(r"\b(?:não|nao|not|never|sem)\b[^.!?]{0,40}$", prefix, re.I))
    return {
        "criterion": criterion,
        "matched": observed and not negated,
        "observed": observed,
        "negated": negated,
    }


def _human_summary(
    *,
    task_id: str,
    objective: str,
    status: str,
    found_count: int,
    requested_quantity: int,
    results: list[dict[str, Any]],
    warnings: list[str],
    errors: list[str],
) -> str:
    lines = [
        f"Tarefa V2: {task_id}",
        f"Objetivo: {objective}",
        f"Estado: {status}",
        f"Resultados: {found_count}/{requested_quantity}",
    ]
    for index, lead in enumerate(results, start=1):
        missing = lead["missing_or_unverified"]
        lines.append(
            f"{index}. {lead.get('company_name') or 'Nome não verificado'}"
            f" | origem: {lead.get('source_url') or 'URL ausente'}"
            f" | ausente/não verificado: {', '.join(missing) if missing else 'nenhum campo solicitado'}"
        )
    if warnings:
        lines.append("Avisos: " + " | ".join(warnings))
    if errors:
        lines.append("Erros: " + " | ".join(errors))
    return "\n".join(lines)


def _operator_summary(objective: str, result: dict[str, Any], actions: list[Any]) -> str:
    operations = result.get("operations", [])
    completed = sum(
        isinstance(item, dict) and item.get("status") == "completed"
        for item in operations
    )
    lines = [
        f"Tarefa de navegador: {result.get('task_id', '')}",
        f"Objetivo: {objective}",
        f"Estado: {result.get('status', 'failed')}",
        f"Ações concluídas: {completed}/{len(actions)}",
    ]
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        lines.append(f"Erro: {error['message']}")
    return "\n".join(lines)
