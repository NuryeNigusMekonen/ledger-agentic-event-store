from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from src.models.events import StoredEvent
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import _next_state


def _event(event_type: str, payload: dict[str, object], global_position: int = 1) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        stream_id="test-stream",
        stream_position=1,
        global_position=global_position,
        event_type=event_type,
        event_version=1,
        payload=payload,
        metadata={},
        recorded_at=datetime.now(UTC),
    )


def test_application_summary_uses_completion_verdict_for_compliance_status() -> None:
    projection = ApplicationSummaryProjection()
    patch = projection._state_patch(
        _event(
            "ComplianceCheckCompleted",
            {"application_id": "app-1", "overall_verdict": "CLEARED"},
        )
    )
    assert patch == {"compliance_status": "CLEARED"}


def test_application_summary_touches_auxiliary_events_for_last_event_tracking() -> None:
    projection = ApplicationSummaryProjection()
    touch_event_types = {
        "DocumentUploadRequested",
        "DocumentUploaded",
        "FraudScreeningRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionRequested",
        "HumanReviewRequested",
    }
    for event_type in touch_event_types:
        patch = projection._state_patch(
            _event(event_type, {"application_id": "app-1"})
        )
        assert patch == {}


def test_compliance_next_state_prefers_completion_verdict_when_valid() -> None:
    state = {
        "regulation_set_version": "2026.03",
        "mandatory_checks": {"rule-a", "rule-b"},
        "passed_checks": {"rule-a"},
        "failed_checks": {},
        "compliance_status": "PENDING",
    }
    updated = _next_state(
        state,
        _event(
            "ComplianceCheckCompleted",
            {"application_id": "app-1", "overall_verdict": "FAILED"},
        ),
    )
    assert updated["compliance_status"] == "FAILED"
    assert updated["mandatory_checks"] == {"rule-a", "rule-b"}
    assert updated["passed_checks"] == {"rule-a"}

