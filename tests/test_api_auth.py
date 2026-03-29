from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values, load_dotenv

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.settings import AppSettings
from src.event_store import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")



def _database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        value = dotenv_values(PROJECT_ROOT / ".env.example").get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not set. Skipping API auth integration test.")
    return value



def _reset_database(database_url: str) -> None:
    async def _run() -> None:
        store = await EventStore.from_dsn(database_url, min_size=1, max_size=4, connect_timeout=2.0)
        await store.apply_schema(PROJECT_ROOT / "src" / "schema.sql")
        async with store._pool.acquire() as conn:
            await conn.execute(
                """
                TRUNCATE TABLE
                  outbox,
                  events,
                  event_streams,
                  projection_checkpoints,
                  auth_audit_log,
                  auth_users,
                  application_summary_projection,
                  compliance_audit_state_projection,
                  compliance_audit_view_projection,
                  agent_performance_projection,
                  client_analytics_projection
                RESTART IDENTITY CASCADE
                """
            )
        await store.close()

    asyncio.run(_run())



def _build_app(database_url: str):
    return create_app(
        AppSettings(
            database_url=database_url,
            api_host="127.0.0.1",
            api_port=8000,
            cors_origins=["http://localhost:5173"],
            api_key=None,
            apply_schema_on_start=True,
            command_timeout_seconds=15.0,
            jwt_secret="test-secret",
            jwt_issuer="ledger-api-test",
            jwt_ttl_minutes=30,
            seed_demo_users=True,
        )
    )



def test_auth_login_and_rbac_permissions() -> None:
    database_url = _database_url()
    _reset_database(database_url)
    app = _build_app(database_url)

    with TestClient(app) as client:
        without_token = client.get("/api/v1/tools")
        assert without_token.status_code == 401
        assert without_token.json()["error"]["error_type"] == "AuthenticationRequired"

        analyst_login = client.post(
            "/api/v1/auth/login",
            json={"username": "analyst", "password": "analyst123!"},
        )
        assert analyst_login.status_code == 200
        analyst_token = analyst_login.json()["result"]["access_token"]
        analyst_headers = {"authorization": f"Bearer {analyst_token}"}

        forbidden = client.post(
            "/api/v1/commands/run_integrity_check",
            headers=analyst_headers,
            json={
                "arguments": {
                    "entity_type": "application",
                    "entity_id": "app-nonexistent",
                    "role": "compliance",
                }
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["error_type"] == "AuthorizationError"

        compliance_login = client.post(
            "/api/v1/auth/login",
            json={"username": "compliance", "password": "compliance123!"},
        )
        assert compliance_login.status_code == 200
        compliance_token = compliance_login.json()["result"]["access_token"]
        compliance_headers = {"authorization": f"Bearer {compliance_token}"}

        audit = client.get("/api/v1/auth/audit", headers=compliance_headers)
        assert audit.status_code == 200
        assert audit.json()["result"]["count"] >= 2
