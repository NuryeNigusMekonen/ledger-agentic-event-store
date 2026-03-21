from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import (
    AgentContextLoadedEvent,
    ApplicationSubmittedEvent,
    ComplianceCheckRequestedEvent,
    ComplianceRulePassedEvent,
    CreditAnalysisCompletedEvent,
    DecisionGeneratedEvent,
)
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping projection integration test.")
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
              outbox,
              events,
              event_streams,
              projection_checkpoints,
              application_summary_projection,
              compliance_audit_state_projection,
              compliance_audit_view_projection,
              agent_performance_projection
            RESTART IDENTITY CASCADE
            """
        )

    yield event_store
    await event_store.close()


@pytest.mark.asyncio
async def test_projection_daemon_updates_tables_and_lag(store: EventStore) -> None:
    app_id = f"app-{uuid4()}"
    agent_id = "agent-01"
    session_id = "s1"
    agent_stream = f"agent-{agent_id}-{session_id}"

    await store.append(
        stream_id=f"loan-{app_id}",
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            ApplicationSubmittedEvent(
                payload={"application_id": app_id, "requested_amount_usd": 10000},
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": app_id,
                    "recommendation": "APPROVE",
                    "compliance_status": "CLEARED",
                    "assessed_max_limit_usd": 12000,
                    "contributing_agent_sessions": [agent_stream],
                    "orchestrator_agent_id": "orchestrator-1",
                    "confidence_score": 0.9,
                    "model_versions": {"orchestrator-1": "orchestrator-v2"},
                },
            ),
        ],
    )
    await store.append(
        stream_id=agent_stream,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            AgentContextLoadedEvent(
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "credit-v1",
                },
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": app_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "credit-v1",
                    "confidence_score": 0.82,
                    "recommended_limit_usd": 11000,
                },
            ),
        ],
    )

    daemon = ProjectionDaemon(
        store=store,
        projections=[
            ApplicationSummaryProjection(),
            ComplianceAuditViewProjection(),
            AgentPerformanceLedgerProjection(),
        ],
        batch_size=100,
    )
    await daemon.initialize()
    await daemon.run_once()

    async with store._pool.acquire() as conn:
        app_row = await conn.fetchrow(
            """
            SELECT current_state, decision_recommendation, compliance_status
            FROM application_summary_projection
            WHERE application_id = $1
            """,
            app_id,
        )
        assert app_row is not None
        assert app_row["current_state"] == "APPROVED_PENDING_HUMAN"
        assert app_row["decision_recommendation"] == "APPROVE"
        assert app_row["compliance_status"] == "CLEARED"

        agent_row = await conn.fetchrow(
            """
            SELECT sessions_started, analyses_completed, avg_confidence_score
            FROM agent_performance_projection
            WHERE agent_id = $1 AND model_version = $2
            """,
            agent_id,
            "credit-v1",
        )
        assert agent_row is not None
        assert agent_row["sessions_started"] == 1
        assert agent_row["analyses_completed"] == 1
        assert float(agent_row["avg_confidence_score"]) > 0

    lags = await daemon.get_all_lags()
    assert set(lags) == {
        "application_summary",
        "compliance_audit_view",
        "agent_performance_ledger",
    }
    assert all(metric.events_behind == 0 for metric in lags.values())


@pytest.mark.asyncio
async def test_compliance_temporal_query_and_rebuild(store: EventStore) -> None:
    projection = ComplianceAuditViewProjection()
    daemon = ProjectionDaemon(
        store=store,
        projections=[projection],
        batch_size=100,
    )
    await daemon.initialize()

    app_id = f"app-{uuid4()}"
    stream_id = f"compliance-{app_id}"

    first = await store.append(
        stream_id=stream_id,
        aggregate_type="ComplianceRecord",
        expected_version=0,
        events=[
            ComplianceCheckRequestedEvent(
                payload={
                    "application_id": app_id,
                    "regulation_set_version": "2026.03",
                    "checks_required": ["rule-a", "rule-b"],
                },
            )
        ],
    )
    second = await store.append(
        stream_id=stream_id,
        aggregate_type="ComplianceRecord",
        expected_version=1,
        events=[
            ComplianceRulePassedEvent(
                payload={
                    "application_id": app_id,
                    "rule_id": "rule-a",
                    "rule_version": "v1",
                },
            )
        ],
    )
    third = await store.append(
        stream_id=stream_id,
        aggregate_type="ComplianceRecord",
        expected_version=2,
        events=[
            ComplianceRulePassedEvent(
                payload={
                    "application_id": app_id,
                    "rule_id": "rule-b",
                    "rule_version": "v1",
                },
            )
        ],
    )

    await daemon.run_once()

    as_of_pending = second.events[0].recorded_at
    as_of_cleared = third.events[0].recorded_at

    async with store._pool.acquire() as conn:
        pending_view = await projection.get_compliance_at(conn, app_id, as_of_pending)
        assert pending_view is not None
        assert pending_view["compliance_status"] == "PENDING"

        cleared_view = await projection.get_compliance_at(conn, app_id, as_of_cleared)
        assert cleared_view is not None
        assert cleared_view["compliance_status"] == "CLEARED"

    await daemon.rebuild_projection("compliance_audit_view")

    async with store._pool.acquire() as conn:
        current = await projection.get_current(conn, app_id)
        assert current is not None
        assert current["compliance_status"] == "CLEARED"
        assert int(current["last_global_position"]) >= first.events[0].global_position
