from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg

from src.event_store import EventStore
from src.models.events import BaseEvent, StoredEvent


@dataclass(slots=True)
class DivergenceEvent:
    event_id: str
    event_type: str
    stream_id: str
    global_position: int
    reason: str


@dataclass(slots=True)
class WhatIfResult:
    application_id: str
    branch_at_event_type: str
    branch_event_id: str
    branch_stream_id: str
    real_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any]
    divergence_events: list[DivergenceEvent]
    simulated_event_count: int


async def run_what_if(
    store: EventStore,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[BaseEvent],
    projections: list[Any] | None = None,
) -> WhatIfResult:
    """Run in-memory counterfactual replay without writing to store."""
    if not counterfactual_events:
        raise ValueError("counterfactual_events must not be empty.")

    related_events = await _load_related_events(store, application_id)
    if not related_events:
        raise ValueError(f"No events found for application '{application_id}'.")

    branch_event = next(
        (event for event in related_events if event.event_type == branch_at_event_type),
        None,
    )
    if branch_event is None:
        raise ValueError(
            "Branch event type "
            f"'{branch_at_event_type}' not found for application '{application_id}'."
        )

    branch_ids = {str(branch_event.event_id)}
    dependent_memo: dict[str, bool] = {}
    id_lookup = {str(event.event_id): event for event in related_events}

    pre_branch = [
        event
        for event in related_events
        if event.global_position < branch_event.global_position
    ]
    post_branch = [
        event
        for event in related_events
        if event.global_position > branch_event.global_position
    ]

    kept_post: list[StoredEvent] = []
    divergences: list[DivergenceEvent] = [
        DivergenceEvent(
            event_id=str(branch_event.event_id),
            event_type=branch_event.event_type,
            stream_id=branch_event.stream_id,
            global_position=branch_event.global_position,
            reason="replaced_by_counterfactual",
        )
    ]

    for event in post_branch:
        if _is_dependent(
            event=event,
            branch_event=branch_event,
            branch_ids=branch_ids,
            id_lookup=id_lookup,
            dependent_memo=dependent_memo,
        ):
            divergences.append(
                DivergenceEvent(
                    event_id=str(event.event_id),
                    event_type=event.event_type,
                    stream_id=event.stream_id,
                    global_position=event.global_position,
                    reason="causally_dependent_on_branch",
                )
            )
            continue
        kept_post.append(event)

    injected = _inject_counterfactual_events(
        counterfactual_events=counterfactual_events,
        branch_event=branch_event,
        application_id=application_id,
    )
    simulated_events = [*pre_branch, *injected, *kept_post]

    real_outcome = _compute_outcome(
        related_events,
        projections=projections,
        application_id=application_id,
    )
    counterfactual_outcome = _compute_outcome(
        simulated_events,
        projections=projections,
        application_id=application_id,
    )

    return WhatIfResult(
        application_id=application_id,
        branch_at_event_type=branch_at_event_type,
        branch_event_id=str(branch_event.event_id),
        branch_stream_id=branch_event.stream_id,
        real_outcome=real_outcome,
        counterfactual_outcome=counterfactual_outcome,
        divergence_events=divergences,
        simulated_event_count=len(simulated_events),
    )


