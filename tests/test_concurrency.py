from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import (
    AppendResult,
    ApplicationSubmittedEvent,
    ComplianceCheckRequestedEvent,
    CreditAnalysisRequestedEvent,
    DecisionGeneratedEvent,
    OptimisticConcurrencyError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping integration concurrency test.")
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
    schema_path = Path(__file__).resolve().parents[1] / "src" / "schema.sql"
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
async def test_double_decision_one_winner_one_occ_loser(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"

    # Seed the stream to version 3 so both contenders race on expected_version=3.
    initial_events = [
        ApplicationSubmittedEvent(
            payload={"application_id": stream_id, "requested_amount_usd": 1}
        ),
        CreditAnalysisRequestedEvent(
            payload={
                "application_id": stream_id,
                "assigned_agent_id": "race-agent",
                "requested_at": "2026-01-01T00:00:00+00:00",
                "priority": "normal",
            }
        ),
        ComplianceCheckRequestedEvent(
            payload={
                "application_id": stream_id,
                "regulation_set_version": "2026.03",
                "checks_required": ["rule-1"],
            }
        ),
    ]
    seeded = await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        events=initial_events,
        expected_version=-1,
    )
    assert seeded.new_stream_version == 3

    start_gate = asyncio.Event()

    async def attempt(label: str) -> AppendResult:
        await start_gate.wait()
        return await store.append(
            stream_id=stream_id,
            aggregate_type="LoanApplication",
            events=[
                DecisionGeneratedEvent(
                    payload={
                        "application_id": stream_id,
                        "orchestrator_agent_id": label,
                        "recommendation": "REFER",
                        "confidence_score": 0.61,
                        "contributing_agent_sessions": [],
                        "decision_basis_summary": "concurrency-race",
                        "model_versions": {},
                    }
                )
            ],
            expected_version=3,
            correlation_id="corr-concurrency-race",
            causation_id="cause-concurrency-race",
        )

    task_a = asyncio.create_task(attempt("agent-a"))
    task_b = asyncio.create_task(attempt("agent-b"))

    await asyncio.sleep(0)
    start_gate.set()
    results = await asyncio.gather(task_a, task_b, return_exceptions=True)

    winners = [r for r in results if isinstance(r, AppendResult)]
    losers = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].new_stream_version == 4

    loser = losers[0]
    assert type(loser) is OptimisticConcurrencyError
    assert loser.expected_version == 3
    assert loser.actual_version == 4

    events = await store.load_stream(stream_id)
    assert len(events) == 4
    assert [event.stream_position for event in events] == [1, 2, 3, 4]
    assert events[-1].metadata["correlation_id"] == "corr-concurrency-race"
    assert events[-1].metadata["causation_id"] == "cause-concurrency-race"
    assert await store.stream_version(stream_id) == 4
