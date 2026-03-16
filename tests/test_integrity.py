from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.integrity.audit_chain import attach_integrity_chain, run_integrity_check
from src.models.events import BaseEvent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping integrity integration test.")
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
async def test_integrity_chain_detects_tampering(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"
    base_events = [
        BaseEvent(
            event_type="ApplicationSubmitted",
            payload={"application_id": "app-1", "requested_amount_usd": 1000},
        ),
        BaseEvent(
            event_type="DecisionGenerated",
            event_version=2,
            payload={
                "application_id": "app-1",
                "recommendation": "DECLINE",
                "model_versions": {"orchestrator-1": "v2"},
                "contributing_agent_sessions": [],
                "decision_basis_summary": "risk too high",
            },
        ),
    ]
    hashed = attach_integrity_chain(
        stream_id=stream_id,
        expected_version=0,
        events=base_events,
    )
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=hashed,
    )

    valid = await run_integrity_check(store, stream_id)
    assert valid.chain_valid is True
    assert valid.events_verified_count == 2

    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE events
            SET payload = payload || '{"tampered": true}'::jsonb
            WHERE stream_id = $1 AND stream_position = 2
            """,
            stream_id,
        )

    tampered = await run_integrity_check(store, stream_id)
    assert tampered.chain_valid is False
    assert len(tampered.violations) >= 1
    assert tampered.violations[0].stream_position == 2

