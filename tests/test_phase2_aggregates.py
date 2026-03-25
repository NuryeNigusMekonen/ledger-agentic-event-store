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
    aggregate = AgentSessionAggregate.replay([])
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
    aggregate = LoanApplicationAggregate.replay([])
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
            event_type="CreditAnalysisRequested",
            payload={"application_id": "app-1"},
            position=2,
        )
    )
    aggregate.apply(
        _stored_event(
            event_type="DecisionGenerated",
            payload={
                "application_id": "app-1",
                "recommendation": "REFER",
                "compliance_status": "PENDING",
                "assessed_max_limit_usd": 700,
                "contributing_agent_sessions": ["agent-a-s1"],
            },
            position=3,
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
            position=4,
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
                position=5,
            )
        )


def test_compliance_status_moves_to_cleared_when_all_checks_pass() -> None:
    aggregate = ComplianceRecordAggregate.replay([])
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


def test_decision_confidence_floor_forces_refer_in_aggregate() -> None:
    application_id = "app-floor"
    loan = LoanApplicationAggregate.replay(
        [
            _stored_event(
                event_type="ApplicationSubmitted",
                payload={
                    "application_id": application_id,
                    "requested_amount_usd": 5000,
                },
                stream_id=f"loan-{application_id}",
                position=1,
            ),
            _stored_event(
                event_type="CreditAnalysisRequested",
                payload={"application_id": application_id},
                stream_id=f"loan-{application_id}",
                position=2,
            ),
        ]
    )
    contributing_stream = "agent-credit-1-s1"
    contributing_events = {
        contributing_stream: [
            _stored_event(
                event_type="AgentContextLoaded",
                payload={
                    "agent_id": "credit-1",
                    "session_id": "s1",
                    "model_version": "credit-v2",
                },
                stream_id=contributing_stream,
                position=1,
            ),
            _stored_event(
                event_type="CreditAnalysisCompleted",
                payload={
                    "application_id": application_id,
                    "agent_id": "credit-1",
                    "session_id": "s1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.55,
                },
                stream_id=contributing_stream,
                position=2,
            ),
        ]
    }

    effective = loan.validate_decision_generation(
        recommendation="APPROVE",
        confidence_score=0.55,
        compliance_status="CLEARED",
        contributing_agent_sessions=[contributing_stream],
        contributing_session_events=contributing_events,
        assessed_max_limit_usd=4000,
    )
    assert effective == "REFER"


def test_decision_requires_contributing_session_causal_chain() -> None:
    application_id = "app-causal"
    loan = LoanApplicationAggregate.replay(
        [
            _stored_event(
                event_type="ApplicationSubmitted",
                payload={
                    "application_id": application_id,
                    "requested_amount_usd": 12000,
                },
                stream_id=f"loan-{application_id}",
                position=1,
            ),
            _stored_event(
                event_type="CreditAnalysisRequested",
                payload={"application_id": application_id},
                stream_id=f"loan-{application_id}",
                position=2,
            ),
        ]
    )
    contributing_stream = "agent-credit-2-s2"

    with pytest.raises(DomainError):
        loan.validate_decision_generation(
            recommendation="REFER",
            confidence_score=0.82,
            compliance_status="CLEARED",
            contributing_agent_sessions=[contributing_stream],
            contributing_session_events={
                contributing_stream: [
                    _stored_event(
                        event_type="CreditAnalysisCompleted",
                        payload={
                            "application_id": application_id,
                            "agent_id": "credit-2",
                            "session_id": "s2",
                            "model_version": "credit-v3",
                            "confidence_score": 0.82,
                        },
                        stream_id=contributing_stream,
                        position=1,
                    )
                ]
            },
            assessed_max_limit_usd=10000,
        )
