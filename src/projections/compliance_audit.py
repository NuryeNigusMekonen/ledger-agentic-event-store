from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from src.models.events import StoredEvent


class ComplianceAuditViewProjection:
    name = "compliance_audit_view"

    async def ensure_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS compliance_audit_state_projection (
              application_id TEXT PRIMARY KEY,
              regulation_set_version TEXT,
              mandatory_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
              passed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
              failed_checks JSONB NOT NULL DEFAULT '{}'::jsonb,
              compliance_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              last_global_position BIGINT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS compliance_audit_view_projection (
              application_id TEXT NOT NULL,
              global_position BIGINT NOT NULL,
              recorded_at TIMESTAMPTZ NOT NULL,
              event_type TEXT NOT NULL,
              compliance_status TEXT NOT NULL,
              regulation_set_version TEXT,
              rule_id TEXT,
              rule_version TEXT,
              failure_reason TEXT,
              payload JSONB NOT NULL,
              metadata JSONB NOT NULL,
              PRIMARY KEY (application_id, global_position)
            );

            CREATE INDEX IF NOT EXISTS idx_compliance_view_recorded
              ON compliance_audit_view_projection (application_id, recorded_at DESC);
            """
        )

    async def reset(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE compliance_audit_view_projection")
        await conn.execute("TRUNCATE TABLE compliance_audit_state_projection")

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        if event.event_type not in {
            "ComplianceCheckRequested",
            "ComplianceRulePassed",
            "ComplianceRuleFailed",
        }:
            return

        payload = event.payload
        application_id = payload.get("application_id")
        if not application_id:
            return

        state = await self._load_state(conn, application_id)
        updated_state = _next_state(state, event)

        await conn.execute(
            """
            INSERT INTO compliance_audit_state_projection (
              application_id,
              regulation_set_version,
              mandatory_checks,
              passed_checks,
              failed_checks,
              compliance_status,
              last_global_position,
              updated_at
            )
            VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6, $7, NOW())
            ON CONFLICT (application_id)
            DO UPDATE SET
              regulation_set_version = EXCLUDED.regulation_set_version,
              mandatory_checks = EXCLUDED.mandatory_checks,
              passed_checks = EXCLUDED.passed_checks,
              failed_checks = EXCLUDED.failed_checks,
              compliance_status = EXCLUDED.compliance_status,
              last_global_position = EXCLUDED.last_global_position,
              updated_at = NOW()
            """,
            application_id,
            updated_state["regulation_set_version"],
            sorted(updated_state["mandatory_checks"]),
            sorted(updated_state["passed_checks"]),
            updated_state["failed_checks"],
            updated_state["compliance_status"],
            event.global_position,
        )

        await conn.execute(
            """
            INSERT INTO compliance_audit_view_projection (
              application_id,
              global_position,
              recorded_at,
              event_type,
              compliance_status,
              regulation_set_version,
              rule_id,
              rule_version,
              failure_reason,
              payload,
              metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
            ON CONFLICT (application_id, global_position) DO NOTHING
            """,
            application_id,
            event.global_position,
            event.recorded_at,
            event.event_type,
            updated_state["compliance_status"],
            updated_state["regulation_set_version"],
            payload.get("rule_id"),
            payload.get("rule_version"),
            payload.get("failure_reason"),
            event.payload,
            event.metadata,
        )

    async def get_compliance_at(
        self,
        conn: asyncpg.Connection,
        application_id: str,
        as_of: datetime,
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            """
            SELECT
              application_id,
              global_position,
              recorded_at,
              event_type,
              compliance_status,
              regulation_set_version,
              rule_id,
              rule_version,
              failure_reason,
              payload,
              metadata
            FROM compliance_audit_view_projection
            WHERE application_id = $1 AND recorded_at <= $2
            ORDER BY recorded_at DESC, global_position DESC
            LIMIT 1
            """,
            application_id,
            as_of,
        )
        if row is None:
            return None
        return dict(row)

    async def get_current(
        self,
        conn: asyncpg.Connection,
        application_id: str,
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            """
            SELECT
              application_id,
              regulation_set_version,
              mandatory_checks,
              passed_checks,
              failed_checks,
              compliance_status,
              last_global_position,
              updated_at
            FROM compliance_audit_state_projection
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return None
        return dict(row)

    async def _load_state(self, conn: asyncpg.Connection, application_id: str) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT
              regulation_set_version,
              mandatory_checks,
              passed_checks,
              failed_checks,
              compliance_status
            FROM compliance_audit_state_projection
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return {
                "regulation_set_version": None,
                "mandatory_checks": set(),
                "passed_checks": set(),
                "failed_checks": {},
                "compliance_status": "NOT_STARTED",
            }
        return {
            "regulation_set_version": row["regulation_set_version"],
            "mandatory_checks": set(row["mandatory_checks"] or []),
            "passed_checks": set(row["passed_checks"] or []),
            "failed_checks": dict(row["failed_checks"] or {}),
            "compliance_status": row["compliance_status"],
        }


def _next_state(state: dict[str, Any], event: StoredEvent) -> dict[str, Any]:
    payload = event.payload
    event_type = event.event_type
    regulation_set_version = state["regulation_set_version"]
    mandatory_checks = set(state["mandatory_checks"])
    passed_checks = set(state["passed_checks"])
    failed_checks = dict(state["failed_checks"])

    if event_type == "ComplianceCheckRequested":
        mandatory_checks = set(str(v) for v in payload.get("checks_required", []))
        passed_checks = set()
        failed_checks = {}
        regulation_set_version = payload.get("regulation_set_version")
    elif event_type == "ComplianceRulePassed":
        rule_id = str(payload.get("rule_id", ""))
        if rule_id:
            passed_checks.add(rule_id)
            failed_checks.pop(rule_id, None)
    elif event_type == "ComplianceRuleFailed":
        rule_id = str(payload.get("rule_id", ""))
        if rule_id:
            failed_checks[rule_id] = str(payload.get("failure_reason", ""))
            passed_checks.discard(rule_id)

    compliance_status = _compute_compliance_status(
        mandatory_checks=mandatory_checks,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
    )
    return {
        "regulation_set_version": regulation_set_version,
        "mandatory_checks": mandatory_checks,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "compliance_status": compliance_status,
    }


def _compute_compliance_status(
    mandatory_checks: set[str],
    passed_checks: set[str],
    failed_checks: dict[str, str],
) -> str:
    if failed_checks:
        return "FAILED"
    if mandatory_checks and mandatory_checks <= passed_checks:
        return "CLEARED"
    if mandatory_checks:
        return "PENDING"
    return "NOT_STARTED"
