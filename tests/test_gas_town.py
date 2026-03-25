from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.integrity.gas_town import AgentContext, reconstruct_agent_context
from src.models.events import (
    AgentContextLoadedEvent,
    AgentSessionStartedEvent,
    BaseEvent,
    CreditAnalysisCompletedEvent,
    DecisionGeneratedEvent,
    DecisionRequestedEvent,
    FraudScreeningCompletedEvent,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping gas town integration test.")
    return value


@pytest_asyncio.fixture
async def store() -> EventStore:
    database_url = _database_url()
    try:
        event_store = await EventStore.from_dsn(
            database_url,
            min_size=1,
            max_size=10,
            connect_timeout=2.0,
        )
    except Exception as exc:  # pragma: no cover - integration environment dependent
        pytest.skip(f"PostgreSQL is not reachable for integration test: {exc}")

    schema_path = PROJECT_ROOT / "src" / "schema.sql"
    await event_store.apply_schema(schema_path)
    async with event_store._pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE TABLE outbox, events, event_streams, projection_checkpoints
            RESTART IDENTITY CASCADE
            """
        )
    yield event_store
    await event_store.close()


@pytest.mark.asyncio
async def test_reconstruct_agent_context_selective_preservation_and_summary(
    store: EventStore,
) -> None:
    agent_id = "agent-gas-1"
    session_id = f"s-{uuid4()}"
    stream_id = f"agent-{agent_id}-{session_id}"
    now = datetime.now(UTC).isoformat()

    await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            AgentSessionStartedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v1",
                    "context_source": "event-replay",
                    "context_token_count": 2048,
                    "started_at": now,
                },
            ),
            AgentContextLoadedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 2048,
                    "model_version": "model-v1",
                },
            ),
            BaseEvent(
                event_type="AgentTaskStateChanged",
                payload={
                    "application_id": "app-gas-1",
                    "task": "fraud_screen",
                    "state": "ERROR",
                    "error_message": "upstream_timeout",
                },
                metadata={},
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-gas-1",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v1",
                    "confidence_score": 0.81,
                    "risk_tier": "MEDIUM",
                    "recommended_limit_usd": 6400,
                    "analysis_duration_ms": 120,
                    "input_data_hash": "hash-credit",
                },
            ),
            FraudScreeningCompletedEvent(
                payload={
                    "application_id": "app-gas-1",
                    "agent_id": agent_id,
                    "fraud_score": 0.12,
                    "anomaly_flags": [],
                    "screening_model_version": "model-v1",
                    "input_data_hash": "hash-fraud",
                },
            ),
            DecisionRequestedEvent(
                payload={
                    "application_id": "app-gas-1",
                    "requested_at": now,
                    "required_inputs": ["credit", "fraud"],
                },
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": "app-gas-1",
                    "recommendation": "REFER",
                    "orchestrator_agent_id": agent_id,
                },
            ),
        ],
    )

    context = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
        token_budget=4096,
    )
    assert isinstance(context, AgentContext)
    assert context.last_event_position == 7
    assert context.session_health_status == "NEEDS_RECONCILIATION"

    assert "Earlier history summary:" in context.context_text
    # PENDING/ERROR state event preserved even though it is older than last 3.
    assert '"stream_position":3' in context.context_text
    # Last three events preserved verbatim.
    assert '"stream_position":5' in context.context_text
    assert '"stream_position":6' in context.context_text
    assert '"stream_position":7' in context.context_text


@pytest.mark.asyncio
async def test_crash_recovery_flags_pending_work_and_fifth_event_position(
    store: EventStore,
) -> None:
    agent_id = "agent-gas-2"
    session_id = f"s-{uuid4()}"
    stream_id = f"agent-{agent_id}-{session_id}"
    now = datetime.now(UTC).isoformat()

    append_result = await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            AgentSessionStartedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v2",
                    "context_source": "event-replay",
                    "context_token_count": 1536,
                    "started_at": now,
                },
            ),
            AgentContextLoadedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 1536,
                    "model_version": "model-v2",
                },
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-gas-2",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v2",
                    "confidence_score": 0.74,
                    "risk_tier": "HIGH",
                    "recommended_limit_usd": 3100,
                    "analysis_duration_ms": 88,
                    "input_data_hash": "hash-credit-2",
                },
            ),
            DecisionRequestedEvent(
                payload={
                    "application_id": "app-gas-2",
                    "requested_at": now,
                    "required_inputs": ["credit"],
                },
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": "app-gas-2",
                    "recommendation": "REFER",
                    "orchestrator_agent_id": agent_id,
                },
            ),
        ],
    )

    context = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
    )
    assert context.pending_work
    assert context.last_event_position == append_result.events[-1].stream_position
    assert context.last_event_position == 5
    assert context.session_health_status == "NEEDS_RECONCILIATION"
