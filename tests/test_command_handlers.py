from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from src.commands.handlers import (
    ComplianceCheckCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    WriteCommandHandlers,
)
from src.models.events import AppendResult, StoredEvent


class RecordingStore:
    def __init__(self, streams: dict[str, list[StoredEvent]]) -> None:
        self._streams = streams
        self.load_calls: list[str] = []
        self.append_calls: list[dict[str, object]] = []
        self.operations: list[tuple[str, str]] = []

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 1,
        to_position: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        _ = (from_position, to_position, limit)
        self.load_calls.append(stream_id)
        self.operations.append(("load", stream_id))
        return list(self._streams.get(stream_id, []))

    async def append(self, **kwargs: object) -> AppendResult:
        stream_id = str(kwargs["stream_id"])
        expected_version = int(kwargs["expected_version"])
        events = list(kwargs["events"])

        self.append_calls.append(dict(kwargs))
        self.operations.append(("append", stream_id))

        stored: list[StoredEvent] = []
        for offset, event in enumerate(events, start=1):
            stored.append(
                StoredEvent(
                    event_id=uuid4(),
                    stream_id=stream_id,
                    stream_position=expected_version + offset,
                    global_position=1000 + offset,
                    event_type=event.event_type,
                    event_version=event.event_version,
                    payload=_json_object(event.payload),
                    metadata=event.metadata,
                    recorded_at=datetime.now(UTC),
                )
            )

        return AppendResult(
            stream_id=stream_id,
            new_stream_version=expected_version + len(events),
            events=stored,
        )


def _stored_event(
    *,
    stream_id: str,
    stream_position: int,
    event_type: str,
    payload: dict[str, object],
    metadata: dict[str, str] | None = None,
) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        stream_id=stream_id,
        stream_position=stream_position,
        global_position=stream_position,
        event_type=event_type,
        event_version=1,
        payload=payload,
        metadata=metadata or {},
        recorded_at=datetime.now(UTC),
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return dict(value)


@pytest.mark.asyncio
async def test_credit_analysis_handler_uses_aggregate_version_and_threads_causality() -> None:
    application_id = "app-123"
    agent_id = "agent-a"
    session_id = "sess-9"
    loan_stream_id = f"loan-{application_id}"
    session_stream_id = f"agent-{agent_id}-{session_id}"

    store = RecordingStore(
        streams={
            loan_stream_id: [
                _stored_event(
                    stream_id=loan_stream_id,
                    stream_position=4,
                    event_type="ApplicationSubmitted",
                    payload={
                        "application_id": application_id,
                        "requested_amount_usd": 20000,
                    },
                )
            ],
            session_stream_id: [
                _stored_event(
                    stream_id=session_stream_id,
                    stream_position=7,
                    event_type="AgentContextLoaded",
                    payload={
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "context_source": "replay",
                        "event_replay_from_position": 0,
                        "context_token_count": 1200,
                        "model_version": "credit-model-v2",
                    },
                )
            ],
        }
    )
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=application_id,
            agent_id=agent_id,
            session_id=session_id,
            model_version="credit-model-v2",
            confidence_score=0.77,
            risk_tier="LOW",
            recommended_limit_usd=12500.0,
            analysis_duration_ms=840,
            input_data_hash="sha256:abc",
            correlation_id="corr-credit-1",
            causation_id="cause-credit-1",
        )
    )

    assert store.operations == [
        ("load", session_stream_id),
        ("load", loan_stream_id),
        ("append", session_stream_id),
    ]
    assert len(store.append_calls) == 1
    append_call = store.append_calls[0]
    assert append_call["expected_version"] == 7
    assert append_call["correlation_id"] == "corr-credit-1"
    assert append_call["causation_id"] == "cause-credit-1"

    appended_events = append_call["events"]
    assert len(appended_events) == 1
    assert appended_events[0].metadata["correlation_id"] == "corr-credit-1"
    assert appended_events[0].metadata["causation_id"] == "cause-credit-1"
    assert result.new_stream_version == 8


