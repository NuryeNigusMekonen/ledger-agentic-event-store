from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.integrity.gas_town import reconstruct_agent_context
from src.models.events import (
    AgentContextLoadedEvent,
    CreditAnalysisCompletedEvent,
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
async def test_reconstruct_agent_context_respects_token_budget(store: EventStore) -> None:
    agent_id = "agent-01"
    session_id = f"s-{uuid4()}"
    stream_id = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            AgentContextLoadedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 4000,
                    "model_version": "model-v1",
                },
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-1",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v1",
                    "confidence_score": 0.77,
                    "risk_tier": "MEDIUM",
                    "recommended_limit_usd": 50000,
                    "analysis_duration_ms": 121,
                    "input_data_hash": "hash-" + ("x" * 800),
                },
            ),
            FraudScreeningCompletedEvent(
                payload={
                    "application_id": "app-1",
                    "agent_id": agent_id,
                    "fraud_score": 0.1,
                    "anomaly_flags": ["none"],
                    "screening_model_version": "model-v1",
                    "input_data_hash": "hash-" + ("y" * 800),
                },
            ),
        ],
    )

    context = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
        token_budget=180,
    )
    assert context.stream_id == stream_id
    assert context.model_version == "model-v1"
    assert context.dropped_events >= 1
    assert context.needs_reconciliation is True
    assert "token_budget_exhausted_partial_context" in context.reconciliation_reasons
    assert any(event.event_type == "AgentContextLoaded" for event in context.included_events)


@pytest.mark.asyncio
async def test_reconstruct_agent_context_flags_missing_context_loaded(store: EventStore) -> None:
    agent_id = "agent-02"
    session_id = f"s-{uuid4()}"
    stream_id = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-2",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "model-v2",
                    "confidence_score": 0.66,
                    "risk_tier": "HIGH",
                    "recommended_limit_usd": 10000,
                    "analysis_duration_ms": 90,
                    "input_data_hash": "hash-z",
                },
            )
        ],
    )

    context = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
        token_budget=512,
    )
    assert context.needs_reconciliation is True
    assert "missing_agent_context_loaded" in context.reconciliation_reasons
    assert context.last_stream_position == 1