async def _load_related_events(store: EventStore, application_id: str) -> list[StoredEvent]:
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
            WHERE stream_id = $1 OR payload->>'application_id' = $2
            ORDER BY global_position ASC
            """,
            f"loan-{application_id}",
            application_id,
        )
    return [_row_to_stored_event(row) for row in rows]


def _is_dependent(
    *,
    event: StoredEvent,
    branch_event: StoredEvent,
    branch_ids: set[str],
    id_lookup: dict[str, StoredEvent],
    dependent_memo: dict[str, bool],
) -> bool:
    event_id = str(event.event_id)
    if event_id in dependent_memo:
        return dependent_memo[event_id]

    # Conservative rule: later events in the same stream as branch are considered dependent.
    if (
        event.stream_id == branch_event.stream_id
        and event.stream_position > branch_event.stream_position
    ):
        dependent_memo[event_id] = True
        return True

    visited: set[str] = set()
    current = event
    while True:
        current_id = str(current.event_id)
        if current_id in visited:
            dependent_memo[event_id] = False
            return False
        visited.add(current_id)

        causation_id = current.metadata.get("causation_id")
        if not causation_id:
            dependent_memo[event_id] = False
            return False
        cause_id = str(causation_id)

        if cause_id in branch_ids:
            dependent_memo[event_id] = True
            return True

        known = dependent_memo.get(cause_id)
        if known is True:
            dependent_memo[event_id] = True
            return True
        if known is False:
            dependent_memo[event_id] = False
            return False

        next_event = id_lookup.get(cause_id)
        if next_event is None:
            dependent_memo[event_id] = False
            return False
        current = next_event


def _inject_counterfactual_events(
    *,
    counterfactual_events: list[BaseEvent],
    branch_event: StoredEvent,
    application_id: str,
) -> list[StoredEvent]:
    injected: list[StoredEvent] = []
    base_position = branch_event.stream_position
    last_recorded = branch_event.recorded_at
    for index, event in enumerate(counterfactual_events, start=1):
        override_stream = event.metadata.get("stream_id_override")
        stream_id = str(override_stream) if override_stream else branch_event.stream_id
        payload = dict(event.payload)
        payload.setdefault("application_id", application_id)
        metadata = dict(event.metadata)
        metadata.setdefault("counterfactual", True)
        metadata.setdefault("counterfactual_branch_event_id", str(branch_event.event_id))
        metadata.setdefault("counterfactual_event_order", index)

        injected.append(
            StoredEvent(
                event_id=uuid4(),
                stream_id=stream_id,
                stream_position=base_position + index,
                global_position=branch_event.global_position + index,
                event_type=event.event_type,
                event_version=event.event_version,
                payload=payload,
                metadata=metadata,
                recorded_at=max(last_recorded, datetime.now(UTC)),
            )
        )
    return injected


def _projection_names(projections: list[Any] | None) -> set[str]:
    if projections is None:
        return {"application_summary", "compliance_audit", "agent_performance"}
    names: set[str] = set()
    for projection in projections:
        if isinstance(projection, str):
            names.add(projection)
        elif hasattr(projection, "name"):
            names.add(str(projection.name))
    return names or {"application_summary"}


def _compute_outcome(
    events: list[StoredEvent],
    projections: list[Any] | None,
    application_id: str,
) -> dict[str, Any]:
    names = _projection_names(projections)
    outcome: dict[str, Any] = {}

    if "application_summary" in names:
        outcome["application_summary"] = _compute_application_summary(events, application_id)
    if "compliance_audit" in names or "compliance_audit_view" in names:
        outcome["compliance_audit"] = _compute_compliance_state(events, application_id)
    if "agent_performance" in names or "agent_performance_ledger" in names:
        outcome["agent_performance"] = _compute_agent_performance(events, application_id)

    return outcome


def _compute_application_summary(events: list[StoredEvent], application_id: str) -> dict[str, Any]:
    state = {
        "application_id": application_id,
        "current_state": "UNKNOWN",
        "decision_recommendation": None,
        "final_decision": None,
        "approved_amount_usd": None,
        "assessed_max_limit_usd": None,
    }
    for event in events:
        payload = event.payload
        if payload.get("application_id") != application_id:
            continue
        if event.event_type == "ApplicationSubmitted":
            state["current_state"] = "SUBMITTED"
        elif event.event_type == "DecisionGenerated":
            recommendation = str(payload.get("recommendation", "")).upper()
            state["decision_recommendation"] = recommendation
            state["assessed_max_limit_usd"] = payload.get("assessed_max_limit_usd")
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

    normalized: list[dict[str, Any]] = []
    for item in metrics.values():
        samples = item.pop("confidence_samples")
        item["avg_confidence_score"] = sum(samples) / len(samples) if samples else 0.0
        normalized.append(item)
    return {"application_id": application_id, "agents": normalized}


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
