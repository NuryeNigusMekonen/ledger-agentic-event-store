from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg

from src.models.events import (
    AppendResult,
    BaseEvent,
    DomainError,
    OptimisticConcurrencyError,
    StoredEvent,
    StreamArchivedError,
    StreamMetadata,
    StreamNotFoundError,
)
from src.upcasting.registry import UpcasterRegistry
from src.upcasting.upcasters import create_default_upcaster_registry


class EventStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        upcaster_registry: UpcasterRegistry | None = None,
    ) -> None:
        self._pool = pool
        self._upcaster_registry = upcaster_registry or create_default_upcaster_registry()

    @classmethod
    async def from_dsn(
        cls,
        dsn: str,
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout: float = 10.0,
        upcaster_registry: UpcasterRegistry | None = None,
    ) -> EventStore:
        async def _init_connection(conn: asyncpg.Connection) -> None:
            await conn.set_type_codec(
                "json",
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )
            await conn.set_type_codec(
                "jsonb",
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )

        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            init=_init_connection,
            timeout=connect_timeout,
        )
        return cls(pool=pool, upcaster_registry=upcaster_registry)

    async def close(self) -> None:
        await self._pool.close()

    def set_upcaster_registry(self, registry: UpcasterRegistry) -> None:
        self._upcaster_registry = registry

    async def apply_schema(self, schema_path: str | Path) -> None:
        sql = Path(schema_path).read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def append(
        self,
        stream_id: str,
        aggregate_type: str,
        events: list[BaseEvent],
        expected_version: int,
        stream_metadata: dict[str, Any] | None = None,
        outbox_topic: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> AppendResult:
        if not events:
            raise DomainError("append() requires at least one event.")

        metadata_patch = stream_metadata or {}
        effective_outbox_topic = outbox_topic or f"{aggregate_type}.events"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                stream_row = await conn.fetchrow(
                    """
                    SELECT stream_id, aggregate_type, current_version, archived_at, metadata
                    FROM event_streams
                    WHERE stream_id = $1
                    FOR UPDATE
                    """,
                    stream_id,
                )

                if stream_row is None:
                    actual_version = 0
                    if expected_version != 0:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=actual_version,
                        )
                    await conn.execute(
                        """
                        INSERT INTO event_streams (
                          stream_id,
                          aggregate_type,
                          current_version,
                          metadata
                        )
                        VALUES ($1, $2, 0, $3::jsonb)
                        """,
                        stream_id,
                        aggregate_type,
                        metadata_patch,
                    )
                    current_version = 0
                else:
                    current_version = int(stream_row["current_version"])
                    if stream_row["aggregate_type"] != aggregate_type:
                        raise DomainError(
                            f"Aggregate type mismatch for stream '{stream_id}': "
                            f"expected '{stream_row['aggregate_type']}', got '{aggregate_type}'."
                        )
                    if stream_row["archived_at"] is not None:
                        raise StreamArchivedError(stream_id)
                    if current_version != expected_version:
                        raise OptimisticConcurrencyError(
                            stream_id=stream_id,
                            expected_version=expected_version,
                            actual_version=current_version,
                        )

                inserted_events: list[StoredEvent] = []
                for offset, event in enumerate(events, start=1):
                    stream_position = current_version + offset
                    event_metadata = _event_metadata_with_lineage(
                        metadata=event.metadata,
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                    )
                    row = await conn.fetchrow(
                        """
                        INSERT INTO events (
                          stream_id,
                          stream_position,
                          event_type,
                          event_version,
                          payload,
                          metadata
                        )
                        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
                        RETURNING
                          event_id,
                          stream_id,
                          stream_position,
                          global_position,
                          event_type,
                          event_version,
                          payload,
                          metadata,
                          recorded_at
                        """,
                        stream_id,
                        stream_position,
                        event.event_type,
                        event.event_version,
                        event.payload,
                        event_metadata,
                    )
                    inserted = _row_to_stored_event(row)
                    inserted_events.append(inserted)

                    await conn.execute(
                        """
                        INSERT INTO outbox (event_id, topic, payload, headers)
                        VALUES ($1, $2, $3::jsonb, $4::jsonb)
                        """,
                        inserted.event_id,
                        effective_outbox_topic,
                        {
                            "event_id": str(inserted.event_id),
                            "stream_id": stream_id,
                            "event_type": inserted.event_type,
                            "event_version": inserted.event_version,
                            "payload": inserted.payload,
                            "metadata": inserted.metadata,
                            "recorded_at": inserted.recorded_at.isoformat(),
                        },
                        {
                            "aggregate_type": aggregate_type,
                            "correlation_id": correlation_id,
                            "causation_id": causation_id,
                        },
                    )

                new_stream_version = current_version + len(events)
                await conn.execute(
                    """
                    UPDATE event_streams
                    SET
                      current_version = $2,
                      metadata = metadata || $3::jsonb
                    WHERE stream_id = $1
                    """,
                    stream_id,
                    new_stream_version,
                    metadata_patch,
                )

                return AppendResult(
                    stream_id=stream_id,
                    new_stream_version=new_stream_version,
                    events=inserted_events,
                )

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 1,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        if limit is not None:
            rows = await self._pool.fetch(
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
                WHERE stream_id = $1 AND stream_position >= $2
                ORDER BY stream_position ASC
                LIMIT $3
                """,
                stream_id,
                from_position,
                limit,
            )
        else:
            rows = await self._pool.fetch(
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
                WHERE stream_id = $1 AND stream_position >= $2
                ORDER BY stream_position ASC
                """,
                stream_id,
                from_position,
            )
        return [
            _row_to_stored_event(row, registry=self._upcaster_registry)
            for row in rows
        ]

    async def load_all(
        self,
        from_global_position: int = 0,
        limit: int | None = None,
        batch_size: int = 500,
        event_type: str | None = None,
    ) -> AsyncIterator[list[StoredEvent]]:
        if batch_size <= 0:
            raise DomainError("load_all() requires batch_size > 0.")

        cursor = from_global_position
        remaining = limit

        while True:
            fetch_limit = batch_size if remaining is None else min(batch_size, remaining)
            if fetch_limit <= 0:
                break

            if event_type is None:
                rows = await self._pool.fetch(
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
                    WHERE global_position > $1
                    ORDER BY global_position ASC
                    LIMIT $2
                    """,
                    cursor,
                    fetch_limit,
                )
            else:
                rows = await self._pool.fetch(
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
                    WHERE global_position > $1 AND event_type = $2
                    ORDER BY global_position ASC
                    LIMIT $3
                    """,
                    cursor,
                    event_type,
                    fetch_limit,
                )

            if not rows:
                break

            batch = [
                _row_to_stored_event(row, registry=self._upcaster_registry)
                for row in rows
            ]
            yield batch

            cursor = batch[-1].global_position
            if remaining is not None:
                remaining -= len(batch)
                if remaining <= 0:
                    break

    async def stream_version(self, stream_id: str) -> int:
        row = await self._pool.fetchrow(
            """
            SELECT current_version
            FROM event_streams
            WHERE stream_id = $1
            """,
            stream_id,
        )
        if row is None:
            return 0
        return int(row["current_version"])

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        row = await self._pool.fetchrow(
            """
            SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata
            FROM event_streams
            WHERE stream_id = $1
            """,
            stream_id,
        )
        if row is None:
            raise StreamNotFoundError(stream_id)
        return _row_to_stream_metadata(row)

    async def set_stream_metadata(
        self,
        stream_id: str,
        metadata: dict[str, Any],
        merge: bool = True,
    ) -> StreamMetadata:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT stream_id FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                    stream_id,
                )
                if row is None:
                    raise StreamNotFoundError(stream_id)

                if merge:
                    updated = await conn.fetchrow(
                        """
                        UPDATE event_streams
                        SET metadata = metadata || $2::jsonb
                        WHERE stream_id = $1
                        RETURNING
                          stream_id,
                          aggregate_type,
                          current_version,
                          created_at,
                          archived_at,
                          metadata
                        """,
                        stream_id,
                        metadata,
                    )
                else:
                    updated = await conn.fetchrow(
                        """
                        UPDATE event_streams
                        SET metadata = $2::jsonb
                        WHERE stream_id = $1
                        RETURNING
                          stream_id,
                          aggregate_type,
                          current_version,
                          created_at,
                          archived_at,
                          metadata
                        """,
                        stream_id,
                        metadata,
                    )
                return _row_to_stream_metadata(updated)

    async def archive_stream(
        self,
        stream_id: str,
        reason: str | None = None,
    ) -> StreamMetadata:
        patch: dict[str, Any] = {}
        if reason:
            patch["archived_reason"] = reason
        patch["archived_by"] = "system"
        patch["archived_at"] = datetime.now(UTC).isoformat()

        row = await self._pool.fetchrow(
            """
            UPDATE event_streams
            SET
              archived_at = NOW(),
              metadata = metadata || $2::jsonb
            WHERE stream_id = $1
            RETURNING stream_id, aggregate_type, current_version, created_at, archived_at, metadata
            """,
            stream_id,
            patch,
        )
        if row is None:
            raise StreamNotFoundError(stream_id)
        return _row_to_stream_metadata(row)


