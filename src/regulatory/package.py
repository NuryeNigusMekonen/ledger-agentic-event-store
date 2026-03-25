from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.models.events import StoredEvent


@dataclass(slots=True)
class RegulatoryPackageResult:
    application_id: str
    examination_date: datetime
    output_path: str | None
    package_hash: str
    package: dict[str, Any]


async def generate_regulatory_package(
    store: EventStore,
    application_id: str,
    examination_date: datetime,
    output_path: str | Path | None = None,
) -> RegulatoryPackageResult:
    related_events = await _load_related_events(store, application_id, examination_date)
    loan_stream_events = [
        event
        for event in related_events
        if event.stream_id == f"loan-{application_id}"
    ]

    projection_states = {
        "application_summary": _compute_application_summary(related_events, application_id),
        "compliance_audit": _compute_compliance_state(related_events, application_id),
        "agent_performance": _compute_agent_performance(related_events, application_id),
    }

    integrity_result = await _integrity_result_as_of(
        store=store,
        application_id=application_id,
        examination_date=examination_date,
        loan_stream_events=loan_stream_events,
    )

    narrative = [
        _narrate_event(event)
        for event in related_events
        if _is_significant(event.event_type)
    ]
    agent_model_metadata = _extract_agent_model_metadata(related_events, application_id)

    package = {
        "package_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "application_id": application_id,
        "examination_date": examination_date.isoformat(),
        "event_stream": [event.model_dump(mode="json") for event in related_events],
        "projection_states_at_examination": projection_states,
        "audit_chain_integrity": integrity_result,
        "narrative": narrative,
        "agent_model_metadata": agent_model_metadata,
        "verification": {
            "event_count": len(related_events),
            "global_positions": [event.global_position for event in related_events],
        },
    }

    package_hash = _canonical_hash(package)
    package["verification"]["package_hash_sha256"] = package_hash

    resolved_output_path: str | None = None
    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
        resolved_output_path = str(out)

    return RegulatoryPackageResult(
        application_id=application_id,
        examination_date=examination_date,
        output_path=resolved_output_path,
        package_hash=package_hash,
        package=package,
    )


