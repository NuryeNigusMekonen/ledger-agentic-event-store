from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.models.events import BaseEvent
from src.what_if.projector import run_what_if

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping what-if integration test.")
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
async def test_run_what_if_produces_materially_different_outcome(store: EventStore) -> None:
    app_id = f"app-{uuid4()}"
    loan_stream = f"loan-{app_id}"
    agent_stream = "agent-credit-agent-1-session-1"

    await store.append(
        stream_id=loan_stream,
        aggregate_type="LoanApplication",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={"application_id": app_id, "requested_amount_usd": 10000},
                metadata={"correlation_id": "corr-1"},
            )
        ],
    )
    await store.append(
        stream_id=agent_stream,
        aggregate_type="AgentSession",
        expected_version=0,
        events=[
            BaseEvent(
                event_type="AgentContextLoaded",
                payload={
                    "agent_id": "credit-agent-1",
                    "session_id": "session-1",
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 1000,
                    "model_version": "credit-v2",
                },
                metadata={"correlation_id": "corr-1"},
            ),
            BaseEvent(
                event_type="CreditAnalysisCompleted",
                event_version=2,
                payload={
                    "application_id": app_id,
                    "agent_id": "credit-agent-1",
                    "session_id": "session-1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.88,
                    "risk_tier": "MEDIUM",
                    "recommended_limit_usd": 9500,
                    "analysis_duration_ms": 120,
                    "input_data_hash": "hash-credit",
                },
                metadata={"correlation_id": "corr-1"},
            ),
        ],
    )
    credit_event = (await store.load_stream(agent_stream))[-1]

    decision = await store.append(
        stream_id=loan_stream,
        aggregate_type="LoanApplication",
        expected_version=1,
        events=[
            BaseEvent(
                event_type="DecisionGenerated",
                event_version=2,
                payload={
                    "application_id": app_id,
                    "orchestrator_agent_id": "orchestrator-1",
                    "recommendation": "APPROVE",
                    "confidence_score": 0.90,
                    "contributing_agent_sessions": [agent_stream],
                    "decision_basis_summary": "all green",
                    "model_versions": {"orchestrator-1": "orch-v1"},
                    "compliance_status": "CLEARED",
                    "assessed_max_limit_usd": 9800,
                },
                metadata={
                    "correlation_id": "corr-1",
                    "causation_id": str(credit_event.event_id),
                },
            )
        ],
    )
    decision_event_id = str(decision.events[-1].event_id)

    review = await store.append(
        stream_id=loan_stream,
        aggregate_type="LoanApplication",
        expected_version=2,
        events=[
            BaseEvent(
                event_type="HumanReviewCompleted",
                payload={
                    "application_id": app_id,
                    "reviewer_id": "human-1",
                    "override": False,
                    "final_decision": "APPROVE",
                },
                metadata={
                    "correlation_id": "corr-1",
                    "causation_id": decision_event_id,
                },
            ),
            BaseEvent(
                event_type="ApplicationApproved",
                payload={
                    "application_id": app_id,
                    "approved_amount_usd": 9000,
                    "interest_rate": 7.1,
                    "conditions": [],
                    "approved_by": "human-1",
                    "effective_date": "2026-03-17",
                },
                metadata={
                    "correlation_id": "corr-1",
                    "causation_id": decision_event_id,
                },
            ),
        ],
    )
    assert review.new_stream_version == 4

    what_if = await run_what_if(
        store=store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[
            BaseEvent(
                event_type="CreditAnalysisCompleted",
                event_version=2,
                payload={
                    "application_id": app_id,
                    "agent_id": "credit-agent-1",
                    "session_id": "session-1",
                    "model_version": "credit-v2",
                    "confidence_score": 0.77,
                    "risk_tier": "HIGH",
                    "recommended_limit_usd": 2000,
                    "analysis_duration_ms": 130,
                    "input_data_hash": "hash-credit-cf",
                },
                metadata={},
            ),
            BaseEvent(
                event_type="DecisionGenerated",
                event_version=2,
                payload={
                    "application_id": app_id,
                    "orchestrator_agent_id": "orchestrator-1",
                    "recommendation": "DECLINE",
                    "confidence_score": 0.92,
                    "contributing_agent_sessions": [agent_stream],
                    "decision_basis_summary": "counterfactual high risk",
                    "model_versions": {"orchestrator-1": "orch-v1"},
                    "compliance_status": "CLEARED",
                    "assessed_max_limit_usd": 2000,
                },
                metadata={"stream_id_override": loan_stream},
            ),
            BaseEvent(
                event_type="ApplicationDeclined",
                payload={
                    "application_id": app_id,
                    "decline_reasons": ["counterfactual high risk"],
                    "declined_by": "auto",
                    "adverse_action_notice_required": True,
                },
                metadata={"stream_id_override": loan_stream},
            ),
        ],
        projections=["application_summary"],
    )

    assert what_if.real_outcome["application_summary"]["current_state"] == "FINAL_APPROVED"
    assert (
        what_if.counterfactual_outcome["application_summary"]["current_state"]
        == "FINAL_DECLINED"
    )
    assert len(what_if.divergence_events) >= 2
