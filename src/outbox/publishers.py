from __future__ import annotations

from src.event_store import EventStore
from src.outbox.relay import OutboxMessage


class PostgresOutboxSinkPublisher:
    """Broker-free delivery target for local end-to-end outbox validation."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    async def ensure_schema(self) -> None:
        async with self.store._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_sink_events (
                  sink_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  outbox_id BIGINT NOT NULL UNIQUE REFERENCES outbox(outbox_id) ON DELETE CASCADE,
                  event_id UUID NOT NULL,
                  topic TEXT NOT NULL,
                  payload JSONB NOT NULL,
                  headers JSONB NOT NULL DEFAULT '{}'::jsonb,
                  delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_sink_topic_delivered
                  ON outbox_sink_events (topic, delivered_at DESC);
                """
            )

    async def publish(self, message: OutboxMessage) -> None:
        async with self.store._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO outbox_sink_events (
                  outbox_id,
                  event_id,
                  topic,
                  payload,
                  headers,
                  delivered_at
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, NOW())
                ON CONFLICT (outbox_id) DO NOTHING
                """,
                message.outbox_id,
                message.event_id,
                message.topic,
                message.payload,
                message.headers,
            )
