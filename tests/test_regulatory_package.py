from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.integrity.audit_chain import attach_integrity_chain
from src.models.events import (
    AgentContextLoadedEvent,
    ApplicationApprovedEvent,
    ApplicationSubmittedEvent,
    ComplianceCheckRequestedEvent,
    ComplianceRulePassedEvent,
    CreditAnalysisCompletedEvent,
    DecisionGeneratedEvent,
    HumanReviewCompletedEvent,
)
from src.regulatory.package import generate_regulatory_package

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping regulatory package integration test.")
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

    await event_store.apply_schema(PROJECT_ROOT / "src" / "schema.sql")
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
async def test_generate_regulatory_package_outputs_self_contained_json(store: EventStore) -> None:
    app_id = f"app-{uuid4()}"
    loan_stream = f"loan-{app_id}"
    agent_stream = "agent-credit-agent-2-session-9"
    compliance_stream = f"compliance-{app_id}"

    loan_events = attach_integrity_chain(
        stream_id=loan_stream,
        expected_version=0,
        events=[
            ApplicationSubmittedEvent(
                payload={
                    "application_id": app_id,
                    "applicant_id": "customer-1",
                    "requested_amount_usd": 15000,
                    "loan_purpose": "expansion",
                    "submission_channel": "portal",
                    "submitted_at": datetime.now(UTC).isoformat(),
                },
                metadata={"correlation_id": "corr-reg-1"},
            ),
            DecisionGeneratedEvent(
                payload={
                    "application_id": app_id,
                    "orchestrator_agent_id": "orchestrator-1",
                    "recommendation": "APPROVE",
                    "confidence_score": 0.89,
                    "contributing_agent_sessions": [agent_stream],
                    "decision_basis_summary": "good profile",
                    "model_versions": {"orchestrator-1": "orch-v2"},
                    "compliance_status": "CLEARED",
                    "assessed_max_limit_usd": 16000,
                },
                metadata={"correlation_id": "corr-reg-1"},
            ),
            HumanReviewCompletedEvent(
                payload={
                    "application_id": app_id,
                    "reviewer_id": "loan-officer-2",
                    "override": False,
                    "final_decision": "APPROVE",
                },
                metadata={"correlation_id": "corr-reg-1"},
            ),
            ApplicationApprovedEvent(
                payload={
                    "application_id": app_id,
                    "approved_amount_usd": 14000,
                    "interest_rate": 6.9,
                    "conditions": ["board resolution"],
                    "approved_by": "loan-officer-2",
                    "effective_date": "2026-03-17",
                },
                metadata={"correlation_id": "corr-reg-1"},
            ),
        ],
    )
    await store.append(
        stream_id=loan_stream,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=loan_events,
    )

    await store.append(
        stream_id=agent_stream,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            AgentContextLoadedEvent(
                payload={
                    "agent_id": "credit-agent-2",
                    "session_id": "session-9",
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 900,
                    "model_version": "credit-v3",
                },
            ),
            CreditAnalysisCompletedEvent(
                payload={
                    "application_id": app_id,
                    "agent_id": "credit-agent-2",
                    "session_id": "session-9",
                    "model_version": "credit-v3",
                    "confidence_score": 0.84,
                    "risk_tier": "LOW",
                    "recommended_limit_usd": 16000,
                    "analysis_duration_ms": 88,
                    "input_data_hash": "hash-credit-xyz",
                },
            ),
        ],
    )

    await store.append(
        stream_id=compliance_stream,
        aggregate_type="ComplianceRecord",
        expected_version=0,
        events=[
            ComplianceCheckRequestedEvent(
                payload={
                    "application_id": app_id,
                    "regulation_set_version": "2026.03",
                    "checks_required": ["rule-a"],
                },
            ),
            ComplianceRulePassedEvent(
                payload={
                    "application_id": app_id,
                    "rule_id": "rule-a",
                    "rule_version": "v1",
                    "evaluation_timestamp": datetime.now(UTC).isoformat(),
                    "evidence_hash": "evidence-1",
                },
            ),
        ],
    )

    out_path = PROJECT_ROOT / "tmp" / f"reg_package_{app_id}.json"
    result = await generate_regulatory_package(
        store=store,
        application_id=app_id,
        examination_date=datetime.now(UTC) + timedelta(minutes=1),
        output_path=out_path,
    )

    assert result.output_path is not None
    assert Path(result.output_path).exists()
    assert result.package["application_id"] == app_id
    assert result.package["audit_chain_integrity"]["chain_valid"] is True
    assert (
        result.package["projection_states_at_examination"]["application_summary"]["current_state"]
        == "FINAL_APPROVED"
    )
    assert len(result.package["agent_model_metadata"]) >= 1
    assert result.package["verification"]["package_hash_sha256"] == result.package_hash
    Path(result.output_path).unlink(missing_ok=True)