async def _load_related_events(
    store: EventStore,
    application_id: str,
    examination_date: datetime,
) -> list[StoredEvent]:
    async with store._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              event_id,
              stream_id,
              stream_position,
              global_position,
              event_type,
              event_version,
              payload,
              metadata,
              recorded_at
            FROM events
            WHERE
              (stream_id = $1 OR payload->>'application_id' = $2)
              AND recorded_at <= $3
            ORDER BY global_position ASC
            """,
            f"loan-{application_id}",
            application_id,
            examination_date,
        )
    return [_row_to_stored_event(row) for row in rows]


async def _integrity_result_as_of(
    *,
    store: EventStore,
    application_id: str,
    examination_date: datetime,
    loan_stream_events: list[StoredEvent],
) -> dict[str, Any]:
    if not loan_stream_events:
        return {
            "chain_valid": False,
            "events_verified_count": 0,
            "final_hash": None,
            "violations": [{"reason": "loan_stream_missing"}],
        }

    max_position = max(event.stream_position for event in loan_stream_events)
    result = await run_integrity_check(
        store=store,
        stream_id=f"loan-{application_id}",
        from_position=1,
        to_position=max_position,
    )
    return {
        "chain_valid": result.chain_valid,
        "tamper_detected": result.tamper_detected,
        "events_verified_count": result.events_verified_count,
        "final_hash": result.final_hash,
        "violations": [
            {
                "stream_position": violation.stream_position,
                "event_id": violation.event_id,
                "reason": violation.reason,
                "expected_previous_hash": violation.expected_previous_hash,
                "actual_previous_hash": violation.actual_previous_hash,
                "expected_integrity_hash": violation.expected_integrity_hash,
                "actual_integrity_hash": violation.actual_integrity_hash,
            }
            for violation in result.violations
        ],
    }


def _compute_application_summary(events: list[StoredEvent], application_id: str) -> dict[str, Any]:
    state = {
        "application_id": application_id,
        "current_state": "UNKNOWN",
        "decision_recommendation": None,
        "final_decision": None,
        "requested_amount_usd": None,
        "approved_amount_usd": None,
    }
    for event in events:
        payload = event.payload
        if payload.get("application_id") != application_id:
            continue
        if event.event_type == "ApplicationSubmitted":
            state["current_state"] = "SUBMITTED"
            state["requested_amount_usd"] = payload.get("requested_amount_usd")
        elif event.event_type == "DecisionGenerated":
            recommendation = str(payload.get("recommendation", "")).upper()
            state["decision_recommendation"] = recommendation
            state["current_state"] = {
                "APPROVE": "APPROVED_PENDING_HUMAN",
                "DECLINE": "DECLINED_PENDING_HUMAN",
            }.get(recommendation, "PENDING_DECISION")
        elif event.event_type == "ApplicationApproved":
            state["current_state"] = "FINAL_APPROVED"
            state["final_decision"] = "APPROVE"
            state["approved_amount_usd"] = payload.get("approved_amount_usd")
        elif event.event_type == "ApplicationDeclined":
            state["current_state"] = "FINAL_DECLINED"
            state["final_decision"] = "DECLINE"
    return state


def _compute_compliance_state(events: list[StoredEvent], application_id: str) -> dict[str, Any]:
    mandatory: set[str] = set()
    passed: set[str] = set()
    failed: dict[str, str] = {}
    regulation_set_version = None

    for event in events:
        payload = event.payload
        if payload.get("application_id") != application_id:
            continue
        if event.event_type == "ComplianceCheckRequested":
            mandatory = set(str(v) for v in payload.get("checks_required", []))
            passed = set()
            failed = {}
            regulation_set_version = payload.get("regulation_set_version")
        elif event.event_type == "ComplianceRulePassed":
            rule_id = str(payload.get("rule_id", ""))
            if rule_id:
                passed.add(rule_id)
                failed.pop(rule_id, None)
        elif event.event_type == "ComplianceRuleFailed":
            rule_id = str(payload.get("rule_id", ""))
            if rule_id:
                failed[rule_id] = str(payload.get("failure_reason", ""))
                passed.discard(rule_id)

    status = "NOT_STARTED"
    if failed:
        status = "FAILED"
    elif mandatory and mandatory <= passed:
        status = "CLEARED"
    elif mandatory:
        status = "PENDING"

    return {
        "application_id": application_id,
        "regulation_set_version": regulation_set_version,
        "mandatory_checks": sorted(mandatory),
        "passed_checks": sorted(passed),
        "failed_checks": failed,
        "compliance_status": status,
    }


def _compute_agent_performance(events: list[StoredEvent], application_id: str) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload
        if payload.get("application_id") != application_id:
            continue
        if event.event_type not in {"CreditAnalysisCompleted", "FraudScreeningCompleted"}:
            continue

        agent_id = str(payload.get("agent_id", "unknown"))
        model_version = str(
            payload.get("model_version", payload.get("screening_model_version", "unknown"))
        )
        key = f"{agent_id}:{model_version}"
        entry = metrics.setdefault(
            key,
            {
                "agent_id": agent_id,
                "model_version": model_version,
                "credit_analyses": 0,
                "fraud_screenings": 0,
                "confidence_samples": [],
            },
        )
        if event.event_type == "CreditAnalysisCompleted":
            entry["credit_analyses"] += 1
            if payload.get("confidence_score") is not None:
                entry["confidence_samples"].append(float(payload["confidence_score"]))
        else:
            entry["fraud_screenings"] += 1

    result_rows: list[dict[str, Any]] = []
    for row in metrics.values():
        samples = row.pop("confidence_samples")
        row["avg_confidence_score"] = sum(samples) / len(samples) if samples else 0.0
        result_rows.append(row)
    return {"application_id": application_id, "agents": result_rows}


def _extract_agent_model_metadata(
    events: list[StoredEvent],
    application_id: str,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload
        if payload.get("application_id") != application_id:
            continue

        if event.event_type == "CreditAnalysisCompleted":
            extracted.append(
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "agent_id": payload.get("agent_id"),
                    "model_version": payload.get("model_version"),
                    "confidence_score": payload.get("confidence_score"),
                    "input_data_hash": payload.get("input_data_hash"),
                }
            )
        elif event.event_type == "FraudScreeningCompleted":
            extracted.append(
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "agent_id": payload.get("agent_id"),
                    "model_version": payload.get("screening_model_version"),
                    "confidence_score": payload.get("fraud_score"),
                    "input_data_hash": payload.get("input_data_hash"),
                }
            )
        elif event.event_type == "DecisionGenerated":
            model_versions = payload.get("model_versions", {})
            orchestrator = payload.get("orchestrator_agent_id")
            extracted.append(
                {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "agent_id": orchestrator,
                    "model_version": model_versions.get(orchestrator)
                    if isinstance(model_versions, dict)
                    else None,
                    "confidence_score": payload.get("confidence_score"),
                    "input_data_hash": None,
                }
            )
    return extracted


def _is_significant(event_type: str) -> bool:
    return event_type in {
        "ApplicationSubmitted",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
    }


def _narrate_event(event: StoredEvent) -> str:
    p = event.payload
    if event.event_type == "ApplicationSubmitted":
        return (
            f"Application {p.get('application_id')} was submitted for "
            f"{p.get('requested_amount_usd')} USD."
        )
    if event.event_type == "CreditAnalysisCompleted":
        return (
            f"Credit analysis by agent {p.get('agent_id')} produced risk tier "
            f"{p.get('risk_tier')} with confidence {p.get('confidence_score')}."
        )
    if event.event_type == "FraudScreeningCompleted":
        return (
            f"Fraud screening by agent {p.get('agent_id')} produced score "
            f"{p.get('fraud_score')}."
        )
    if event.event_type == "ComplianceCheckRequested":
        return (
            f"Compliance checks requested under regulation set "
            f"{p.get('regulation_set_version')}."
        )
    if event.event_type == "ComplianceRulePassed":
        return f"Compliance rule {p.get('rule_id')} passed."
    if event.event_type == "ComplianceRuleFailed":
        return f"Compliance rule {p.get('rule_id')} failed: {p.get('failure_reason')}."
    if event.event_type == "DecisionGenerated":
        return (
            f"Decision orchestrator recommended {p.get('recommendation')} with "
            f"confidence {p.get('confidence_score')}."
        )
    if event.event_type == "HumanReviewCompleted":
        return (
            f"Human reviewer {p.get('reviewer_id')} completed review with "
            f"decision {p.get('final_decision')}."
        )
    if event.event_type == "ApplicationApproved":
        return (
            f"Application approved for {p.get('approved_amount_usd')} USD at "
            f"interest rate {p.get('interest_rate')}."
        )
    if event.event_type == "ApplicationDeclined":
        return "Application was declined after review."
    return f"{event.event_type} occurred."


def _canonical_hash(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row_to_stored_event(row: asyncpg.Record) -> StoredEvent:
    return StoredEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=int(row["stream_position"]),
        global_position=int(row["global_position"]),
        event_type=row["event_type"],
        event_version=int(row["event_version"]),
        payload=dict(row["payload"]),
        metadata=dict(row["metadata"]),
        recorded_at=row["recorded_at"],
    )
