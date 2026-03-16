from __future__ import annotations

from typing import Any

import asyncpg

from src.models.events import StoredEvent


class ApplicationSummaryProjection:
    name = "application_summary"

    async def ensure_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_summary_projection (
              application_id TEXT PRIMARY KEY,
              current_state TEXT NOT NULL,
              decision_recommendation TEXT,
              final_decision TEXT,
              requested_amount_usd DOUBLE PRECISION,
              approved_amount_usd DOUBLE PRECISION,
              assessed_max_limit_usd DOUBLE PRECISION,
              compliance_status TEXT,
              last_event_type TEXT NOT NULL,
              last_global_position BIGINT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_application_summary_state
              ON application_summary_projection (current_state);
            """
        )

    async def reset(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE application_summary_projection")

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        payload = event.payload
        application_id = payload.get("application_id")
        if not application_id:
            return

        state_patch = self._state_patch(event)
        if state_patch is None:
            return

        row = await conn.fetchrow(
            """
            SELECT
              current_state,
              decision_recommendation,
              final_decision,
              requested_amount_usd,
              approved_amount_usd,
              assessed_max_limit_usd,
              compliance_status,
              last_global_position
            FROM application_summary_projection
            WHERE application_id = $1
            """,
            application_id,
        )
        current = dict(row) if row else {}

        merged = _merge_state(current=current, patch=state_patch)
        await conn.execute(
            """
            INSERT INTO application_summary_projection (
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
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (application_id)
            DO UPDATE SET
              current_state = EXCLUDED.current_state,
              decision_recommendation = EXCLUDED.decision_recommendation,
              final_decision = EXCLUDED.final_decision,
              requested_amount_usd = EXCLUDED.requested_amount_usd,
              approved_amount_usd = EXCLUDED.approved_amount_usd,
              assessed_max_limit_usd = EXCLUDED.assessed_max_limit_usd,
              compliance_status = EXCLUDED.compliance_status,
              last_event_type = EXCLUDED.last_event_type,
              last_global_position = EXCLUDED.last_global_position,
              updated_at = NOW()
            """,
            application_id,
            merged["current_state"],
            merged.get("decision_recommendation"),
            merged.get("final_decision"),
            merged.get("requested_amount_usd"),
            merged.get("approved_amount_usd"),
            merged.get("assessed_max_limit_usd"),
            merged.get("compliance_status"),
            event.event_type,
            event.global_position,
        )

    def _state_patch(self, event: StoredEvent) -> dict[str, Any] | None:
        payload = event.payload
        event_type = event.event_type

        if event_type == "ApplicationSubmitted":
            return {
                "current_state": "SUBMITTED",
                "requested_amount_usd": payload.get("requested_amount_usd"),
            }
        if event_type == "CreditAnalysisRequested":
            return {"current_state": "AWAITING_ANALYSIS"}
        if event_type == "DecisionGenerated":
            recommendation = str(payload.get("recommendation", "")).upper()
            current_state = {
                "APPROVE": "APPROVED_PENDING_HUMAN",
                "DECLINE": "DECLINED_PENDING_HUMAN",
            }.get(recommendation, "PENDING_DECISION")
            return {
                "current_state": current_state,
                "decision_recommendation": recommendation,
                "compliance_status": payload.get("compliance_status"),
                "assessed_max_limit_usd": payload.get("assessed_max_limit_usd"),
            }
        if event_type == "HumanReviewCompleted":
            return {
                "final_decision": str(payload.get("final_decision", "")).upper() or None,
            }
        if event_type == "ApplicationApproved":
            return {
                "current_state": "FINAL_APPROVED",
                "final_decision": "APPROVE",
                "approved_amount_usd": payload.get("approved_amount_usd"),
            }
        if event_type == "ApplicationDeclined":
            return {
                "current_state": "FINAL_DECLINED",
                "final_decision": "DECLINE",
            }
        if event_type == "ComplianceCheckRequested":
            return {"compliance_status": "PENDING"}
        if event_type == "ComplianceRuleFailed":
            return {"compliance_status": "FAILED"}
        if event_type == "ComplianceRulePassed":
            return {"compliance_status": "PENDING"}
        return None


def _merge_state(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(current)
    merged.update({k: v for k, v in patch.items() if v is not None})
    if "current_state" not in merged:
        merged["current_state"] = "SUBMITTED"
    return merged