def _row_to_stored_event(
    row: asyncpg.Record,
    registry: UpcasterRegistry | None = None,
) -> StoredEvent:
    payload = dict(row["payload"])
    metadata = dict(row["metadata"])
    event_version = int(row["event_version"])

    if registry is not None:
        upcasted = registry.upcast(
            event_type=row["event_type"],
            version=event_version,
            payload=payload,
            metadata=metadata,
        )
        payload = upcasted.payload
        metadata = upcasted.metadata
        event_version = upcasted.current_version

    return StoredEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=int(row["stream_position"]),
        global_position=int(row["global_position"]),
        event_type=row["event_type"],
        event_version=event_version,
        payload=payload,
        metadata=metadata,
        recorded_at=row["recorded_at"],
    )


def _row_to_stream_metadata(row: asyncpg.Record) -> StreamMetadata:
    return StreamMetadata(
        stream_id=row["stream_id"],
        aggregate_type=row["aggregate_type"],
        current_version=int(row["current_version"]),
        created_at=row["created_at"],
        archived_at=row["archived_at"],
        metadata=dict(row["metadata"]),
    )


def _event_metadata_with_lineage(
    metadata: dict[str, Any],
    correlation_id: str | None,
    causation_id: str | None,
) -> dict[str, Any]:
    enriched = dict(metadata)
    if correlation_id is not None:
        enriched["correlation_id"] = correlation_id
    if causation_id is not None:
        enriched["causation_id"] = causation_id
    return enriched
