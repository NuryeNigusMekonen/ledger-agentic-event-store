from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import BaseEvent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping integration upcasting test.")
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
async def test_credit_analysis_v1_upcasted_to_v2_without_mutating_raw_row(
    store: EventStore,
) -> None:
    stream_id = f"agent-agent01-{uuid4()}"
    original_payload = {
        "application_id": "app-1",
        "agent_id": "agent01",
        "session_id": "s1",
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 50000,
        "input_data_hash": "hash-abc",
    }
    await store.append(
        stream_id=stream_id,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="CreditAnalysisCompleted",
                event_version=1,
                payload=original_payload,
                metadata={},
            )
        ],
    )

    loaded = await store.load_stream(stream_id)
    assert len(loaded) == 1
    assert loaded[0].event_version == 2
    assert loaded[0].payload["model_version"] == "legacy-unknown"
    assert "analysis_duration_ms" in loaded[0].payload

    async with store._pool.acquire() as conn:
        raw = await conn.fetchrow(
            """
            SELECT event_version, payload
            FROM events
            WHERE stream_id = $1
            ORDER BY stream_position ASC
            LIMIT 1
            """,
            stream_id,
        )
    assert raw is not None
    assert int(raw["event_version"]) == 1
    assert dict(raw["payload"]) == original_payload
