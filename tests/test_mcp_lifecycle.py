from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import dotenv_values, load_dotenv

from src.event_store import EventStore
from src.mcp.server import LedgerMCPServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping MCP integration test.")
    return value


@pytest_asyncio.fixture
async def server() -> LedgerMCPServer:
    database_url = _database_url()
    try:
        store = await EventStore.from_dsn(
            database_url,
            min_size=1,
            max_size=10,
            connect_timeout=2.0,
        )
    except Exception as exc:  # pragma: no cover - integration environment dependent
        pytest.skip(f"PostgreSQL is not reachable for integration test: {exc}")

    await store.apply_schema(PROJECT_ROOT / "src" / "schema.sql")
    async with store._pool.acquire() as conn:
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

    mcp_server = LedgerMCPServer(store=store, auto_project=True)
    await mcp_server.initialize()
    yield mcp_server
    await store.close()


@pytest.mark.asyncio
async def test_mcp_full_lifecycle_via_tools_and_resources(server: LedgerMCPServer) -> None:
    assert len(server.list_tools()) == 8
    assert len(server.list_resources()) == 6

    app_id = f"app-{uuid4()}"
    agent_id = "credit-agent-1"
    session_id = "session-1"
    session_stream = f"agent-{agent_id}-{session_id}"

    submit_result = await server.call_tool(
        "submit_application",
        {
            "application_id": app_id,
            "applicant_id": "customer-123",
            "requested_amount_usd": 10000,
            "loan_purpose": "equipment financing",
            "submission_channel": "portal",
            "submitted_at": datetime.now(UTC).isoformat(),
        },
    )
    assert submit_result["ok"] is True

    start_session = await server.call_tool(
        "start_agent_session",
        {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": "event-replay",
            "event_replay_from_position": 1,
            "context_token_count": 1200,
            "model_version": "credit-v2",
        },
    )
    assert start_session["ok"] is True

    credit = await server.call_tool(
        "record_credit_analysis",
        {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "credit-v2",
            "confidence_score": 0.87,
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": 9500,
            "analysis_duration_ms": 142,
            "input_data_hash": "hash-credit-001",
        },
    )
    assert credit["ok"] is True

    fraud = await server.call_tool(
        "record_fraud_screening",
        {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "fraud_score": 0.08,
            "anomaly_flags": [],
            "screening_model_version": "credit-v2",
            "input_data_hash": "hash-fraud-001",
        },
    )
    assert fraud["ok"] is True

    compliance_1 = await server.call_tool(
        "record_compliance_check",
        {
            "application_id": app_id,
            "regulation_set_version": "2026.03",
            "rule_id": "rule-a",
            "rule_version": "v1",
            "passed": True,
            "checks_required": ["rule-a", "rule-b"],
        },
    )
    assert compliance_1["ok"] is True

    compliance_2 = await server.call_tool(
        "record_compliance_check",
        {
            "application_id": app_id,
            "regulation_set_version": "2026.03",
            "rule_id": "rule-b",
            "rule_version": "v1",
            "passed": True,
        },
    )
    assert compliance_2["ok"] is True

    decision = await server.call_tool(
        "generate_decision",
        {
            "application_id": app_id,
            "orchestrator_agent_id": "orchestrator-1",
            "recommendation": "APPROVE",
            "confidence_score": 0.91,
            "decision_basis_summary": "credit/fraud/compliance all acceptable",
            "contributing_agent_sessions": [session_stream],
            "model_versions": {"orchestrator-1": "orch-v1"},
        },
    )
    assert decision["ok"] is True
    assert decision["result"]["recommendation"] == "APPROVE"

    review = await server.call_tool(
        "record_human_review",
        {
            "application_id": app_id,
            "reviewer_id": "loan-officer-7",
            "override": False,
            "final_decision": "APPROVE",
            "approved_amount_usd": 9000,
            "interest_rate": 7.2,
            "conditions": ["signed guarantee"],
            "effective_date": "2026-03-17",
        },
    )
    assert review["ok"] is True
    assert review["result"]["application_state"] == "ApplicationApproved"

    compliance_view = await server.read_resource(f"ledger://applications/{app_id}/compliance")
    assert compliance_view["ok"] is True
    assert compliance_view["result"]["snapshot"]["compliance_status"] == "CLEARED"
    assert len(compliance_view["result"]["timeline"]) >= 3

    app_summary = await server.read_resource(f"ledger://applications/{app_id}")
    assert app_summary["ok"] is True
    assert app_summary["result"]["current_state"] == "FINAL_APPROVED"

    integrity = await server.call_tool(
        "run_integrity_check",
        {
            "entity_type": "application",
            "entity_id": app_id,
            "role": "compliance",
        },
    )
    assert integrity["ok"] is True
    assert "chain_valid" in integrity["result"]


@pytest.mark.asyncio
async def test_mcp_returns_structured_precondition_error(server: LedgerMCPServer) -> None:
    app_id = f"app-{uuid4()}"
    result = await server.call_tool(
        "record_credit_analysis",
        {
            "application_id": app_id,
            "agent_id": "missing-agent",
            "session_id": "missing-session",
            "model_version": "v1",
            "confidence_score": 0.8,
            "risk_tier": "MEDIUM",
            "recommended_limit_usd": 4000,
            "analysis_duration_ms": 55,
            "input_data_hash": "hash",
        },
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "PreconditionFailed"
    assert "suggested_action" in result["error"]


@pytest.mark.asyncio
async def test_submit_application_can_process_document_and_emit_docpkg_events(
    server: LedgerMCPServer,
    tmp_path: Path,
) -> None:
    app_id = f"app-{uuid4()}"
    document_path = tmp_path / "application_financials.txt"
    document_path.write_text(
        (
            "Total Revenue: 1250000\n"
            "Net Income: 210000\n"
            "EBITDA: 320000\n"
            "Total Assets: 4500000\n"
            "Total Liabilities: 1700000\n"
        ),
        encoding="utf-8",
    )

    submit_result = await server.call_tool(
        "submit_application",
        {
            "application_id": app_id,
            "applicant_id": "customer-xyz",
            "requested_amount_usd": 12000,
            "loan_purpose": "fleet purchase",
            "submission_channel": "portal",
            "submitted_at": datetime.now(UTC).isoformat(),
            "document_path": str(document_path),
            "process_documents_after_submit": True,
        },
    )
    assert submit_result["ok"] is True

    doc_events = await server.store.load_stream(f"docpkg-{app_id}")
    doc_event_types = [event.event_type for event in doc_events]
    assert "ExtractionCompleted" in doc_event_types
    assert "QualityAssessmentCompleted" in doc_event_types
    assert "PackageReadyForAnalysis" in doc_event_types

    extraction_event = next(
        event for event in doc_events if event.event_type == "ExtractionCompleted"
    )
    assert extraction_event.payload["facts"]["total_revenue"] == 1250000.0
    assert extraction_event.payload["facts"]["net_income"] == 210000.0

    loan_events = await server.store.load_stream(f"loan-{app_id}")
    assert any(event.event_type == "CreditAnalysisRequested" for event in loan_events)
