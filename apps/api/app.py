from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.event_store import EventStore
from src.mcp.server import LedgerMCPServer

from .auth import (
    COMMAND_ROLE_POLICY,
    AuthPrincipal,
    can_bootstrap_demo,
    can_invoke_command,
    can_rebuild_projections,
    can_view_auth_audit,
    configured_seed_users,
    create_password_hash,
    decode_access_token,
    issue_access_token,
    verify_password,
)
from .settings import AppSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "src" / "schema.sql"

PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/login",
}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)


class RebuildProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_name: str | None = None


class DemoScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str | None = None
    applicant_id: str = "et-borrower-001"
    agent_id: str = "credit-agent-ethi-01"
    session_id: str | None = None


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved_settings = settings or AppSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = await EventStore.from_dsn(resolved_settings.database_url, min_size=1, max_size=12)
        if resolved_settings.apply_schema_on_start:
            await store.apply_schema(SCHEMA_PATH)
        await _ensure_auth_schema(store)

        if resolved_settings.seed_demo_users:
            await _seed_demo_users(store, configured_seed_users())

        mcp_server = LedgerMCPServer(store=store, auto_project=True)
        await mcp_server.initialize()

        app.state.settings = resolved_settings
        app.state.store = store
        app.state.mcp = mcp_server
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(
        title="Ledger Event Store API",
        version="0.3.0",
        summary="Operational API layer with JWT auth, RBAC, and audit logging",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_origin_regex=resolved_settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        api_key = resolved_settings.api_key
        if not api_key:
            return await call_next(request)

        path = request.url.path
        if path in {"/api/v1/health", "/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)

        if path.startswith("/api/v1"):
            provided = request.headers.get("x-api-key")
            if provided != api_key:
                return _json_response(
                    status_code=401,
                    content=_error_payload(
                        error_type="AuthorizationError",
                        message="Missing or invalid API key.",
                        suggested_action="provide_x_api_key_header",
                    ),
                )
        return await call_next(request)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not path.startswith("/api/v1") or path in PUBLIC_PATHS:
            return await call_next(request)

        if path in {"/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _json_response(
                status_code=401,
                content=_error_payload(
                    error_type="AuthenticationRequired",
                    message="Bearer token required.",
                    suggested_action="login_and_include_bearer_token",
                ),
            )

        try:
            principal = decode_access_token(
                token,
                secret=resolved_settings.jwt_secret,
                issuer=resolved_settings.jwt_issuer,
            )
        except ValueError as exc:
            await _write_auth_audit(
                app.state.store,
                username=None,
                role=None,
                action="auth_token_rejected",
                success=False,
                request=request,
                details={"reason": str(exc)},
            )
            return _json_response(
                status_code=401,
                content=_error_payload(
                    error_type="AuthenticationFailed",
                    message=str(exc),
                    suggested_action="login_and_retry",
                ),
            )

        async with app.state.store._pool.acquire() as conn:
            auth_row = await conn.fetchrow(
                """
                SELECT role, is_active
                FROM auth_users
                WHERE username = $1
                """,
                principal.username,
            )
        if auth_row is None or not bool(auth_row["is_active"]):
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action="auth_token_rejected",
                success=False,
                request=request,
                details={"reason": "user_inactive_or_missing"},
            )
            return _json_response(
                status_code=401,
                content=_error_payload(
                    error_type="AuthenticationFailed",
                    message="Token user is inactive.",
                    suggested_action="login_with_active_account",
                ),
            )

        db_role = str(auth_row["role"])
        if db_role != principal.role:
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action="auth_token_rejected",
                success=False,
                request=request,
                details={"reason": "role_mismatch"},
            )
            return _json_response(
                status_code=401,
                content=_error_payload(
                    error_type="AuthenticationFailed",
                    message="Token role no longer matches account role.",
                    suggested_action="login_and_retry",
                ),
            )

        request.state.principal = principal
        return await call_next(request)

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        async with app.state.store._pool.acquire() as conn:
            now = await conn.fetchval("SELECT NOW()")
        return {
            "ok": True,
            "result": {
                "service": "ledger-api",
                "status": "healthy",
                "time": now.isoformat(),
            },
        }

    @app.post("/api/v1/auth/login")
    async def login(request: Request, payload: LoginRequest) -> JSONResponse:
        async with app.state.store._pool.acquire() as conn:
            user_row = await conn.fetchrow(
                """
                SELECT username, password_hash, role, is_active
                FROM auth_users
                WHERE username = $1
                """,
                payload.username,
            )

        invalid_response = _json_response(
            status_code=401,
            content=_error_payload(
                error_type="AuthenticationFailed",
                message="Invalid username or password.",
                suggested_action="verify_credentials_and_retry",
            ),
        )

        if user_row is None or not bool(user_row["is_active"]):
            await _write_auth_audit(
                app.state.store,
                username=payload.username,
                role=None,
                action="auth_login",
                success=False,
                request=request,
                details={"reason": "unknown_or_inactive_user"},
            )
            return invalid_response

        stored_hash = str(user_row["password_hash"])
        if not verify_password(payload.password, stored_hash):
            await _write_auth_audit(
                app.state.store,
                username=payload.username,
                role=str(user_row["role"]),
                action="auth_login",
                success=False,
                request=request,
                details={"reason": "password_mismatch"},
            )
            return invalid_response

        username = str(user_row["username"])
        role = str(user_row["role"])
        try:
            token = issue_access_token(
                username=username,
                role=role,
                secret=resolved_settings.jwt_secret,
                issuer=resolved_settings.jwt_issuer,
                ttl_minutes=resolved_settings.jwt_ttl_minutes,
            )
            principal = decode_access_token(
                token,
                secret=resolved_settings.jwt_secret,
                issuer=resolved_settings.jwt_issuer,
            )
        except ValueError:
            await _write_auth_audit(
                app.state.store,
                username=username,
                role=role,
                action="auth_login",
                success=False,
                request=request,
                details={"reason": "unsupported_role"},
            )
            return invalid_response

        async with app.state.store._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE auth_users
                SET last_login_at = NOW(), updated_at = NOW()
                WHERE username = $1
                """,
                username,
            )

        await _write_auth_audit(
            app.state.store,
            username=username,
            role=role,
            action="auth_login",
            success=True,
            request=request,
            details={"expires_at": principal.expires_at.isoformat()},
        )

        return _json_response(
            status_code=200,
            content={
                "ok": True,
                "result": {
                    "access_token": token,
                    "token_type": "bearer",
                    "expires_at": principal.expires_at.isoformat(),
                    "user": {
                        "username": principal.username,
                        "role": principal.role,
                        "issued_at": principal.issued_at.isoformat(),
                    },
                    "allowed_commands": _allowed_commands_for_role(principal.role),
                },
            },
        )

    @app.get("/api/v1/auth/me")
    async def auth_me(request: Request) -> dict[str, Any]:
        principal = _principal_from_request(request)
        return {
            "ok": True,
            "result": {
                "username": principal.username,
                "role": principal.role,
                "issued_at": principal.issued_at.isoformat(),
                "expires_at": principal.expires_at.isoformat(),
                "allowed_commands": _allowed_commands_for_role(principal.role),
            },
        }

    @app.get("/api/v1/auth/audit")
    async def auth_audit(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> JSONResponse:
        principal = _principal_from_request(request)
        if not can_view_auth_audit(principal.role):
            return _role_forbidden_response(
                action="view_auth_audit",
                role=principal.role,
            )

        async with app.state.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  audit_id,
                  username,
                  role,
                  action,
                  success,
                  ip_address,
                  user_agent,
                  details,
                  created_at
                FROM auth_audit_log
                ORDER BY audit_id DESC
                LIMIT $1
                """,
                limit,
            )

        return _json_response(
            status_code=200,
            content={
                "ok": True,
                "result": {
                    "items": [
                        {
                            "audit_id": int(row["audit_id"]),
                            "username": row["username"],
                            "role": row["role"],
                            "action": row["action"],
                            "success": bool(row["success"]),
                            "ip_address": row["ip_address"],
                            "user_agent": row["user_agent"],
                            "details": dict(row["details"]),
                            "created_at": row["created_at"].isoformat(),
                        }
                        for row in rows
                    ],
                    "count": len(rows),
                },
            },
        )

    @app.get("/api/v1/tools")
    async def list_tools(request: Request) -> dict[str, Any]:
        principal = _principal_from_request(request)
        tools = app.state.mcp.list_tools()
        visible_tools = [
            item for item in tools if can_invoke_command(principal.role, str(item.get("name", "")))
        ]
        return {"ok": True, "result": {"tools": visible_tools, "count": len(visible_tools)}}

    @app.get("/api/v1/resources")
    async def list_resources() -> dict[str, Any]:
        resources = app.state.mcp.list_resources()
        return {"ok": True, "result": {"resources": resources, "count": len(resources)}}

    @app.post("/api/v1/commands/{tool_name}")
    async def call_command(
        request: Request,
        tool_name: str,
        request_body: CommandRequest,
    ) -> JSONResponse:
        principal = _principal_from_request(request)
        if not can_invoke_command(principal.role, tool_name):
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action=f"command:{tool_name}",
                success=False,
                request=request,
                details={"reason": "role_forbidden"},
            )
            return _role_forbidden_response(action=f"command:{tool_name}", role=principal.role)

        try:
            result = await asyncio.wait_for(
                app.state.mcp.call_tool(tool_name, request_body.arguments),
                timeout=resolved_settings.command_timeout_seconds,
            )
        except TimeoutError:
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action=f"command:{tool_name}",
                success=False,
                request=request,
                details={"reason": "timeout"},
            )
            return _json_response(
                status_code=504,
                content=_error_payload(
                    error_type="CommandTimeout",
                    message=f"Command '{tool_name}' timed out.",
                    suggested_action="retry_or_reduce_payload",
                ),
            )

        if result.get("ok"):
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action=f"command:{tool_name}",
                success=True,
                request=request,
                details={"result_keys": sorted(result.get("result", {}).keys())},
            )
            return _json_response(status_code=200, content=result)

        error = result.get("error", {})
        await _write_auth_audit(
            app.state.store,
            username=principal.username,
            role=principal.role,
            action=f"command:{tool_name}",
            success=False,
            request=request,
            details={
                "error_type": error.get("error_type"),
                "message": error.get("message"),
                "suggested_action": error.get("suggested_action"),
            },
        )
        status_code = _error_status_code(str(error.get("error_type", "InternalError")))
        return _json_response(status_code=status_code, content={"ok": False, "error": error})

    @app.post("/api/v1/bootstrap/demo")
    async def bootstrap_demo(request: Request, request_body: DemoScenarioRequest) -> JSONResponse:
        principal = _principal_from_request(request)
        if not can_bootstrap_demo(principal.role):
            return _role_forbidden_response(action="bootstrap_demo", role=principal.role)

        application_id = request_body.application_id or f"app-{uuid4()}"
        session_id = request_body.session_id or f"session-{uuid4().hex[:8]}"
        session_stream = f"agent-{request_body.agent_id}-{session_id}"

        steps = [
            (
                "submit_application",
                {
                    "application_id": application_id,
                    "applicant_id": request_body.applicant_id,
                    "requested_amount_usd": 1300000,
                    "loan_purpose": "import_financing",
                    "submission_channel": "addis-branch",
                    "submitted_at": datetime.now(UTC).isoformat(),
                },
            ),
            (
                "start_agent_session",
                {
                    "agent_id": request_body.agent_id,
                    "session_id": session_id,
                    "context_source": "event-replay",
                    "event_replay_from_position": 1,
                    "context_token_count": 1200,
                    "model_version": "credit-v2",
                },
            ),
            (
                "record_credit_analysis",
                {
                    "application_id": application_id,
                    "agent_id": request_body.agent_id,
                    "session_id": session_id,
                    "model_version": "credit-v2",
                    "confidence_score": 0.87,
                    "risk_tier": "MEDIUM",
                    "recommended_limit_usd": 1100000,
                    "analysis_duration_ms": 142,
                    "input_data_hash": "hash-credit-001",
                },
            ),
            (
                "record_fraud_screening",
                {
                    "application_id": application_id,
                    "agent_id": request_body.agent_id,
                    "session_id": session_id,
                    "fraud_score": 0.08,
                    "anomaly_flags": [],
                    "screening_model_version": "credit-v2",
                    "input_data_hash": "hash-fraud-001",
                },
            ),
            (
                "record_compliance_check",
                {
                    "application_id": application_id,
                    "regulation_set_version": "2026.03",
                    "rule_id": "rule-a",
                    "rule_version": "v1",
                    "passed": True,
                    "checks_required": ["rule-a", "rule-b"],
                },
            ),
            (
                "record_compliance_check",
                {
                    "application_id": application_id,
                    "regulation_set_version": "2026.03",
                    "rule_id": "rule-b",
                    "rule_version": "v1",
                    "passed": True,
                },
            ),
            (
                "generate_decision",
                {
                    "application_id": application_id,
                    "orchestrator_agent_id": "orchestrator-1",
                    "recommendation": "APPROVE",
                    "confidence_score": 0.91,
                    "decision_basis_summary": "credit/fraud/compliance acceptable",
                    "contributing_agent_sessions": [session_stream],
                    "model_versions": {"orchestrator-1": "orch-v1"},
                },
            ),
            (
                "record_human_review",
                {
                    "application_id": application_id,
                    "reviewer_id": "loan-officer-addis-01",
                    "override": False,
                    "final_decision": "APPROVE",
                    "approved_amount_usd": 1000000,
                    "interest_rate": 7.2,
                    "conditions": ["signed guarantee"],
                    "effective_date": datetime.now(UTC).date().isoformat(),
                },
            ),
        ]

        if principal.role in {"compliance", "admin"}:
            steps.append(
                (
                    "run_integrity_check",
                    {
                        "entity_type": "application",
                        "entity_id": application_id,
                        "role": principal.role,
                    },
                )
            )

        executed: list[dict[str, Any]] = []
        for step_name, args in steps:
            result = await app.state.mcp.call_tool(step_name, args)
            executed.append({"step": step_name, "result": result})
            if not result.get("ok"):
                error = result["error"]
                await _write_auth_audit(
                    app.state.store,
                    username=principal.username,
                    role=principal.role,
                    action="bootstrap_demo",
                    success=False,
                    request=request,
                    details={"step": step_name, "error_type": error.get("error_type")},
                )
                return _json_response(
                    status_code=_error_status_code(error.get("error_type", "InternalError")),
                    content={
                        "ok": False,
                        "error": error,
                        "result": {"application_id": application_id, "steps": executed},
                    },
                )

        summary = await app.state.mcp.read_resource(f"ledger://applications/{application_id}")
        await _write_auth_audit(
            app.state.store,
            username=principal.username,
            role=principal.role,
            action="bootstrap_demo",
            success=True,
            request=request,
            details={"application_id": application_id},
        )
        return _json_response(
            status_code=200,
            content={
                "ok": True,
                "result": {
                    "application_id": application_id,
                    "agent_id": request_body.agent_id,
                    "session_id": session_id,
                    "steps": executed,
                    "summary": summary,
                },
            },
        )

    @app.get("/api/v1/applications")
    async def list_applications(
        state: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        async with app.state.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  application_id,
                  current_state,
                  decision_recommendation,
                  final_decision,
                  requested_amount_usd,
                  approved_amount_usd,
                  assessed_max_limit_usd,
                  compliance_status,
                  last_event_type,
                  last_global_position,
                  updated_at
                FROM application_summary_projection
                WHERE ($1::text IS NULL OR current_state = $1)
                ORDER BY updated_at DESC
                LIMIT $2 OFFSET $3
                """,
                state,
                limit,
                offset,
            )
            total = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM application_summary_projection
                WHERE ($1::text IS NULL OR current_state = $1)
                """,
                state,
            )

        return {
            "ok": True,
            "result": {
                "items": [dict(row) for row in rows],
                "count": len(rows),
                "total": int(total),
                "limit": limit,
                "offset": offset,
            },
        }

    @app.get("/api/v1/application-states")
    async def application_states() -> dict[str, Any]:
        async with app.state.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT current_state, COUNT(*) AS total
                FROM application_summary_projection
                GROUP BY current_state
                ORDER BY total DESC, current_state ASC
                """
            )
        return {
            "ok": True,
            "result": {
                "states": [
                    {"state": row["current_state"], "count": int(row["total"])} for row in rows
                ]
            },
        }

    @app.get("/api/v1/applications/{application_id}")
    async def get_application(application_id: str) -> JSONResponse:
        result = await app.state.mcp.read_resource(f"ledger://applications/{application_id}")
        return _resource_response(result)

    @app.get("/api/v1/applications/{application_id}/compliance")
    async def get_application_compliance(
        application_id: str,
        as_of: str | None = Query(default=None),
    ) -> JSONResponse:
        uri = f"ledger://applications/{application_id}/compliance"
        if as_of:
            uri = f"{uri}?as_of={quote(as_of, safe='')}"
        result = await app.state.mcp.read_resource(uri)
        return _resource_response(result)

    @app.get("/api/v1/applications/{application_id}/audit-trail")
    async def get_application_audit_trail(application_id: str) -> JSONResponse:
        result = await app.state.mcp.read_resource(
            f"ledger://applications/{application_id}/audit-trail"
        )
        return _resource_response(result)

    @app.get("/api/v1/applications/{application_id}/events")
    async def get_application_events(
        application_id: str,
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        loan_stream_id = f"loan-{application_id}"
        compliance_stream_id = f"compliance-{application_id}"
        audit_stream_id = f"audit-application-{application_id}"
        async with app.state.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH scoped_streams AS (
                  SELECT DISTINCT stream_id
                  FROM events
                  WHERE payload ->> 'application_id' = $1
                )
                SELECT
                  event_id,
                  stream_id,
                  stream_position,
                  global_position,
                  event_type,
                  event_version,
                  payload,
                  metadata,
                  recorded_at
                FROM events
                WHERE
                  payload ->> 'application_id' = $1
                  OR (
                    payload ->> 'entity_type' = 'application'
                    AND payload ->> 'entity_id' = $1
                  )
                  OR stream_id IN (SELECT stream_id FROM scoped_streams)
                  OR stream_id IN ($2, $3, $4)
                ORDER BY global_position ASC
                LIMIT $5
                """,
                application_id,
                loan_stream_id,
                compliance_stream_id,
                audit_stream_id,
                limit,
            )

        events = [
            {
                "event_id": str(row["event_id"]),
                "stream_id": row["stream_id"],
                "stream_position": int(row["stream_position"]),
                "global_position": int(row["global_position"]),
                "event_type": row["event_type"],
                "event_version": int(row["event_version"]),
                "payload": dict(row["payload"]),
                "metadata": dict(row["metadata"]),
                "recorded_at": row["recorded_at"].isoformat(),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "result": {
                "application_id": application_id,
                "items": events,
                "count": len(events),
            },
        }

    @app.get("/api/v1/agents/{agent_id}/performance")
    async def get_agent_performance(agent_id: str) -> JSONResponse:
        result = await app.state.mcp.read_resource(f"ledger://agents/{agent_id}/performance")
        return _resource_response(result)

    @app.get("/api/v1/agents/{agent_id}/sessions/{session_id}")
    async def get_agent_session(agent_id: str, session_id: str) -> JSONResponse:
        result = await app.state.mcp.read_resource(
            f"ledger://agents/{agent_id}/sessions/{session_id}"
        )
        return _resource_response(result)

    @app.get("/api/v1/ledger/health")
    async def ledger_health() -> JSONResponse:
        result = await app.state.mcp.read_resource("ledger://ledger/health")
        return _resource_response(result)

    @app.get("/api/v1/ledger/delivery")
    async def ledger_delivery() -> JSONResponse:
        snapshot = await _read_outbox_delivery_snapshot(
            store=app.state.store,
            settings=app.state.settings,
        )
        return _json_response(status_code=200, content={"ok": True, "result": snapshot})

    @app.get("/api/v1/analytics/summary")
    async def analytics_summary(
        window_days: int = Query(default=30, ge=7, le=90),
    ) -> JSONResponse:
        if window_days not in {7, 30, 90}:
            return _json_response(
                status_code=422,
                content=_error_payload(
                    error_type="ValidationError",
                    message="window_days must be one of: 7, 30, 90.",
                    suggested_action="use_window_days_7_30_or_90",
                ),
            )
        snapshot = await _read_client_analytics_summary(
            store=app.state.store,
            window_days=window_days,
        )
        return _json_response(status_code=200, content={"ok": True, "result": snapshot})

    @app.get("/api/v1/metrics/summary")
    async def metrics_summary(
        window_days: int = Query(default=30, ge=7, le=90),
    ) -> JSONResponse:
        if window_days not in {7, 30, 90}:
            return _json_response(
                status_code=422,
                content=_error_payload(
                    error_type="ValidationError",
                    message="window_days must be one of: 7, 30, 90.",
                    suggested_action="use_window_days_7_30_or_90",
                ),
            )

        summary = await _read_projection_metrics_summary(
            store=app.state.store,
            window_days=window_days,
        )
        return _json_response(status_code=200, content={"ok": True, "result": summary})

    @app.get("/api/v1/metrics/daily")
    async def metrics_daily(
        window_days: int = Query(default=30, ge=7, le=90),
    ) -> JSONResponse:
        if window_days not in {7, 30, 90}:
            return _json_response(
                status_code=422,
                content=_error_payload(
                    error_type="ValidationError",
                    message="window_days must be one of: 7, 30, 90.",
                    suggested_action="use_window_days_7_30_or_90",
                ),
            )

        rows = await _read_projection_metrics_daily(
            store=app.state.store,
            window_days=window_days,
        )
        return _json_response(status_code=200, content={"ok": True, "result": rows})

    @app.get("/api/v1/metrics/agents")
    async def metrics_agents(
        window_days: int = Query(default=30, ge=7, le=90),
    ) -> JSONResponse:
        if window_days not in {7, 30, 90}:
            return _json_response(
                status_code=422,
                content=_error_payload(
                    error_type="ValidationError",
                    message="window_days must be one of: 7, 30, 90.",
                    suggested_action="use_window_days_7_30_or_90",
                ),
            )

        rows = await _read_projection_metrics_agents(
            store=app.state.store,
            window_days=window_days,
        )
        return _json_response(status_code=200, content={"ok": True, "result": rows})

    @app.post("/api/v1/projections/rebuild")
    async def rebuild_projection(
        request: Request,
        request_body: RebuildProjectionRequest,
    ) -> JSONResponse:
        principal = _principal_from_request(request)
        if not can_rebuild_projections(principal.role):
            await _write_auth_audit(
                app.state.store,
                username=principal.username,
                role=principal.role,
                action="projections_rebuild",
                success=False,
                request=request,
                details={"reason": "role_forbidden"},
            )
            return _role_forbidden_response(action="projections_rebuild", role=principal.role)

        projection_name = request_body.projection_name
        available = sorted(app.state.mcp.daemon._projections.keys())
        if projection_name and projection_name not in app.state.mcp.daemon._projections:
            return _json_response(
                status_code=404,
                content=_error_payload(
                    error_type="UnknownProjection",
                    message=f"Unknown projection '{projection_name}'.",
                    suggested_action="use_projection_name_from_list",
                    details={"available_projections": available},
                ),
            )

        if projection_name:
            await app.state.mcp.daemon.rebuild_projection(projection_name)
            rebuilt = [projection_name]
        else:
            await app.state.mcp.daemon.rebuild_all()
            rebuilt = available

        await _write_auth_audit(
            app.state.store,
            username=principal.username,
            role=principal.role,
            action="projections_rebuild",
            success=True,
            request=request,
            details={"rebuilt": rebuilt},
        )

        return _json_response(
            status_code=200,
            content={"ok": True, "result": {"rebuilt": rebuilt}},
        )

    @app.get("/api/v1/events/recent")
    async def recent_events(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        async with app.state.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  event_id,
                  stream_id,
                  stream_position,
                  global_position,
                  event_type,
                  event_version,
                  payload,
                  metadata,
                  recorded_at
                FROM events
                ORDER BY global_position DESC
                LIMIT $1
                """,
                limit,
            )

        events = [
            {
                "event_id": str(row["event_id"]),
                "stream_id": row["stream_id"],
                "stream_position": int(row["stream_position"]),
                "global_position": int(row["global_position"]),
                "event_type": row["event_type"],
                "event_version": int(row["event_version"]),
                "payload": dict(row["payload"]),
                "metadata": dict(row["metadata"]),
                "recorded_at": row["recorded_at"].isoformat(),
            }
            for row in rows
        ]
        return {"ok": True, "result": {"items": events, "count": len(events)}}

    @app.get("/api/v1/streams/{stream_id}")
    async def load_stream_events(
        stream_id: str,
        from_position: int = Query(default=1, ge=1),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        events = await app.state.store.load_stream(
            stream_id,
            from_position=from_position,
            limit=limit,
        )
        return {
            "ok": True,
            "result": {
                "stream_id": stream_id,
                "items": [event.model_dump(mode="json") for event in events],
                "count": len(events),
            },
        }

    @app.get("/api/v1/metrics")
    async def metrics() -> PlainTextResponse:
        lags = await app.state.mcp.daemon.get_all_lags()
        delivery = await _read_outbox_delivery_snapshot(
            store=app.state.store,
            settings=app.state.settings,
        )
        lines = [
            "# HELP ledger_projection_events_behind Number of events not yet projected.",
            "# TYPE ledger_projection_events_behind gauge",
            "# HELP ledger_projection_lag_ms Backlog lag in milliseconds (0 when fully synced).",
            "# TYPE ledger_projection_lag_ms gauge",
            "# HELP ledger_projection_checkpoint_age_ms "
            "Checkpoint staleness in milliseconds since last projection update.",
            "# TYPE ledger_projection_checkpoint_age_ms gauge",
            "# HELP ledger_outbox_pending Number of pending outbox messages.",
            "# TYPE ledger_outbox_pending gauge",
            "# HELP ledger_outbox_retrying Number of pending outbox messages with retries.",
            "# TYPE ledger_outbox_retrying gauge",
            "# HELP ledger_outbox_dead_letter Number of dead-lettered outbox messages.",
            "# TYPE ledger_outbox_dead_letter gauge",
            "# HELP ledger_outbox_published_total Total number of published outbox messages.",
            "# TYPE ledger_outbox_published_total gauge",
            "# HELP ledger_outbox_published_last_5m "
            "Outbox messages published in the last five minutes.",
            "# TYPE ledger_outbox_published_last_5m gauge",
            "# HELP ledger_outbox_published_last_1h Outbox messages published in the last hour.",
            "# TYPE ledger_outbox_published_last_1h gauge",
        ]
        for name in sorted(lags.keys()):
            lag = lags[name]
            lines.append(
                f'ledger_projection_events_behind{{projection="{name}"}} {lag.events_behind}'
            )
            lines.append(f'ledger_projection_lag_ms{{projection="{name}"}} {lag.lag_ms:.2f}')
            lines.append(
                f'ledger_projection_checkpoint_age_ms{{projection="{name}"}} '
                f"{lag.checkpoint_age_ms:.2f}"
            )
            lines.append(
                f'ledger_projection_checkpoint{{projection="{name}"}} {lag.checkpoint_position}'
            )
            lines.append(f'ledger_projection_latest{{projection="{name}"}} {lag.latest_position}')
        lines.append(f"ledger_outbox_pending {delivery['outbox']['pending']}")
        lines.append(f"ledger_outbox_retrying {delivery['outbox']['retrying']}")
        lines.append(f"ledger_outbox_dead_letter {delivery['outbox']['dead_letter']}")
        lines.append(f"ledger_outbox_published_total {delivery['outbox']['published_total']}")
        lines.append(
            f"ledger_outbox_published_last_5m {delivery['throughput']['published_last_5m']}"
        )
        lines.append(
            f"ledger_outbox_published_last_1h {delivery['throughput']['published_last_1h']}"
        )
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.get("/api/v1/stream/lag")
    async def stream_projection_lag(
        request: Request,
        interval_seconds: float = Query(default=2.0, ge=0.5, le=30.0),
    ) -> StreamingResponse:
        async def event_generator():
            while True:
                if await request.is_disconnected():
                    break

                lags = await app.state.mcp.daemon.get_all_lags()
                payload = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "lags": {
                        name: {
                            "events_behind": lag.events_behind,
                            "lag_ms": lag.lag_ms,
                            "checkpoint_age_ms": lag.checkpoint_age_ms,
                            "status": lag.status,
                            "checkpoint_position": lag.checkpoint_position,
                            "latest_position": lag.latest_position,
                        }
                        for name, lag in lags.items()
                    },
                }
                yield f"event: lag\ndata: {json.dumps(payload)}\n\n"
                await asyncio.sleep(interval_seconds)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return app


async def _read_outbox_delivery_snapshot(
    *,
    store: EventStore,
    settings: AppSettings,
) -> dict[str, Any]:
    async with store._pool.acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
              COUNT(*) FILTER (WHERE status = 'pending' AND attempts > 0) AS retrying_count,
              COUNT(*) FILTER (WHERE status = 'dead_letter') AS dead_letter_count,
              COUNT(*) FILTER (WHERE status = 'published') AS published_count,
              COUNT(*) FILTER (
                WHERE status = 'pending' AND next_attempt_at <= NOW()
              ) AS due_now_count,
              COALESCE(AVG(attempts) FILTER (WHERE status = 'pending'), 0) AS pending_avg_attempts,
              COUNT(*) FILTER (
                WHERE status = 'published' AND published_at >= NOW() - INTERVAL '5 minutes'
              ) AS published_last_5m,
              COUNT(*) FILTER (
                WHERE status = 'published' AND published_at >= NOW() - INTERVAL '1 hour'
              ) AS published_last_1h,
              MAX(published_at) AS last_published_at,
              MIN(created_at) FILTER (WHERE status = 'pending') AS oldest_pending_created_at
            FROM outbox
            """
        )
        topic_rows = await conn.fetch(
            """
            SELECT
              topic,
              COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
              COUNT(*) FILTER (WHERE status = 'dead_letter') AS dead_letter_count,
              COUNT(*) FILTER (
                WHERE status = 'published' AND published_at >= NOW() - INTERVAL '1 hour'
              ) AS published_last_1h
            FROM outbox
            GROUP BY topic
            ORDER BY pending_count DESC, dead_letter_count DESC, topic ASC
            LIMIT 8
            """
        )

    now = datetime.now(UTC)
    pending = int(summary["pending_count"] or 0)
    retrying = int(summary["retrying_count"] or 0)
    dead_letter = int(summary["dead_letter_count"] or 0)
    published_total = int(summary["published_count"] or 0)
    due_now = int(summary["due_now_count"] or 0)
    published_last_5m = int(summary["published_last_5m"] or 0)
    published_last_1h = int(summary["published_last_1h"] or 0)
    pending_avg_attempts = float(summary["pending_avg_attempts"] or 0.0)
    last_published_at = summary["last_published_at"]
    oldest_pending_created_at = summary["oldest_pending_created_at"]

    oldest_pending_age_ms: float | None = None
    if oldest_pending_created_at is not None:
        oldest_pending_age_ms = max(
            0.0,
            (now - oldest_pending_created_at).total_seconds() * 1000,
        )

    if dead_letter > 0:
        delivery_status = "critical"
    elif pending > 0 or retrying > 0:
        delivery_status = "warning"
    else:
        delivery_status = "healthy"

    publisher_mode = (settings.outbox_publisher or "sink").strip().lower()
    kafka_bootstrap = settings.kafka_bootstrap_servers
    if publisher_mode == "kafka":
        if kafka_bootstrap:
            transport_status = "configured"
            transport_detail = f"Kafka configured on {kafka_bootstrap}."
        else:
            transport_status = "misconfigured"
            transport_detail = "Kafka mode selected but KAFKA_BOOTSTRAP_SERVERS is missing."
    elif publisher_mode == "sink":
        transport_status = "local_validation"
        transport_detail = "Using local PostgreSQL sink publisher."
    else:
        transport_status = "custom"
        transport_detail = f"Using custom publisher mode '{publisher_mode}'."

    return {
        "status": delivery_status,
        "transport": {
            "mode": publisher_mode,
            "status": transport_status,
            "detail": transport_detail,
            "broker": kafka_bootstrap if publisher_mode == "kafka" else None,
        },
        "outbox": {
            "pending": pending,
            "retrying": retrying,
            "dead_letter": dead_letter,
            "published_total": published_total,
            "due_now": due_now,
            "pending_avg_attempts": round(pending_avg_attempts, 2),
            "oldest_pending_age_ms": oldest_pending_age_ms,
            "last_published_at": (
                last_published_at.isoformat() if last_published_at is not None else None
            ),
        },
        "throughput": {
            "published_last_5m": published_last_5m,
            "published_last_1h": published_last_1h,
        },
        "topics": [
            {
                "topic": str(row["topic"]),
                "pending": int(row["pending_count"] or 0),
                "dead_letter": int(row["dead_letter_count"] or 0),
                "published_last_1h": int(row["published_last_1h"] or 0),
            }
            for row in topic_rows
        ],
        "updated_at": now.isoformat(),
    }


async def _read_client_analytics_summary(
    *,
    store: EventStore,
    window_days: int = 30,
) -> dict[str, Any]:
    effective_window_days = window_days if window_days in {7, 30, 90} else 30

    async with store._pool.acquire() as conn:
        trend_rows = await conn.fetch(
            """
            WITH days AS (
              SELECT generate_series(
                date_trunc('day', NOW() - (($1::int - 1) * INTERVAL '1 day')),
                date_trunc('day', NOW()),
                INTERVAL '1 day'
              )::date AS day
            ),
            submitted AS (
              SELECT date_trunc('day', recorded_at)::date AS day, COUNT(*) AS total
              FROM events
              WHERE event_type = 'ApplicationSubmitted'
                AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              GROUP BY 1
            ),
            approved AS (
              SELECT date_trunc('day', recorded_at)::date AS day, COUNT(*) AS total
              FROM events
              WHERE event_type = 'ApplicationApproved'
                AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              GROUP BY 1
            ),
            declined AS (
              SELECT date_trunc('day', recorded_at)::date AS day, COUNT(*) AS total
              FROM events
              WHERE event_type = 'ApplicationDeclined'
                AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              GROUP BY 1
            )
            SELECT
              d.day,
              COALESCE(s.total, 0) AS submitted,
              COALESCE(a.total, 0) AS approved,
              COALESCE(k.total, 0) AS declined
            FROM days AS d
            LEFT JOIN submitted AS s ON s.day = d.day
            LEFT JOIN approved AS a ON a.day = d.day
            LEFT JOIN declined AS k ON k.day = d.day
            ORDER BY d.day ASC
            """,
            effective_window_days,
        )

        kpi_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE event_type = 'ApplicationSubmitted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS submitted,
              COUNT(*) FILTER (
                WHERE event_type = 'ApplicationApproved'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS approved,
              COUNT(*) FILTER (
                WHERE event_type = 'ApplicationDeclined'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS declined,
              COALESCE(SUM(
                CASE
                  WHEN event_type = 'ApplicationSubmitted'
                    AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                  THEN NULLIF(payload ->> 'requested_amount_usd', '')::double precision
                  ELSE 0
                END
              ), 0) AS requested_volume,
              COALESCE(SUM(
                CASE
                  WHEN event_type = 'ApplicationApproved'
                    AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                  THEN NULLIF(payload ->> 'approved_amount_usd', '')::double precision
                  ELSE 0
                END
              ), 0) AS approved_volume
            FROM events
            """,
            effective_window_days,
        )

        turnaround_row = await conn.fetchrow(
            """
            WITH submissions AS (
              SELECT
                payload ->> 'application_id' AS application_id,
                MIN(recorded_at) AS submitted_at
              FROM events
              WHERE event_type = 'ApplicationSubmitted'
                AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              GROUP BY 1
            ),
            final_decisions AS (
              SELECT
                payload ->> 'application_id' AS application_id,
                MIN(recorded_at) AS decided_at
              FROM events
              WHERE event_type IN ('ApplicationApproved', 'ApplicationDeclined')
              GROUP BY 1
            ),
            durations AS (
              SELECT
                EXTRACT(EPOCH FROM (f.decided_at - s.submitted_at)) / 3600.0 AS hours_to_decision
              FROM submissions AS s
              INNER JOIN final_decisions AS f
                ON f.application_id = s.application_id
              WHERE f.decided_at >= s.submitted_at
            )
            SELECT
              COUNT(*) AS decided_applications,
              AVG(hours_to_decision) AS avg_hours_to_decision,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY hours_to_decision)
                AS median_hours_to_decision
            FROM durations
            """,
            effective_window_days,
        )

        funnel_row = await conn.fetchrow(
            """
            SELECT
              COUNT(DISTINCT payload ->> 'application_id') FILTER (
                WHERE event_type = 'ApplicationSubmitted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS submitted_apps,
              COUNT(DISTINCT payload ->> 'application_id') FILTER (
                WHERE event_type = 'CreditAnalysisCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS analyzed_apps,
              COUNT(DISTINCT payload ->> 'application_id') FILTER (
                WHERE event_type = 'ComplianceCheckCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS compliance_completed_apps,
              COUNT(DISTINCT payload ->> 'application_id') FILTER (
                WHERE event_type = 'DecisionGenerated'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS decisioned_apps,
              COUNT(DISTINCT payload ->> 'application_id') FILTER (
                WHERE event_type IN ('ApplicationApproved', 'ApplicationDeclined')
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS finalized_apps
            FROM events
            WHERE payload ? 'application_id'
            """,
            effective_window_days,
        )

        compliance_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE event_type = 'ComplianceCheckCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS checks_completed,
              COUNT(*) FILTER (
                WHERE event_type = 'ComplianceCheckCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                  AND payload ->> 'overall_verdict' = 'CLEARED'
              ) AS checks_cleared,
              COUNT(*) FILTER (
                WHERE event_type = 'ComplianceRuleFailed'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS failed_rule_events
            FROM events
            """,
            effective_window_days,
        )

        top_rule_rows = await conn.fetch(
            """
            SELECT
              payload ->> 'rule_id' AS rule_id,
              payload ->> 'rule_version' AS rule_version,
              COUNT(*) AS failures
            FROM events
            WHERE event_type = 'ComplianceRuleFailed'
              AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
            GROUP BY 1, 2
            ORDER BY failures DESC, rule_id ASC
            LIMIT 5
            """,
            effective_window_days,
        )

        review_row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE event_type = 'HumanReviewCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS reviews_total,
              COUNT(*) FILTER (
                WHERE event_type = 'HumanReviewCompleted'
                  AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                  AND LOWER(COALESCE(payload ->> 'override', 'false')) = 'true'
              ) AS overrides_total
            FROM events
            """,
            effective_window_days,
        )

        agent_rows = await conn.fetch(
            """
            WITH decisions AS (
              SELECT
                payload ->> 'application_id' AS application_id,
                COALESCE(NULLIF(payload ->> 'orchestrator_agent_id', ''), 'unknown') AS agent_id,
                NULLIF(payload ->> 'confidence_score', '')::double precision AS confidence_score
              FROM events
              WHERE event_type = 'DecisionGenerated'
                AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND payload ? 'application_id'
            ),
            outcomes AS (
              SELECT
                payload ->> 'application_id' AS application_id,
                CASE
                  WHEN BOOL_OR(event_type = 'ApplicationApproved') THEN 'APPROVED'
                  WHEN BOOL_OR(event_type = 'ApplicationDeclined') THEN 'DECLINED'
                  ELSE NULL
                END AS outcome
              FROM events
              WHERE event_type IN ('ApplicationApproved', 'ApplicationDeclined')
                AND payload ? 'application_id'
              GROUP BY 1
            )
            SELECT
              d.agent_id,
              COUNT(*) AS decisions_generated,
              AVG(d.confidence_score) AS avg_confidence_score,
              COUNT(*) FILTER (WHERE o.outcome = 'APPROVED') AS approved_outcomes,
              COUNT(*) FILTER (WHERE o.outcome = 'DECLINED') AS declined_outcomes
            FROM decisions AS d
            LEFT JOIN outcomes AS o
              ON o.application_id = d.application_id
            GROUP BY d.agent_id
            ORDER BY decisions_generated DESC, d.agent_id ASC
            LIMIT 6
            """,
            effective_window_days,
        )

    submitted = int(kpi_row["submitted"] or 0)
    approved = int(kpi_row["approved"] or 0)
    declined = int(kpi_row["declined"] or 0)
    finalized = approved + declined

    approval_rate_pct: float | None = None
    if finalized > 0:
        approval_rate_pct = round((approved / finalized) * 100.0, 2)

    checks_completed = int(compliance_row["checks_completed"] or 0)
    checks_cleared = int(compliance_row["checks_cleared"] or 0)
    cleared_rate_pct: float | None = None
    if checks_completed > 0:
        cleared_rate_pct = round((checks_cleared / checks_completed) * 100.0, 2)

    reviews_total = int(review_row["reviews_total"] or 0)
    overrides_total = int(review_row["overrides_total"] or 0)
    override_rate_pct: float | None = None
    if reviews_total > 0:
        override_rate_pct = round((overrides_total / reviews_total) * 100.0, 2)

    trend_window: list[dict[str, Any]] = []
    for row in trend_rows:
        approved_on_day = int(row["approved"] or 0)
        declined_on_day = int(row["declined"] or 0)
        finalized_on_day = approved_on_day + declined_on_day
        approval_rate_on_day = (
            round((approved_on_day / finalized_on_day) * 100.0, 2)
            if finalized_on_day > 0
            else None
        )
        trend_window.append(
            {
                "day": row["day"].isoformat(),
                "submitted": int(row["submitted"] or 0),
                "approved": approved_on_day,
                "declined": declined_on_day,
                "approval_rate_pct": approval_rate_on_day,
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": effective_window_days,
        "kpis": {
            "submitted": submitted,
            "approved": approved,
            "declined": declined,
            "finalized": finalized,
            "approval_rate_pct": approval_rate_pct,
            "requested_volume_usd": round(float(kpi_row["requested_volume"] or 0.0), 2),
            "approved_volume_usd": round(float(kpi_row["approved_volume"] or 0.0), 2),
            "avg_turnaround_hours": round(
                float(turnaround_row["avg_hours_to_decision"] or 0.0), 2
            ),
            "median_turnaround_hours": round(
                float(turnaround_row["median_hours_to_decision"] or 0.0), 2
            ),
            "turnaround_sample_size": int(turnaround_row["decided_applications"] or 0),
        },
        "funnel": {
            "submitted_apps": int(funnel_row["submitted_apps"] or 0),
            "analyzed_apps": int(funnel_row["analyzed_apps"] or 0),
            "compliance_completed_apps": int(funnel_row["compliance_completed_apps"] or 0),
            "decisioned_apps": int(funnel_row["decisioned_apps"] or 0),
            "finalized_apps": int(funnel_row["finalized_apps"] or 0),
        },
        "compliance": {
            "checks_completed": checks_completed,
            "checks_cleared": checks_cleared,
            "cleared_rate_pct": cleared_rate_pct,
            "failed_rule_events": int(compliance_row["failed_rule_events"] or 0),
            "top_failed_rules": [
                {
                    "rule_id": str(row["rule_id"] or "unknown"),
                    "rule_version": str(row["rule_version"] or "unknown"),
                    "failures": int(row["failures"] or 0),
                }
                for row in top_rule_rows
            ],
        },
        "human_review": {
            "reviews_total": reviews_total,
            "overrides": overrides_total,
            "override_rate_pct": override_rate_pct,
        },
        "agent_leaderboard": [
            {
                "agent_id": str(row["agent_id"]),
                "decisions_generated": int(row["decisions_generated"] or 0),
                "avg_confidence_score": (
                    round(float(row["avg_confidence_score"]), 3)
                    if row["avg_confidence_score"] is not None
                    else None
                ),
                "approved_outcomes": int(row["approved_outcomes"] or 0),
                "declined_outcomes": int(row["declined_outcomes"] or 0),
                "approval_rate_pct": (
                    round(
                        (
                            int(row["approved_outcomes"] or 0)
                            / (
                                int(row["approved_outcomes"] or 0)
                                + int(row["declined_outcomes"] or 0)
                            )
                        )
                        * 100.0,
                        2,
                    )
                    if (int(row["approved_outcomes"] or 0) + int(row["declined_outcomes"] or 0))
                    > 0
                    else None
                ),
            }
            for row in agent_rows
        ],
        "approval_trend": trend_window,
        "approval_trend_recent_7d": trend_window[-7:],
    }


def _effective_window_days(window_days: int) -> int:
    return window_days if window_days in {7, 30, 90} else 30


async def _read_projection_metrics_summary(
    *,
    store: EventStore,
    window_days: int = 30,
) -> dict[str, Any]:
    effective_window_days = _effective_window_days(window_days)

    async with store._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE submitted_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS submitted,
              COUNT(*) FILTER (
                WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
              ) AS finalized,
              COUNT(*) FILTER (
                WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                  AND final_decision = 'APPROVE'
              ) AS approved,
              COUNT(*) FILTER (
                WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                  AND final_decision = 'DECLINE'
              ) AS declined,
              AVG(
                EXTRACT(EPOCH FROM (finalized_at - submitted_at))
              ) FILTER (
                WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                  AND submitted_at IS NOT NULL
                  AND finalized_at >= submitted_at
              ) AS avg_processing_seconds
            FROM client_analytics_projection
            """,
            effective_window_days,
        )

    submitted = int(row["submitted"] or 0)
    finalized = int(row["finalized"] or 0)
    approved = int(row["approved"] or 0)
    declined = int(row["declined"] or 0)
    approval_rate = round((approved / finalized) * 100.0, 2) if finalized > 0 else 0.0
    avg_processing_raw = row["avg_processing_seconds"]
    avg_processing_time = (
        round(float(avg_processing_raw), 2) if avg_processing_raw is not None else 0.0
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": effective_window_days,
        "submitted": submitted,
        "finalized": finalized,
        "approved": approved,
        "declined": declined,
        "approval_rate": approval_rate,
        "avg_processing_time": avg_processing_time,
    }


async def _read_projection_metrics_daily(
    *,
    store: EventStore,
    window_days: int = 30,
) -> list[dict[str, Any]]:
    effective_window_days = _effective_window_days(window_days)

    async with store._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH days AS (
              SELECT generate_series(
                date_trunc('day', NOW() - (($1::int - 1) * INTERVAL '1 day')),
                date_trunc('day', NOW()),
                INTERVAL '1 day'
              )::date AS day
            ),
            submitted AS (
              SELECT
                (submitted_at AT TIME ZONE 'UTC')::date AS day,
                COUNT(*) AS total
              FROM client_analytics_projection
              WHERE submitted_at >= NOW() - ($1::int * INTERVAL '1 day')
              GROUP BY 1
            ),
            approved AS (
              SELECT
                (finalized_at AT TIME ZONE 'UTC')::date AS day,
                COUNT(*) AS total
              FROM client_analytics_projection
              WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND final_decision = 'APPROVE'
              GROUP BY 1
            ),
            declined AS (
              SELECT
                (finalized_at AT TIME ZONE 'UTC')::date AS day,
                COUNT(*) AS total
              FROM client_analytics_projection
              WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND final_decision = 'DECLINE'
              GROUP BY 1
            ),
            processing AS (
              SELECT
                (finalized_at AT TIME ZONE 'UTC')::date AS day,
                AVG(
                  EXTRACT(EPOCH FROM (finalized_at - submitted_at))
                ) AS avg_processing_seconds
              FROM client_analytics_projection
              WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND submitted_at IS NOT NULL
                AND finalized_at >= submitted_at
              GROUP BY 1
            )
            SELECT
              d.day,
              COALESCE(s.total, 0) AS submitted,
              COALESCE(a.total, 0) AS approved,
              COALESCE(k.total, 0) AS declined,
              COALESCE(p.avg_processing_seconds, 0) AS avg_processing_seconds
            FROM days AS d
            LEFT JOIN submitted AS s ON s.day = d.day
            LEFT JOIN approved AS a ON a.day = d.day
            LEFT JOIN declined AS k ON k.day = d.day
            LEFT JOIN processing AS p ON p.day = d.day
            ORDER BY d.day ASC
            """,
            effective_window_days,
        )

    return [
        {
            "date": row["day"].isoformat(),
            "submitted": int(row["submitted"] or 0),
            "approved": int(row["approved"] or 0),
            "declined": int(row["declined"] or 0),
            "avg_processing_seconds": round(float(row["avg_processing_seconds"] or 0.0), 2),
        }
        for row in rows
    ]


async def _read_projection_metrics_agents(
    *,
    store: EventStore,
    window_days: int = 30,
) -> list[dict[str, Any]]:
    effective_window_days = _effective_window_days(window_days)

    async with store._pool.acquire() as conn:
        activity_rows = await conn.fetch(
            """
            WITH activity_events AS (
              SELECT
                COALESCE(NULLIF(payload ->> 'agent_id', ''), 'unknown') AS agent_id,
                COUNT(*)::bigint AS event_count
              FROM events
              WHERE recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND event_type IN (
                  'AgentContextLoaded',
                  'CreditAnalysisCompleted',
                  'FraudScreeningCompleted'
                )
              GROUP BY 1
              UNION ALL
              SELECT
                COALESCE(NULLIF(payload ->> 'orchestrator_agent_id', ''), 'unknown') AS agent_id,
                COUNT(*)::bigint AS event_count
              FROM events
              WHERE recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND event_type = 'DecisionGenerated'
              GROUP BY 1
              UNION ALL
              SELECT
                COALESCE(NULLIF(payload ->> 'reviewer_id', ''), 'unknown') AS agent_id,
                COUNT(*)::bigint AS event_count
              FROM events
              WHERE recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
                AND event_type = 'HumanReviewCompleted'
              GROUP BY 1
            )
            SELECT
              agent_id,
              SUM(event_count)::bigint AS activity_score
            FROM activity_events
            WHERE agent_id IS NOT NULL AND agent_id <> ''
            GROUP BY 1
            ORDER BY activity_score DESC, agent_id ASC
            """,
            effective_window_days,
        )
        decision_rows = await conn.fetch(
            """
            SELECT
              COALESCE(NULLIF(decision_agent_id, ''), 'unknown') AS agent_id,
              COUNT(*) AS decisions,
              COUNT(*) FILTER (WHERE final_decision = 'APPROVE') AS approved,
              COUNT(*) FILTER (WHERE final_decision = 'DECLINE') AS declined
            FROM client_analytics_projection
            WHERE finalized_at >= NOW() - ($1::int * INTERVAL '1 day')
              AND final_decision IN ('APPROVE', 'DECLINE')
            GROUP BY 1
            ORDER BY decisions DESC, agent_id ASC
            """,
            effective_window_days,
        )

    by_agent: dict[str, dict[str, Any]] = {}
    for row in activity_rows:
        agent_id = str(row["agent_id"])
        by_agent[agent_id] = {
            "agent_id": agent_id,
            "activity_score": int(row["activity_score"] or 0),
            "decisions": 0,
            "approval_rate": 0.0,
        }

    for row in decision_rows:
        agent_id = str(row["agent_id"])
        approved = int(row["approved"] or 0)
        declined = int(row["declined"] or 0)
        decided = approved + declined
        if agent_id not in by_agent:
            by_agent[agent_id] = {
                "agent_id": agent_id,
                "activity_score": 0,
                "decisions": 0,
                "approval_rate": 0.0,
            }
        by_agent[agent_id]["decisions"] = int(row["decisions"] or 0)
        by_agent[agent_id]["approval_rate"] = (
            round((approved / decided) * 100.0, 2) if decided > 0 else 0.0
        )

    return sorted(
        by_agent.values(),
        key=lambda item: (
            -int(item["activity_score"]),
            -int(item["decisions"]),
            str(item["agent_id"]),
        ),
    )


async def _ensure_auth_schema(store: EventStore) -> None:
    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_users (
              username TEXT PRIMARY KEY,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL,
              is_active BOOLEAN NOT NULL DEFAULT TRUE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              last_login_at TIMESTAMPTZ,
              CONSTRAINT chk_auth_role
                CHECK (role IN ('analyst', 'compliance', 'ops', 'admin'))
            );

            CREATE TABLE IF NOT EXISTS auth_audit_log (
              audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              username TEXT,
              role TEXT,
              action TEXT NOT NULL,
              success BOOLEAN NOT NULL DEFAULT FALSE,
              ip_address TEXT,
              user_agent TEXT,
              details JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_auth_audit_created
              ON auth_audit_log (created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_auth_audit_action
              ON auth_audit_log (action, created_at DESC);
            """
        )


async def _seed_demo_users(
    store: EventStore,
    users: list[tuple[str, str, str]],
) -> None:
    async with store._pool.acquire() as conn:
        for username, password, role in users:
            await conn.execute(
                """
                INSERT INTO auth_users (
                  username,
                  password_hash,
                  role,
                  is_active,
                  created_at,
                  updated_at
                )
                VALUES ($1, $2, $3, TRUE, NOW(), NOW())
                ON CONFLICT (username)
                DO UPDATE SET
                  password_hash = EXCLUDED.password_hash,
                  role = EXCLUDED.role,
                  is_active = TRUE,
                  updated_at = NOW()
                """,
                username,
                create_password_hash(password),
                role,
            )

        allowed_usernames = [username for username, _, _ in users]
        await conn.execute(
            """
            UPDATE auth_users
            SET is_active = FALSE, updated_at = NOW()
            WHERE username <> ALL($1::text[]) AND is_active = TRUE
            """,
            allowed_usernames,
        )


async def _write_auth_audit(
    store: EventStore,
    *,
    username: str | None,
    role: str | None,
    action: str,
    success: bool,
    request: Request,
    details: dict[str, Any],
) -> None:
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    async with store._pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO auth_audit_log (
              username,
              role,
              action,
              success,
              ip_address,
              user_agent,
              details,
              created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW())
            """,
            username,
            role,
            action,
            success,
            ip_address,
            user_agent,
            details,
        )


def _principal_from_request(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, AuthPrincipal):
        raise RuntimeError("Principal missing on authenticated request.")
    return principal


def _allowed_commands_for_role(role: str) -> list[str]:
    allowed = [name for name, roles in COMMAND_ROLE_POLICY.items() if role in roles]
    return sorted(allowed)


def _role_forbidden_response(action: str, role: str) -> JSONResponse:
    return _json_response(
        status_code=403,
        content=_error_payload(
            error_type="AuthorizationError",
            message=f"Role '{role}' cannot perform '{action}'.",
            suggested_action="use_account_with_required_role",
        ),
    )


def _json_response(*, status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


def _resource_response(result: dict[str, Any]) -> JSONResponse:
    if result.get("ok"):
        return _json_response(status_code=200, content=result)

    error = result.get("error", {})
    status_code = _error_status_code(str(error.get("error_type", "InternalError")))
    return _json_response(status_code=status_code, content={"ok": False, "error": error})


def _error_status_code(error_type: str) -> int:
    if error_type in {"ValidationError"}:
        return 422
    if error_type in {"AuthenticationRequired", "AuthenticationFailed"}:
        return 401
    if error_type in {"AuthorizationError"}:
        return 403
    if error_type in {"RateLimitExceeded"}:
        return 429
    if error_type in {
        "PreconditionFailed",
        "DomainError",
        "OptimisticConcurrencyError",
    }:
        return 409
    if error_type in {"NotFound", "UnknownResource", "UnknownTool", "UnknownProjection"}:
        return 404
    return 500


def _error_payload(
    *,
    error_type: str,
    message: str,
    suggested_action: str,
    details: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "error_type": error_type,
            "message": message,
            "suggested_action": suggested_action,
        },
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload
