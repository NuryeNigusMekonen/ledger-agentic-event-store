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
        pytest.skip("DATABASE_URL is not set. Skipping API integration test.")
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
                  agent_performance_projection
                RESTART IDENTITY CASCADE
                """
            )
        await store.close()

    asyncio.run(_run())



def test_api_bootstrap_and_views() -> None:
    database_url = _database_url()
    _reset_database(database_url)

    app = create_app(
        AppSettings(
            database_url=database_url,
            api_host="127.0.0.1",
            api_port=8000,
            cors_origins=["http://localhost:5173"],
            api_key=None,
            apply_schema_on_start=True,
            command_timeout_seconds=15.0,
        )
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "analyst", "password": "analyst123!"},
        )
        assert login.status_code == 200
        token = login.json()["result"]["access_token"]
        headers = {"authorization": f"Bearer {token}"}

        bootstrap = client.post("/api/v1/bootstrap/demo", json={}, headers=headers)
        assert bootstrap.status_code == 200
        bootstrap_json = bootstrap.json()
        assert bootstrap_json["ok"] is True

        application_id = bootstrap_json["result"]["application_id"]

        app_summary = client.get(f"/api/v1/applications/{application_id}", headers=headers)
        assert app_summary.status_code == 200
        assert app_summary.json()["result"]["current_state"] == "FINAL_APPROVED"

        compliance = client.get(
            f"/api/v1/applications/{application_id}/compliance",
            headers=headers,
        )
        assert compliance.status_code == 200
        assert compliance.json()["result"]["snapshot"]["compliance_status"] == "CLEARED"

        events = client.get("/api/v1/events/recent?limit=10", headers=headers)
        assert events.status_code == 200
        assert events.json()["result"]["count"] > 0

        metrics = client.get("/api/v1/metrics", headers=headers)
        assert metrics.status_code == 200
        assert "ledger_projection_events_behind" in metrics.text
