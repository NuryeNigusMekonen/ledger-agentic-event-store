from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import BaseEvent
from src.outbox import OutboxMessage, OutboxRelay, PostgresOutboxSinkPublisher

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping outbox relay integration test.")
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
            TRUNCATE TABLE
              outbox_sink_events,
              outbox,
              events,
              event_streams
            RESTART IDENTITY CASCADE
            """
        )
    yield event_store
    await event_store.close()


class _AlwaysFailPublisher:
    async def publish(self, message: OutboxMessage) -> None:
        raise RuntimeError(f"simulated_publish_failure:{message.outbox_id}")


@pytest.mark.asyncio
async def test_outbox_relay_publishes_to_sink_and_marks_published(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={"application_id": str(uuid4()), "requested_amount_usd": 1000},
            )
        ],
    )

    publisher = PostgresOutboxSinkPublisher(store)
    await publisher.ensure_schema()
    relay = OutboxRelay(
        store=store,
        publisher=publisher,
        batch_size=10,
        max_attempts=3,
        retry_base_seconds=0.1,
        retry_max_seconds=1.0,
        claim_ttl_seconds=1.0,
    )

    result = await relay.run_once()
    assert result.claimed == 1
    assert result.published == 1
    assert result.failed == 0
    assert result.dead_lettered == 0

    async with store._pool.acquire() as conn:
        outbox_row = await conn.fetchrow(
            """
            SELECT status, attempts, published_at, last_error
            FROM outbox
            ORDER BY outbox_id ASC
            LIMIT 1
            """
        )
        assert outbox_row is not None
        assert outbox_row["status"] == "published"
        assert int(outbox_row["attempts"]) == 1
        assert outbox_row["published_at"] is not None
        assert outbox_row["last_error"] is None

        sink_row = await conn.fetchrow(
            """
            SELECT topic, payload
            FROM outbox_sink_events
            ORDER BY sink_id ASC
            LIMIT 1
            """
        )
        assert sink_row is not None
        assert sink_row["topic"] == "LoanApplication.events"
        assert sink_row["payload"]["stream_id"] == stream_id


@pytest.mark.asyncio
async def test_outbox_relay_retries_and_dead_letters_after_max_attempts(
    store: EventStore,
) -> None:
    stream_id = f"loan-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={"application_id": str(uuid4()), "requested_amount_usd": 2000},
            )
        ],
    )

    relay = OutboxRelay(
        store=store,
        publisher=_AlwaysFailPublisher(),
        batch_size=10,
        max_attempts=2,
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        claim_ttl_seconds=0.01,
    )

    first = await relay.run_once()
    assert first.claimed == 1
    assert first.published == 0
    assert first.failed == 1
    assert first.dead_lettered == 0

    async with store._pool.acquire() as conn:
        pending_row = await conn.fetchrow(
            """
            SELECT status, attempts, last_error
            FROM outbox
            ORDER BY outbox_id ASC
            LIMIT 1
            """
        )
        assert pending_row is not None
        assert pending_row["status"] == "pending"
        assert int(pending_row["attempts"]) == 1
        assert "simulated_publish_failure" in str(pending_row["last_error"])

        await conn.execute(
            """
            UPDATE outbox
            SET next_attempt_at = NOW() - INTERVAL '1 second'
            """
        )

    second = await relay.run_once()
    assert second.claimed == 1
    assert second.published == 0
    assert second.failed == 1
    assert second.dead_lettered == 1

    async with store._pool.acquire() as conn:
        dead_row = await conn.fetchrow(
            """
            SELECT status, attempts, last_error
            FROM outbox
            ORDER BY outbox_id ASC
            LIMIT 1
            """
        )
        assert dead_row is not None
        assert dead_row["status"] == "dead_letter"
        assert int(dead_row["attempts"]) == 2
        assert "simulated_publish_failure" in str(dead_row["last_error"])