@pytest.mark.asyncio
async def test_fraud_screening_handler_uses_aggregate_version_and_threads_causality() -> None:
    application_id = "app-456"
    agent_id = "agent-b"
    session_id = "sess-3"
    loan_stream_id = f"loan-{application_id}"
    session_stream_id = f"agent-{agent_id}-{session_id}"

    store = RecordingStore(
        streams={
            loan_stream_id: [
                _stored_event(
                    stream_id=loan_stream_id,
                    stream_position=2,
                    event_type="ApplicationSubmitted",
                    payload={
                        "application_id": application_id,
                        "requested_amount_usd": 50000,
                    },
                )
            ],
            session_stream_id: [
                _stored_event(
                    stream_id=session_stream_id,
                    stream_position=5,
                    event_type="AgentContextLoaded",
                    payload={
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "context_source": "replay",
                        "event_replay_from_position": 0,
                        "context_token_count": 900,
                        "model_version": "fraud-model-v1",
                    },
                )
            ],
        }
    )
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=application_id,
            agent_id=agent_id,
            session_id=session_id,
            fraud_score=0.08,
            anomaly_flags=[],
            screening_model_version="fraud-model-v1",
            input_data_hash="sha256:def",
            correlation_id="corr-fraud-1",
            causation_id="cause-fraud-1",
        )
    )

    assert store.operations == [
        ("load", session_stream_id),
        ("load", loan_stream_id),
        ("append", session_stream_id),
    ]
    assert len(store.append_calls) == 1
    append_call = store.append_calls[0]
    assert append_call["expected_version"] == 5
    assert append_call["correlation_id"] == "corr-fraud-1"
    assert append_call["causation_id"] == "cause-fraud-1"

    appended_events = append_call["events"]
    assert len(appended_events) == 1
    assert appended_events[0].metadata["correlation_id"] == "corr-fraud-1"
    assert appended_events[0].metadata["causation_id"] == "cause-fraud-1"
    assert result.new_stream_version == 6


@pytest.mark.asyncio
async def test_submit_application_emits_document_and_analysis_request_events(
    tmp_path: Path,
) -> None:
    application_id = "app-789"
    document_path = tmp_path / "borrower_report.txt"
    document_path.write_text("Total Revenue: 1000000\nNet Income: 220000\n", encoding="utf-8")

    store = RecordingStore(streams={})
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_submit_application(
        SubmitApplicationCommand(
            application_id=application_id,
            applicant_id="customer-789",
            requested_amount_usd=18000.0,
            loan_purpose="inventory",
            submission_channel="portal",
            submitted_at=datetime.now(UTC),
            document_path=str(document_path),
            process_documents_after_submit=False,
            correlation_id="corr-submit-1",
            causation_id="cause-submit-1",
        )
    )

    assert store.operations == [
        ("load", f"loan-{application_id}"),
        ("append", f"loan-{application_id}"),
    ]
    append_call = store.append_calls[0]
    event_types = [event.event_type for event in append_call["events"]]
    assert event_types == [
        "ApplicationSubmitted",
        "DocumentUploadRequested",
        "DocumentUploaded",
        "CreditAnalysisRequested",
        "FraudScreeningRequested",
    ]
    assert result.new_stream_version == 5


@pytest.mark.asyncio
async def test_start_agent_session_emits_started_and_recovery_events() -> None:
    agent_id = "credit-agent-2"
    session_id = "session-new"
    stream_id = f"agent-{agent_id}-{session_id}"

    store = RecordingStore(streams={})
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id,
            session_id=session_id,
            context_source="prior_session_replay:session-old",
            event_replay_from_position=15,
            context_token_count=2048,
            model_version="credit-v3",
            correlation_id="corr-session-1",
            causation_id="cause-session-1",
        )
    )

    assert store.operations == [("load", stream_id), ("append", stream_id)]
    append_call = store.append_calls[0]
    event_types = [event.event_type for event in append_call["events"]]
    assert event_types == [
        "AgentSessionStarted",
        "AgentSessionRecovered",
        "AgentContextLoaded",
    ]
    assert result.new_stream_version == 3


