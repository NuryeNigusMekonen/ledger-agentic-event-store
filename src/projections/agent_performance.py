from __future__ import annotations

import asyncpg

from src.models.events import StoredEvent


class AgentPerformanceLedgerProjection:
    name = "agent_performance_ledger"

    async def ensure_schema(self, conn: asyncpg.Connection) -> None:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_performance_projection (
              agent_id TEXT NOT NULL,
              model_version TEXT NOT NULL,
              sessions_started INTEGER NOT NULL DEFAULT 0,
              analyses_completed INTEGER NOT NULL DEFAULT 0,
              fraud_screenings_completed INTEGER NOT NULL DEFAULT 0,
              decisions_recorded INTEGER NOT NULL DEFAULT 0,
              total_confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0,
              confidence_samples INTEGER NOT NULL DEFAULT 0,
              avg_confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0,
              last_global_position BIGINT NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (agent_id, model_version)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_performance_updated
              ON agent_performance_projection (updated_at DESC);
            """
        )

    async def reset(self, conn: asyncpg.Connection) -> None:
        await conn.execute("TRUNCATE TABLE agent_performance_projection")

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        if event.event_type == "AgentContextLoaded":
            await self._increment(
                conn=conn,
                agent_id=event.payload.get("agent_id"),
                model_version=event.payload.get("model_version"),
                sessions_started=1,
                last_global_position=event.global_position,
            )
            return

        if event.event_type == "CreditAnalysisCompleted":
            await self._increment(
                conn=conn,
                agent_id=event.payload.get("agent_id"),
                model_version=event.payload.get("model_version"),
                analyses_completed=1,
                confidence_delta=float(event.payload.get("confidence_score", 0.0)),
                confidence_samples_delta=1,
                last_global_position=event.global_position,
            )
            return

        if event.event_type == "FraudScreeningCompleted":
            await self._increment(
                conn=conn,
                agent_id=event.payload.get("agent_id"),
                model_version=event.payload.get("screening_model_version"),
                fraud_screenings_completed=1,
                last_global_position=event.global_position,
            )
            return

        if event.event_type == "DecisionGenerated":
            orchestrator = event.payload.get("orchestrator_agent_id")
            versions = event.payload.get("model_versions", {})
            if isinstance(versions, dict):
                version = versions.get(orchestrator, "unknown")
            else:
                version = "unknown"
            await self._increment(
                conn=conn,
                agent_id=orchestrator,
                model_version=version,
                decisions_recorded=1,
                confidence_delta=float(event.payload.get("confidence_score", 0.0)),
                confidence_samples_delta=1,
                last_global_position=event.global_position,
            )

    async def _increment(
        self,
        conn: asyncpg.Connection,
        agent_id: str | None,
        model_version: str | None,
        sessions_started: int = 0,
        analyses_completed: int = 0,
        fraud_screenings_completed: int = 0,
        decisions_recorded: int = 0,
        confidence_delta: float = 0.0,
        confidence_samples_delta: int = 0,
        last_global_position: int = 0,
    ) -> None:
        if not agent_id or not model_version:
            return

        await conn.execute(
            """
            INSERT INTO agent_performance_projection (
              agent_id,
              model_version,
              sessions_started,
              analyses_completed,
              fraud_screenings_completed,
              decisions_recorded,
              total_confidence_score,
              confidence_samples,
              avg_confidence_score,
              last_global_position,
              updated_at
            )
            VALUES (
              $1, $2, $3::integer, $4::integer, $5::integer, $6::integer,
              $7::double precision,
              $8::integer,
              CASE
                WHEN $8::integer > 0
                THEN $7::double precision / $8::double precision
                ELSE 0
              END,
              $9, NOW()
            )
            ON CONFLICT (agent_id, model_version)
            DO UPDATE SET
              sessions_started =
                agent_performance_projection.sessions_started
                + EXCLUDED.sessions_started,
              analyses_completed =
                agent_performance_projection.analyses_completed
                + EXCLUDED.analyses_completed,
              fraud_screenings_completed =
                agent_performance_projection.fraud_screenings_completed
                + EXCLUDED.fraud_screenings_completed,
              decisions_recorded =
                agent_performance_projection.decisions_recorded
                + EXCLUDED.decisions_recorded,
              total_confidence_score =
                agent_performance_projection.total_confidence_score
                + EXCLUDED.total_confidence_score,
              confidence_samples =
                agent_performance_projection.confidence_samples + EXCLUDED.confidence_samples,
              avg_confidence_score =
                CASE
                  WHEN (
                    agent_performance_projection.confidence_samples
                    + EXCLUDED.confidence_samples
                  ) > 0
                  THEN
                    (
                      agent_performance_projection.total_confidence_score
                      + EXCLUDED.total_confidence_score
                    )
                    / (
                      agent_performance_projection.confidence_samples
                      + EXCLUDED.confidence_samples
                    )
                  ELSE 0
                END,
              last_global_position = GREATEST(
                agent_performance_projection.last_global_position,
                EXCLUDED.last_global_position
              ),
              updated_at = NOW()
            """,
            agent_id,
            model_version,
            sessions_started,
            analyses_completed,
            fraud_screenings_completed,
            decisions_recorded,
            confidence_delta,
            confidence_samples_delta,
            last_global_position,
        )
