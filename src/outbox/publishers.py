from __future__ import annotations

import json
from typing import Any

from src.event_store import EventStore
from src.outbox.relay import OutboxMessage

try:  # pragma: no cover - optional runtime dependency
    from aiokafka import AIOKafkaProducer
except Exception:  # pragma: no cover - optional runtime dependency
    AIOKafkaProducer = None  # type: ignore[assignment]


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


class KafkaOutboxPublisher:
    """Kafka delivery target for production-style outbox integration."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        client_id: str = "ledger-outbox-relay",
        compression_type: str | None = None,
        linger_ms: int = 10,
    ) -> None:
        if not bootstrap_servers.strip():
            raise ValueError("bootstrap_servers is required for Kafka publisher.")

        self.bootstrap_servers = bootstrap_servers
        self.client_id = client_id
        self.compression_type = compression_type
        self.linger_ms = linger_ms
        self._producer: Any | None = None

    async def start(self) -> None:
        if self._producer is not None:
            return
        if AIOKafkaProducer is None:
            raise RuntimeError(
                "Kafka publisher requested but aiokafka is not installed. "
                "Install aiokafka>=0.11.0."
            )

        producer_kwargs: dict[str, Any] = {
            "bootstrap_servers": self.bootstrap_servers,
            "client_id": self.client_id,
            "acks": "all",
            "linger_ms": self.linger_ms,
        }
        if self.compression_type:
            producer_kwargs["compression_type"] = self.compression_type

        producer = AIOKafkaProducer(**producer_kwargs)
        await producer.start()
        self._producer = producer

    async def stop(self) -> None:
        if self._producer is None:
            return
        producer = self._producer
        self._producer = None
        await producer.stop()

    async def publish(self, message: OutboxMessage) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka publisher is not started.")

        payload_bytes = json.dumps(
            message.payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        key = _message_key(message)
        kafka_headers = _kafka_headers(message.headers)

        await self._producer.send_and_wait(
            topic=message.topic,
            value=payload_bytes,
            key=key.encode("utf-8") if key else None,
            headers=kafka_headers,
        )


def _message_key(message: OutboxMessage) -> str:
    stream_id = message.payload.get("stream_id")
    if isinstance(stream_id, str) and stream_id:
        return stream_id
    return str(message.event_id)


def _kafka_headers(headers: dict[str, Any]) -> list[tuple[str, bytes]]:
    encoded: list[tuple[str, bytes]] = []
    for key, value in headers.items():
        if not key or value is None:
            continue
        if isinstance(value, str):
            encoded.append((key, value.encode("utf-8")))
            continue
        encoded.append((key, json.dumps(value, default=str).encode("utf-8")))
    return encoded
