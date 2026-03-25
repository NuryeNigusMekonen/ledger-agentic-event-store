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
              human_reviews_completed INTEGER NOT NULL DEFAULT 0,
              overrides_recorded INTEGER NOT NULL DEFAULT 0,
              override_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
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
        await conn.execute(
            """
            ALTER TABLE agent_performance_projection
              ADD COLUMN IF NOT EXISTS human_reviews_completed INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE agent_performance_projection
              ADD COLUMN IF NOT EXISTS overrides_recorded INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE agent_performance_projection
              ADD COLUMN IF NOT EXISTS override_rate DOUBLE PRECISION NOT NULL DEFAULT 0;
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
            confidence_delta, confidence_samples_delta = self._confidence_stats(event.payload)
            await self._increment(
                conn=conn,
                agent_id=event.payload.get("agent_id"),
                model_version=event.payload.get("model_version"),
                analyses_completed=1,
                confidence_delta=confidence_delta,
                confidence_samples_delta=confidence_samples_delta,
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
            confidence_delta, confidence_samples_delta = self._confidence_stats(event.payload)
            await self._increment(
                conn=conn,
                agent_id=orchestrator,
                model_version=version,
                decisions_recorded=1,
                confidence_delta=confidence_delta,
                confidence_samples_delta=confidence_samples_delta,
                last_global_position=event.global_position,
            )
            return

        if event.event_type == "HumanReviewCompleted":
            override_value = event.payload.get("override")
            overrides_recorded = 1 if bool(override_value) else 0
            await self._increment(
                conn=conn,
                agent_id=event.payload.get("reviewer_id"),
                model_version="human-review",
                human_reviews_completed=1,
                overrides_recorded=overrides_recorded,
                last_global_position=event.global_position,
            )

    def _confidence_stats(self, payload: dict[str, object]) -> tuple[float, int]:
        raw_confidence = payload.get("confidence_score")
        if raw_confidence is None:
            return 0.0, 0

        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            return 0.0, 0

        if not 0.0 <= confidence <= 1.0:
            return 0.0, 0
        return confidence, 1

    async def _increment(
        self,
        conn: asyncpg.Connection,
        agent_id: str | None,
        model_version: str | None,
        sessions_started: int = 0,
        analyses_completed: int = 0,
        fraud_screenings_completed: int = 0,
        decisions_recorded: int = 0,
        human_reviews_completed: int = 0,
        overrides_recorded: int = 0,
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
              human_reviews_completed,
              overrides_recorded,
              override_rate,
              total_confidence_score,
              confidence_samples,
              avg_confidence_score,
              last_global_position,
              updated_at
            )
            VALUES (
              $1, $2, $3::integer, $4::integer, $5::integer, $6::integer,
              $7::integer, $8::integer,
              CASE
                WHEN $7::integer > 0
                THEN $8::double precision / $7::double precision
                ELSE 0
              END,
              $9::double precision,
              $10::integer,
              CASE
                WHEN $10::integer > 0
                THEN $9::double precision / $10::double precision
                ELSE 0
              END,
              $11, NOW()
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
              human_reviews_completed =
                agent_performance_projection.human_reviews_completed
                + EXCLUDED.human_reviews_completed,
              overrides_recorded =
                agent_performance_projection.overrides_recorded
                + EXCLUDED.overrides_recorded,
              override_rate =
                CASE
                  WHEN (
                    agent_performance_projection.human_reviews_completed
                    + EXCLUDED.human_reviews_completed
                  ) > 0
                  THEN
                    (
                      agent_performance_projection.overrides_recorded
                      + EXCLUDED.overrides_recorded
                    )
                    / (
                      agent_performance_projection.human_reviews_completed
                      + EXCLUDED.human_reviews_completed
                    )::double precision
                  ELSE 0
                END,
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
            human_reviews_completed,
            overrides_recorded,
            confidence_delta,
            confidence_samples_delta,
            last_global_position,
        )
