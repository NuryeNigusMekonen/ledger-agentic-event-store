from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel

from src.commands.handlers import (
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
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
        limit: int | None = None,
    ) -> list[StoredEvent]:
        _ = (from_position, limit)
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