@pytest.mark.asyncio
async def test_generate_decision_emits_request_and_human_review_markers() -> None:
    application_id = "app-decision"
    loan_stream = f"loan-{application_id}"
    compliance_stream = f"compliance-{application_id}"
    session_stream = "agent-credit-agent-1-session-1"

    store = RecordingStore(
        streams={
            loan_stream: [
                _stored_event(
                    stream_id=loan_stream,
                    stream_position=1,
                    event_type="ApplicationSubmitted",
                    payload={
                        "application_id": application_id,
                        "requested_amount_usd": 40000,
                    },
                ),
                _stored_event(
                    stream_id=loan_stream,
                    stream_position=2,
                    event_type="CreditAnalysisRequested",
                    payload={"application_id": application_id},
                ),
            ],
            compliance_stream: [
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=1,
                    event_type="ComplianceCheckRequested",
                    payload={
                        "application_id": application_id,
                        "regulation_set_version": "2026.03",
                        "checks_required": ["rule-a"],
                    },
                ),
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=2,
                    event_type="ComplianceRulePassed",
                    payload={
                        "application_id": application_id,
                        "rule_id": "rule-a",
                        "rule_version": "v1",
                    },
                ),
            ],
            session_stream: [
                _stored_event(
                    stream_id=session_stream,
                    stream_position=1,
                    event_type="AgentContextLoaded",
                    payload={
                        "agent_id": "credit-agent-1",
                        "session_id": "session-1",
                        "context_source": "event-replay",
                        "event_replay_from_position": 1,
                        "context_token_count": 1200,
                        "model_version": "credit-v2",
                    },
                ),
                _stored_event(
                    stream_id=session_stream,
                    stream_position=2,
                    event_type="CreditAnalysisCompleted",
                    payload={
                        "application_id": application_id,
                        "agent_id": "credit-agent-1",
                        "session_id": "session-1",
                        "model_version": "credit-v2",
                        "confidence_score": 0.85,
                        "risk_tier": "LOW",
                        "recommended_limit_usd": 35000,
                        "analysis_duration_ms": 120,
                        "input_data_hash": "hash-credit",
                    },
                ),
                _stored_event(
                    stream_id=session_stream,
                    stream_position=3,
                    event_type="FraudScreeningCompleted",
                    payload={
                        "application_id": application_id,
                        "agent_id": "credit-agent-1",
                        "fraud_score": 0.05,
                        "anomaly_flags": [],
                        "screening_model_version": "credit-v2",
                        "input_data_hash": "hash-fraud",
                    },
                ),
            ],
        }
    )
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            orchestrator_agent_id="orchestrator-1",
            recommendation="APPROVE",
            confidence_score=0.9,
            decision_basis_summary="all clear",
            contributing_agent_sessions=[session_stream],
            model_versions={"orchestrator-1": "orch-v1"},
            correlation_id="corr-decision-1",
            causation_id="cause-decision-1",
        )
    )

    assert store.operations == [
        ("load", loan_stream),
        ("load", compliance_stream),
        ("load", session_stream),
        ("append", loan_stream),
    ]
    append_call = store.append_calls[0]
    event_types = [event.event_type for event in append_call["events"]]
    assert event_types == [
        "DecisionRequested",
        "DecisionGenerated",
        "HumanReviewRequested",
    ]
    assert result.new_stream_version == 5


