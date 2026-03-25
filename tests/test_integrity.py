from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.integrity.audit_chain import attach_integrity_chain, run_integrity_check
from src.models.events import ApplicationSubmittedEvent, DecisionGeneratedEvent

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
        ApplicationSubmittedEvent(
            payload={"application_id": "app-1", "requested_amount_usd": 1000},
        ),
        DecisionGeneratedEvent(
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
    assert valid.tamper_detected is False
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
    assert tampered.tamper_detected is True
    assert len(tampered.violations) >= 1
    assert tampered.violations[0].stream_position == 2


@pytest.mark.asyncio
async def test_store_append_auto_attaches_integrity_metadata(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": "app-2", "requested_amount_usd": 2500},
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": "app-2",
                    "recommendation": "REFER",
                    "model_versions": {"orchestrator-1": "v2"},
                    "contributing_agent_sessions": [],
                    "decision_basis_summary": "requires manual adjudication",
                },
            ),
        ],
    )

    result = await run_integrity_check(store, stream_id)
    assert result.chain_valid is True
    assert result.tamper_detected is False
    assert result.events_verified_count == 2


@pytest.mark.asyncio
async def test_backfill_integrity_hashes_repairs_legacy_missing_metadata(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": "app-3", "requested_amount_usd": 1500},
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": "app-3",
                    "recommendation": "APPROVE",
                    "model_versions": {"orchestrator-1": "v2"},
                    "contributing_agent_sessions": [],
                    "decision_basis_summary": "eligible with conditions",
                },
            ),
        ],
    )

    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE events
            SET metadata = metadata - 'previous_hash' - 'integrity_hash'
            WHERE stream_id = $1
            """,
            stream_id,
        )
        await conn.execute(
            """
            UPDATE event_streams
            SET metadata = metadata - 'last_integrity_hash'
            WHERE stream_id = $1
            """,
            stream_id,
        )

    invalid_before = await run_integrity_check(store, stream_id)
    assert invalid_before.chain_valid is False
    assert invalid_before.tamper_detected is True

    preview = await store.backfill_integrity_hashes(stream_id=stream_id, dry_run=True)
    assert preview.events_repaired == 2
    assert preview.streams_metadata_repaired == 1
    assert preview.unresolved_violations == 0

    still_invalid = await run_integrity_check(store, stream_id)
    assert still_invalid.chain_valid is False
    assert still_invalid.tamper_detected is True

    applied = await store.backfill_integrity_hashes(stream_id=stream_id, dry_run=False)
    assert applied.events_repaired == 2
    assert applied.streams_metadata_repaired == 1
    assert applied.unresolved_violations == 0

    valid_after = await run_integrity_check(store, stream_id)
    assert valid_after.chain_valid is True
    assert valid_after.tamper_detected is False

    idempotent = await store.backfill_integrity_hashes(stream_id=stream_id, dry_run=False)
    assert idempotent.events_repaired == 0
    assert idempotent.streams_metadata_repaired == 0


@pytest.mark.asyncio
async def test_run_integrity_check_can_append_audit_event(store: EventStore) -> None:
    stream_id = f"loan-{uuid4()}"
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": "app-audit", "requested_amount_usd": 5100},
            ),
        ],
    )

    result = await run_integrity_check(
        store=store,
        stream_id=stream_id,
        append_audit_event=True,
        audit_entity_type="application",
        audit_entity_id="app-audit",
    )
    assert result.chain_valid is True
    assert result.tamper_detected is False
    assert result.audit_stream_id == "audit-application-app-audit"
    assert result.audit_event_id is not None

    audit_events = await store.load_stream("audit-application-app-audit")
    assert len(audit_events) == 1
    assert audit_events[0].event_type == "AuditIntegrityCheckRun"
    assert audit_events[0].payload.get("tamper_detected") is False
