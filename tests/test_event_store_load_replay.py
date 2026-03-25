from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import (
    ApplicationSubmittedEvent,
    CreditAnalysisCompletedEvent,
    DecisionGeneratedEvent,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping event store load/replay test.")
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
async def test_load_stream_supports_position_window_order_and_transparent_upcasting(
    store: EventStore,
) -> None:
    stream_id = f"agent-a-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=-1,
        events=[
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-1",
                    "agent_id": "agent-a",
                    "session_id": "s-1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.8,
                }
            ),
            CreditAnalysisCompletedEvent(
                event_version=1,
                payload={
                    "application_id": "app-1",
                    "agent_id": "agent-a",
                    "session_id": "s-1",
                },
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-1",
                    "agent_id": "agent-a",
                    "session_id": "s-1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.77,
                }
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": "app-1",
                    "agent_id": "agent-a",
                    "session_id": "s-1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.74,
                }
            ),
        ],
    )

    window = await store.load_stream(stream_id, from_position=2, to_position=3)
    assert [event.stream_position for event in window] == [2, 3]
    # Position 2 was written as v1 and should be transparently upcast on load.
    assert window[0].event_version == 2
    assert window[0].payload["model_version"] in {"credit-v1", "credit-v1.5", "credit-v2"}


@pytest.mark.asyncio
async def test_load_all_async_generator_supports_filters_and_batching(
    store: EventStore,
) -> None:
    app_a = f"app-{uuid4()}"
    app_b = f"app-{uuid4()}"

    first = await store.append(
        stream_id=f"loan-{app_a}",
        aggregate_type="LoanApplication",
        expected_version=-1,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": app_a, "requested_amount_usd": 1000}
            ),
            DecisionGeneratedEvent(
                payload={"application_id": app_a, "recommendation": "REFER"}
            ),
        ],
    )
    await store.append(
        stream_id=f"loan-{app_b}",
        aggregate_type="LoanApplication",
        expected_version=-1,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": app_b, "requested_amount_usd": 2000}
            )
        ],
    )
    await store.append(
        stream_id=f"agent-x-{uuid4()}",
        aggregate_type="AgentSession",
        expected_version=-1,
        events=[
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": app_a,
                    "agent_id": "agent-x",
                    "session_id": "s-1",
                    "model_version": "credit-v2",
                }
            )
        ],
    )

    filtered_batches: list[list[str]] = []
    filtered_events = []
    async for batch in store.load_all(
        from_global_position=0,
        event_types=["ApplicationSubmitted", "DecisionGenerated"],
        batch_size=1,
    ):
        filtered_batches.append([event.event_type for event in batch])
        filtered_events.extend(batch)

    assert all(len(batch) <= 1 for batch in filtered_batches)
    assert [event.event_type for event in filtered_events] == [
        "ApplicationSubmitted",
        "DecisionGenerated",
        "ApplicationSubmitted",
    ]
    assert [event.global_position for event in filtered_events] == sorted(
        event.global_position for event in filtered_events
    )

    resumed = []
    async for batch in store.load_all(
        from_global_position=first.events[0].global_position,
        event_types=["ApplicationSubmitted", "DecisionGenerated"],
        batch_size=50,
    ):
        resumed.extend(batch)
    assert [event.event_type for event in resumed] == [
        "DecisionGenerated",
        "ApplicationSubmitted",
    ]


@pytest.mark.asyncio
async def test_stream_supporting_methods_present_and_working(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"

    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=-1,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": stream_id, "requested_amount_usd": 5000}
            )
        ],
    )

    assert await store.stream_version(stream_id) == 1

    metadata = await store.get_stream_metadata(stream_id)
    assert metadata.stream_id == stream_id
    assert metadata.current_version == 1
    assert metadata.archived_at is None

    archived = await store.archive_stream(stream_id, reason="rubric-check")
    assert archived.archived_at is not None
    assert archived.metadata.get("archived_reason") == "rubric-check"