@pytest.mark.asyncio
async def test_generate_decision_forces_refer_below_confidence_floor() -> None:
    application_id = "app-floor-handler"
    loan_stream = f"loan-{application_id}"
    compliance_stream = f"compliance-{application_id}"
    session_stream = "agent-credit-agent-9-session-9"

    store = RecordingStore(
        streams={
            loan_stream: [
                _stored_event(
                    stream_id=loan_stream,
                    stream_position=1,
                    event_type="ApplicationSubmitted",
                    payload={
                        "application_id": application_id,
                        "requested_amount_usd": 12000,
                    },
                ),
                _stored_event(
                    stream_id=loan_stream,
                    stream_position=2,
                    event_type="CreditAnalysisRequested",
                    payload={"application_id": application_id},
                ),
            ],
            compliance_stream: [
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=1,
                    event_type="ComplianceCheckRequested",
                    payload={
                        "application_id": application_id,
                        "regulation_set_version": "2026.03",
                        "checks_required": ["rule-a"],
                    },
                ),
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=2,
                    event_type="ComplianceRulePassed",
                    payload={
                        "application_id": application_id,
                        "rule_id": "rule-a",
                        "rule_version": "v1",
                    },
                ),
            ],
            session_stream: [
                _stored_event(
                    stream_id=session_stream,
                    stream_position=1,
                    event_type="AgentContextLoaded",
                    payload={
                        "agent_id": "credit-agent-9",
                        "session_id": "session-9",
                        "context_source": "event-replay",
                        "event_replay_from_position": 1,
                        "context_token_count": 1200,
                        "model_version": "credit-v2",
                    },
                ),
                _stored_event(
                    stream_id=session_stream,
                    stream_position=2,
                    event_type="CreditAnalysisCompleted",
                    payload={
                        "application_id": application_id,
                        "agent_id": "credit-agent-9",
                        "session_id": "session-9",
                        "model_version": "credit-v2",
                        "confidence_score": 0.55,
                        "risk_tier": "MEDIUM",
                        "recommended_limit_usd": 9000,
                        "analysis_duration_ms": 150,
                        "input_data_hash": "hash-9",
                    },
                ),
            ],
        }
    )
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    await handlers.handle_generate_decision(
        GenerateDecisionCommand(
            application_id=application_id,
            orchestrator_agent_id="orchestrator-9",
            recommendation="APPROVE",
            confidence_score=0.55,
            decision_basis_summary="low confidence should force refer",
            contributing_agent_sessions=[session_stream],
            model_versions={"orchestrator-9": "orch-v9"},
            correlation_id="corr-floor-1",
            causation_id="cause-floor-1",
        )
    )

    append_call = store.append_calls[0]
    decision_event = next(
        event for event in append_call["events"] if event.event_type == "DecisionGenerated"
    )
    assert decision_event.payload.recommendation == "REFER"


@pytest.mark.asyncio
async def test_compliance_check_emits_completed_when_verdict_becomes_terminal() -> None:
    application_id = "app-compliance"
    compliance_stream = f"compliance-{application_id}"

    store = RecordingStore(
        streams={
            compliance_stream: [
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=1,
                    event_type="ComplianceCheckRequested",
                    payload={
                        "application_id": application_id,
                        "regulation_set_version": "2026.03",
                        "checks_required": ["rule-a", "rule-b"],
                    },
                ),
                _stored_event(
                    stream_id=compliance_stream,
                    stream_position=2,
                    event_type="ComplianceRulePassed",
                    payload={
                        "application_id": application_id,
                        "rule_id": "rule-a",
                        "rule_version": "v1",
                    },
                ),
            ]
        }
    )
    handlers = WriteCommandHandlers(store=store)  # type: ignore[arg-type]

    result = await handlers.handle_compliance_check(
        ComplianceCheckCommand(
            application_id=application_id,
            regulation_set_version="2026.03",
            rule_id="rule-b",
            rule_version="v1",
            passed=True,
            correlation_id="corr-comp-1",
            causation_id="cause-comp-1",
        )
    )

    assert store.operations == [("load", compliance_stream), ("append", compliance_stream)]
    append_call = store.append_calls[0]
    event_types = [event.event_type for event in append_call["events"]]
    assert event_types == ["ComplianceRulePassed", "ComplianceCheckCompleted"]
    completion = append_call["events"][-1]
    assert completion.payload.overall_verdict == "CLEARED"
    assert result.new_stream_version == 4
