from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg

from src.event_store import EventStore
from src.projections.base import Projection, ProjectionLag


class ProjectionDaemon:
    def __init__(
        self,
        store: EventStore,
        projections: list[Projection] | None = None,
        batch_size: int = 250,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        self.store = store
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._projections: dict[str, Projection] = {}
        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

        for projection in projections or []:
            self.register(projection)

    def register(self, projection: Projection) -> None:
        self._projections[projection.name] = projection

    def register_many(self, projections: list[Projection]) -> None:
        for projection in projections:
            self.register(projection)

    async def initialize(self) -> None:
        if not self._projections:
            return
        async with self.store._pool.acquire() as conn:
            for projection in self._projections.values():
                await projection.ensure_schema(conn)
                await self._ensure_checkpoint_row(conn, projection.name)

    async def run_once(self, projection_name: str | None = None) -> dict[str, int]:
        if projection_name:
            projection = self._projections[projection_name]
            processed = await self._run_projection_batch(projection)
            return {projection_name: processed}

        results: dict[str, int] = {}
        for projection in self._projections.values():
            results[projection.name] = await self._run_projection_batch(projection)
        return results

    async def run_forever(self, poll_interval: float = 0.5) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            results = await self.run_once()
            processed = sum(results.values())
            if processed == 0:
                await asyncio.sleep(poll_interval)

    async def start(self, poll_interval: float = 0.5) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self.run_forever(poll_interval=poll_interval))

    async def stop(self) -> None:
        self._stop_event.set()
        if self._loop_task:
            await self._loop_task
            self._loop_task = None

    async def get_lag(self, projection_name: str) -> ProjectionLag:
        async with self.store._pool.acquire() as conn:
            checkpoint = await conn.fetchrow(
                """
                SELECT last_global_position, updated_at
                FROM projection_checkpoints
                WHERE projection_name = $1
                """,
                projection_name,
            )
            if checkpoint is None:
                raise KeyError(f"Unknown projection checkpoint '{projection_name}'.")

            latest = await conn.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events")
            checkpoint_position = int(checkpoint["last_global_position"])
            latest_position = int(latest or 0)
            events_behind = max(0, latest_position - checkpoint_position)
            updated_at = checkpoint["updated_at"]
            lag_ms = max(0.0, (datetime.now(UTC) - updated_at).total_seconds() * 1000)

            status = "OK"
            if lag_ms > 5000:
                status = "CRITICAL"
            elif lag_ms > 1000:
                status = "WARNING"

            return ProjectionLag(
                projection_name=projection_name,
                checkpoint_position=checkpoint_position,
                latest_position=latest_position,
                events_behind=events_behind,
                lag_ms=lag_ms,
                status=status,
                updated_at=updated_at,
            )

    async def get_all_lags(self) -> dict[str, ProjectionLag]:
        lags: dict[str, ProjectionLag] = {}
        for name in self._projections:
            lags[name] = await self.get_lag(name)
        return lags

    async def rebuild_projection(self, projection_name: str) -> None:
        projection = self._projections[projection_name]

        async with self.store._pool.acquire() as conn:
            async with conn.transaction():
                await projection.reset(conn)
                await conn.execute(
                    """
                    INSERT INTO projection_checkpoints (
                      projection_name,
                      last_global_position,
                      last_event_at,
                      updated_at,
                      metadata
                    )
                    VALUES ($1, 0, NULL, NOW(), '{"rebuilding": true}'::jsonb)
                    ON CONFLICT (projection_name)
                    DO UPDATE SET
                      last_global_position = 0,
                      last_event_at = NULL,
                      updated_at = NOW(),
                      metadata = projection_checkpoints.metadata || '{"rebuilding": true}'::jsonb
                    """,
                    projection_name,
                )

        while True:
            processed = await self.run_once(projection_name=projection_name)
            if processed[projection_name] == 0:
                break

        async with self.store._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE projection_checkpoints
                SET
                  metadata = metadata || jsonb_build_object(
                    'rebuilding', false,
                    'last_rebuild_at', NOW()::text
                  ),
                  updated_at = NOW()
                WHERE projection_name = $1
                """,
                projection_name,
            )

    async def rebuild_all(self) -> None:
        for name in self._projections:
            await self.rebuild_projection(name)

    async def _run_projection_batch(self, projection: Projection) -> int:
        checkpoint = await self._checkpoint_position(projection.name)
        async for events in self.store.load_all(
            from_global_position=checkpoint,
            limit=self.batch_size,
            batch_size=self.batch_size,
        ):
            for event in events:
                await self._apply_event_with_retry(projection=projection, event=event)
            return len(events)
        return 0

    async def _apply_event_with_retry(self, projection: Projection, event) -> None:
        attempt = 0
        while True:
            try:
                async with self.store._pool.acquire() as conn:
                    async with conn.transaction():
                        await projection.apply(conn, event)
                        await self._save_checkpoint(
                            conn=conn,
                            projection_name=projection.name,
                            global_position=event.global_position,
                            event_time=event.recorded_at,
                        )
                return
            except Exception:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                await asyncio.sleep(self.retry_delay_seconds * attempt)

    async def _checkpoint_position(self, projection_name: str) -> int:
        async with self.store._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_global_position
                FROM projection_checkpoints
                WHERE projection_name = $1
                """,
                projection_name,
            )
            if row is None:
                await self._ensure_checkpoint_row(conn, projection_name)
                return 0
            return int(row["last_global_position"])

    async def _ensure_checkpoint_row(self, conn: asyncpg.Connection, projection_name: str) -> None:
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (
              projection_name,
              last_global_position,
              last_event_at,
              updated_at,
              metadata
            )
            VALUES ($1, 0, NULL, NOW(), '{}'::jsonb)
            ON CONFLICT (projection_name) DO NOTHING
            """,
            projection_name,
        )

    async def _save_checkpoint(
        self,
        conn: asyncpg.Connection,
        projection_name: str,
        global_position: int,
        event_time: datetime,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO projection_checkpoints (
              projection_name,
              last_global_position,
              last_event_at,
              updated_at,
              metadata
            )
            VALUES ($1, $2, $3, NOW(), '{}'::jsonb)
            ON CONFLICT (projection_name)
            DO UPDATE SET
              last_global_position = EXCLUDED.last_global_position,
              last_event_at = EXCLUDED.last_event_at,
              updated_at = NOW()
            """,
            projection_name,
            global_position,
            event_time,
        )
