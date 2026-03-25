from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import BaseEvent
from src.upcasting.registry import UpcasterRegistry
from src.upcasting.upcasters import (
    upcast_credit_analysis_completed_v1_to_v2,
    upcast_decision_generated_v1_to_v2,
)

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
    assert loaded[0].payload["model_version"] in {"credit-v2", "credit-v1.5", "credit-v1"}
    assert loaded[0].payload["confidence_score"] is None
    assert loaded[0].payload["regulatory_basis"] is not None

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


@pytest.mark.asyncio
async def test_decision_generated_v1_upcasted_to_v2_without_mutating_raw_row(
    store: EventStore,
) -> None:
    stream_id = f"loan-{uuid4()}"
    original_payload = {
        "application_id": "app-legacy",
        "recommendation": "REFER",
        "orchestrator_agent_id": "orchestrator-1",
        "contributing_agent_sessions": ["agent-risk-team-s1", "agent-fraud-team-s2"],
        "decision_basis_summary": "legacy decision body",
    }
    await store.append(
        stream_id=stream_id,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="DecisionGenerated",
                event_version=1,
                payload=original_payload,
                metadata={
                    "agent_model_versions": {
                        "risk-team": "risk-v2",
                        "fraud-team": "fraud-v4",
                    },
                    "orchestrator_model_version": "orch-v3",
                },
            )
        ],
    )

    loaded = await store.load_stream(stream_id)
    assert len(loaded) == 1
    assert loaded[0].event_version == 2
    assert loaded[0].payload["model_versions"]["risk-team"] == "risk-v2"
    assert loaded[0].payload["model_versions"]["fraud-team"] == "fraud-v4"
    assert loaded[0].payload["model_versions"]["orchestrator-1"] == "orch-v3"

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


def test_credit_upcaster_prefers_payload_model_version_and_nulls_unknown_confidence() -> None:
    payload = {
        "application_id": "app-1",
        "agent_id": "agent01",
        "session_id": "s1",
        "model_version": "credit-v2",
        "confidence_score": 0.87,
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 50000,
        "analysis_duration_ms": 142,
        "input_data_hash": "hash-abc",
    }
    metadata = {}

    upcasted_payload, upcasted_metadata = upcast_credit_analysis_completed_v1_to_v2(
        payload,
        metadata,
    )

    assert upcasted_payload["model_version"] == "credit-v2"
    assert upcasted_payload["confidence_score"] == 0.87
    assert (
        upcasted_metadata["upcast_notes"]["model_version_inference_method"]
        == "payload:model_version"
    )
    assert upcasted_metadata["upcast_notes"]["confidence_score_inference_method"] == (
        "payload:confidence_score"
    )


def test_credit_upcaster_uses_null_when_confidence_unknown() -> None:
    payload = {
        "application_id": "app-1",
        "agent_id": "agent01",
        "session_id": "s1",
        "risk_tier": "MEDIUM",
    }
    metadata = {"__recorded_at": "2026-02-01T00:00:00+00:00"}
    upcasted_payload, upcasted_metadata = upcast_credit_analysis_completed_v1_to_v2(
        payload,
        metadata,
    )
    assert upcasted_payload["confidence_score"] is None
    assert upcasted_payload["model_version"] == "credit-v2"
    assert upcasted_payload["regulatory_basis"] == "regset-2026.1"
    assert upcasted_metadata["upcast_notes"]["regulatory_basis_inference_method"] == (
        "timestamp:active_rule_versions"
    )


def test_decision_upcaster_reconstructs_model_versions_from_contributing_sessions() -> None:
    payload = {
        "application_id": "app-9",
        "recommendation": "APPROVE",
        "orchestrator_agent_id": "orchestrator-1",
        "contributing_agent_sessions": ["agent-credit-agent-s55", "agent-fraud-agent-s99"],
    }
    metadata = {
        "agent_model_versions": {
            "credit-agent": "credit-v3",
            "fraud-agent": "fraud-v7",
        },
        "orchestrator_model_version": "orch-v5",
    }
    upcasted_payload, _ = upcast_decision_generated_v1_to_v2(payload, metadata)
    assert upcasted_payload["model_versions"]["credit-agent"] == "credit-v3"
    assert upcasted_payload["model_versions"]["fraud-agent"] == "fraud-v7"
    assert upcasted_payload["model_versions"]["orchestrator-1"] == "orch-v5"


def test_registry_decorator_registration_applies_full_version_chain() -> None:
    registry = UpcasterRegistry()

    @registry.upcaster(event_type="SyntheticEvent", from_version=1, to_version=2)
    def _v1_to_v2(
        payload: dict[str, object],
        metadata: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        next_payload = dict(payload)
        next_payload["phase"] = "v2"
        return next_payload, metadata

    @registry.upcaster(event_type="SyntheticEvent", from_version=2, to_version=3)
    def _v2_to_v3(
        payload: dict[str, object],
        metadata: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        next_payload = dict(payload)
        next_payload["phase"] = "v3"
        return next_payload, metadata

    result = registry.upcast(
        event_type="SyntheticEvent",
        version=1,
        payload={"phase": "v1"},
        metadata={},
    )
    assert result.current_version == 3
    assert result.payload["phase"] == "v3"
    assert result.applied_steps == [
        "SyntheticEvent:v1->v2",
        "SyntheticEvent:v2->v3",
    ]
