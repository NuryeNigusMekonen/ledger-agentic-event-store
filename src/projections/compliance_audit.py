from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from src.event_store import EventStore
from src.models.events import StoredEvent


class ComplianceAuditViewProjection:
    name = "compliance_audit_view"
    state_table = "compliance_audit_state_projection"
    view_table = "compliance_audit_view_projection"
    state_shadow_table = "compliance_audit_state_projection_rebuild"
    view_shadow_table = "compliance_audit_view_projection_rebuild"
    subscribed_event_types = {
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceCheckCompleted",
    }

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
        await conn.execute(f"TRUNCATE TABLE {self.view_table}")
        await conn.execute(f"TRUNCATE TABLE {self.state_table}")

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        await self._apply_to_tables(
            conn=conn,
            event=event,
            state_table=self.state_table,
            view_table=self.view_table,
        )

    async def rebuild_from_scratch(
        self,
        store: EventStore,
        batch_size: int = 500,
    ) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0.")

        async with store._pool.acquire() as conn:
            await self.ensure_schema(conn)
            await self._ensure_shadow_schema(conn)
            await conn.execute(f"TRUNCATE TABLE {self.view_shadow_table}")
            await conn.execute(f"TRUNCATE TABLE {self.state_shadow_table}")
            replay_until = int(
                await conn.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events")
                or 0
            )

        processed = 0
        stop_replay = False
        async for batch in store.load_all(
            from_global_position=0,
            batch_size=batch_size,
            event_types=self.subscribed_event_types,
        ):
            if stop_replay:
                break
            async with store._pool.acquire() as conn:
                async with conn.transaction():
                    for event in batch:
                        if event.global_position > replay_until:
                            stop_replay = True
                            break
                        await self._apply_to_tables(
                            conn=conn,
                            event=event,
                            state_table=self.state_shadow_table,
                            view_table=self.view_shadow_table,
                        )
                        processed += 1

        async with store._pool.acquire() as conn:
            async with conn.transaction():
                old_state_table = f"{self.state_table}_old"
                old_view_table = f"{self.view_table}_old"
                await conn.execute(f"DROP TABLE IF EXISTS {old_view_table}")
                await conn.execute(f"DROP TABLE IF EXISTS {old_state_table}")
                await conn.execute(f"ALTER TABLE {self.view_table} RENAME TO {old_view_table}")
                await conn.execute(f"ALTER TABLE {self.state_table} RENAME TO {old_state_table}")
                await conn.execute(f"ALTER TABLE {self.view_shadow_table} RENAME TO {self.view_table}")
                await conn.execute(f"ALTER TABLE {self.state_shadow_table} RENAME TO {self.state_table}")
                await conn.execute(f"DROP TABLE IF EXISTS {old_view_table}")
                await conn.execute(f"DROP TABLE IF EXISTS {old_state_table}")
                await conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_compliance_view_recorded
                      ON compliance_audit_view_projection (application_id, recorded_at DESC)
                    """
                )
                await conn.execute(
                    """
                    INSERT INTO projection_checkpoints (
                      projection_name,
                      last_global_position,
                      last_event_at,
                      updated_at,
                      metadata
                    )
                    VALUES (
                      $1,
                      $2,
                      NOW(),
                      NOW(),
                      jsonb_build_object(
                        'snapshot_strategy', 'state+timeline',
                        'last_rebuild_at', NOW()::text
                      )
                    )
                    ON CONFLICT (projection_name)
                    DO UPDATE SET
                      last_global_position = EXCLUDED.last_global_position,
                      last_event_at = EXCLUDED.last_event_at,
                      updated_at = NOW(),
                      metadata = projection_checkpoints.metadata
                        || jsonb_build_object(
                          'snapshot_strategy', 'state+timeline',
                          'last_rebuild_at', NOW()::text
                        )
                    """,
                    self.name,
                    replay_until,
                )
        return processed

    async def _apply_to_tables(
        self,
        conn: asyncpg.Connection,
        event: StoredEvent,
        state_table: str,
        view_table: str,
    ) -> None:
        if event.event_type not in self.subscribed_event_types:
            return

        payload = event.payload
        application_id = payload.get("application_id")
        if not application_id:
            return

        state = await self._load_state(conn, application_id, state_table=state_table)
        updated_state = _next_state(state, event)

        await conn.execute(
            f"""
            INSERT INTO {state_table} (
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
            f"""
            INSERT INTO {view_table} (
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

    async def _ensure_shadow_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.state_shadow_table} (
              application_id TEXT PRIMARY KEY,
              regulation_set_version TEXT,
              mandatory_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
              passed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
              failed_checks JSONB NOT NULL DEFAULT '{{}}'::jsonb,
              compliance_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
              last_global_position BIGINT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS {self.view_shadow_table} (
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
            """
        )

    async def get_compliance_at(
        self,
        conn: asyncpg.Connection,
        application_id: str,
        as_of: datetime,
    ) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"""
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
            FROM {self.view_table}
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
            f"""
            SELECT
              application_id,
              regulation_set_version,
              mandatory_checks,
              passed_checks,
              failed_checks,
              compliance_status,
              last_global_position,
              updated_at
            FROM {self.state_table}
            WHERE application_id = $1
            """,
            application_id,
        )
        if row is None:
            return None
        return dict(row)

    async def _load_state(
        self,
        conn: asyncpg.Connection,
        application_id: str,
        state_table: str,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            f"""
            SELECT
              regulation_set_version,
              mandatory_checks,
              passed_checks,
              failed_checks,
              compliance_status
            FROM {state_table}
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
    elif event_type == "ComplianceCheckCompleted":
        verdict = str(payload.get("overall_verdict", "")).upper()
        if verdict in {"CLEARED", "FAILED", "PENDING", "NOT_STARTED"}:
            return {
                "regulation_set_version": regulation_set_version,
                "mandatory_checks": mandatory_checks,
                "passed_checks": passed_checks,
                "failed_checks": failed_checks,
                "compliance_status": verdict,
            }

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
