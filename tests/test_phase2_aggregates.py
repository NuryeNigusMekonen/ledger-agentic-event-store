from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.models.events import DomainError, StoredEvent


def _stored_event(
    event_type: str,
    payload: dict,
    stream_id: str = "test-stream",
    position: int = 1,
    metadata: dict | None = None,
) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        stream_id=stream_id,
        stream_position=position,
        global_position=position,
        event_type=event_type,
        event_version=1,
        payload=payload,
        metadata=metadata or {},
        recorded_at=datetime.now(UTC),
    )


def test_agent_session_requires_context_before_output() -> None:
    aggregate = AgentSessionAggregate.load([])
    with pytest.raises(DomainError):
        aggregate.apply(
            _stored_event(
                event_type="CreditAnalysisCompleted",
                payload={
                    "application_id": "a1",
                    "agent_id": "agent-1",
                    "session_id": "s1",
                    "model_version": "mv-1",
                },
            )
        )


def test_loan_approval_rejected_when_compliance_pending() -> None:
    aggregate = LoanApplicationAggregate.load([])
    aggregate.apply(
        _stored_event(
            event_type="ApplicationSubmitted",
            payload={
                "application_id": "app-1",
                "requested_amount_usd": 1000,
            },
            position=1,
        )
    )
    aggregate.apply(
        _stored_event(
            event_type="DecisionGenerated",
            payload={
                "application_id": "app-1",
                "recommendation": "APPROVE",
                "compliance_status": "PENDING",
                "assessed_max_limit_usd": 700,
                "contributing_agent_sessions": ["agent-a-s1"],
            },
            position=2,
        )
    )
    aggregate.apply(
        _stored_event(
            event_type="HumanReviewCompleted",
            payload={
                "application_id": "app-1",
                "reviewer_id": "u1",
                "override": False,
                "final_decision": "APPROVE",
            },
            position=3,
        )
    )

    with pytest.raises(DomainError):
        aggregate.apply(
            _stored_event(
                event_type="ApplicationApproved",
                payload={
                    "application_id": "app-1",
                    "approved_amount_usd": 600,
                },
                position=4,
            )
        )


def test_compliance_status_moves_to_cleared_when_all_checks_pass() -> None:
    aggregate = ComplianceRecordAggregate.load([])
    aggregate.apply(
        _stored_event(
            event_type="ComplianceCheckRequested",
            payload={
                "application_id": "app-1",
                "regulation_set_version": "2026.03",
                "checks_required": ["rule-a", "rule-b"],
            },
            position=1,
        )
    )
    assert aggregate.status == "PENDING"

    aggregate.apply(
        _stored_event(
            event_type="ComplianceRulePassed",
            payload={
                "application_id": "app-1",
                "rule_id": "rule-a",
                "rule_version": "v1",
            },
            position=2,
        )
    )
    assert aggregate.status == "PENDING"

    aggregate.apply(
        _stored_event(
            event_type="ComplianceRulePassed",
            payload={
                "application_id": "app-1",
                "rule_id": "rule-b",
                "rule_version": "v1",
            },
            position=3,
        )
    )
    assert aggregate.status == "CLEARED"

