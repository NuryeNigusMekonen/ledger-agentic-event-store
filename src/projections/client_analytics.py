from __future__ import annotations

from datetime import datetime

import asyncpg

from src.models.events import StoredEvent


class ClientAnalyticsProjection:
    name = "client_analytics"

    _subscribed_event_types = {
        "ApplicationSubmitted",
        "DecisionGenerated",
        "ApplicationApproved",
        "ApplicationDeclined",
    }

    async def ensure_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS client_analytics_projection (
              application_id TEXT PRIMARY KEY,
              submitted_at TIMESTAMPTZ,
              finalized_at TIMESTAMPTZ,
              final_decision TEXT,
              requested_amount_usd DOUBLE PRECISION,
              approved_amount_usd DOUBLE PRECISION,
              decision_agent_id TEXT,
              decision_generated_at TIMESTAMPTZ,
              processing_time_hours DOUBLE PRECISION,
              last_global_position BIGINT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_client_analytics_submitted_at
              ON client_analytics_projection (submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_client_analytics_finalized_at
              ON client_analytics_projection (finalized_at DESC);
            CREATE INDEX IF NOT EXISTS idx_client_analytics_agent
              ON client_analytics_projection (decision_agent_id, finalized_at DESC);
            """
        )

    async def reset(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE client_analytics_projection")

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        if event.event_type not in self._subscribed_event_types:
            return

        payload = event.payload
        application_id = payload.get("application_id")
        if not isinstance(application_id, str) or not application_id:
            return

        existing_row = await conn.fetchrow(
            """
            SELECT
              submitted_at,
              finalized_at,
              final_decision,
              requested_amount_usd,
              approved_amount_usd,
              decision_agent_id,
              decision_generated_at,
              processing_time_hours
            FROM client_analytics_projection
            WHERE application_id = $1
            """,
            application_id,
        )

        submitted_at = _read_datetime(existing_row, "submitted_at")
        finalized_at = _read_datetime(existing_row, "finalized_at")
        final_decision = _read_string(existing_row, "final_decision")
        requested_amount_usd = _read_float(existing_row, "requested_amount_usd")
        approved_amount_usd = _read_float(existing_row, "approved_amount_usd")
        decision_agent_id = _read_string(existing_row, "decision_agent_id")
        decision_generated_at = _read_datetime(existing_row, "decision_generated_at")

        if event.event_type == "ApplicationSubmitted":
            submitted_at = _earliest_timestamp(submitted_at, event.recorded_at)
            requested_amount_usd = _coalesce_float(
                _payload_float(payload.get("requested_amount_usd")),
                requested_amount_usd,
            )

        elif event.event_type == "DecisionGenerated":
            candidate_agent = payload.get("orchestrator_agent_id")
            if isinstance(candidate_agent, str) and candidate_agent:
                decision_agent_id = candidate_agent
            decision_generated_at = _earliest_timestamp(decision_generated_at, event.recorded_at)

        elif event.event_type == "ApplicationApproved":
            finalized_at = _earliest_timestamp(finalized_at, event.recorded_at)
            final_decision = final_decision or "APPROVE"
            approved_amount_usd = _coalesce_float(
                _payload_float(payload.get("approved_amount_usd")),
                approved_amount_usd,
            )

        elif event.event_type == "ApplicationDeclined":
            finalized_at = _earliest_timestamp(finalized_at, event.recorded_at)
            final_decision = final_decision or "DECLINE"

        processing_time_hours = _processing_hours(submitted_at, finalized_at)

        await conn.execute(
            """
            INSERT INTO client_analytics_projection (
              application_id,
              submitted_at,
              finalized_at,
              final_decision,
              requested_amount_usd,
              approved_amount_usd,
              decision_agent_id,
              decision_generated_at,
              processing_time_hours,
              last_global_position,
              updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (application_id)
            DO UPDATE SET
              submitted_at = EXCLUDED.submitted_at,
              finalized_at = EXCLUDED.finalized_at,
              final_decision = EXCLUDED.final_decision,
              requested_amount_usd = EXCLUDED.requested_amount_usd,
              approved_amount_usd = EXCLUDED.approved_amount_usd,
              decision_agent_id = EXCLUDED.decision_agent_id,
              decision_generated_at = EXCLUDED.decision_generated_at,
              processing_time_hours = EXCLUDED.processing_time_hours,
              last_global_position = GREATEST(
                client_analytics_projection.last_global_position,
                EXCLUDED.last_global_position
              ),
              updated_at = NOW()
            """,
            application_id,
            submitted_at,
            finalized_at,
            final_decision,
            requested_amount_usd,
            approved_amount_usd,
            decision_agent_id,
            decision_generated_at,
            processing_time_hours,
            event.global_position,
        )


def _read_datetime(row: asyncpg.Record | None, key: str) -> datetime | None:
    if row is None:
        return None
    value = row.get(key)
    if isinstance(value, datetime):
        return value
    return None


def _read_string(row: asyncpg.Record | None, key: str) -> str | None:
    if row is None:
        return None
    value = row.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _read_float(row: asyncpg.Record | None, key: str) -> float | None:
    if row is None:
        return None
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _payload_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coalesce_float(primary: float | None, fallback: float | None) -> float | None:
    return primary if primary is not None else fallback


def _earliest_timestamp(current: datetime | None, candidate: datetime) -> datetime:
    if current is None:
        return candidate
    return candidate if candidate < current else current


def _processing_hours(submitted_at: datetime | None, finalized_at: datetime | None) -> float | None:
    if submitted_at is None or finalized_at is None:
        return None
    seconds = (finalized_at - submitted_at).total_seconds()
    if seconds < 0:
        return None
    return round(seconds / 3600.0, 4)
