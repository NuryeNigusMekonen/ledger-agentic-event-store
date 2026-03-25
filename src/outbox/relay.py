from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from src.event_store import EventStore


@dataclass(slots=True)
class OutboxMessage:
    outbox_id: int
    event_id: UUID
    topic: str
    payload: dict[str, Any]
    headers: dict[str, Any]
    attempts: int
    created_at: datetime
    next_attempt_at: datetime


@dataclass(slots=True)
class OutboxRunResult:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    dead_lettered: int = 0


class OutboxPublisher(Protocol):
    async def publish(self, message: OutboxMessage) -> None:
        """Publish one outbox message to the target bus/sink."""


class OutboxRelay:
    def __init__(
        self,
        store: EventStore,
        publisher: OutboxPublisher,
        batch_size: int = 100,
        max_attempts: int = 8,
        retry_base_seconds: float = 0.5,
        retry_max_seconds: float = 60.0,
        claim_ttl_seconds: float = 30.0,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0.")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be > 0.")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be > 0.")
        if retry_max_seconds <= 0:
            raise ValueError("retry_max_seconds must be > 0.")
        if claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be > 0.")

        self.store = store
        self.publisher = publisher
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.claim_ttl_seconds = claim_ttl_seconds

        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    async def run_once(self) -> OutboxRunResult:
        now = datetime.now(UTC)
        claim_until = now + timedelta(seconds=self.claim_ttl_seconds)
        messages = await self._claim_batch(now=now, claim_until=claim_until)
        result = OutboxRunResult(claimed=len(messages))

        for message in messages:
            try:
                await self.publisher.publish(message)
            except Exception as exc:
                result.failed += 1
                dead_lettered = await self._mark_failure(
                    outbox_id=message.outbox_id,
                    attempts=message.attempts,
                    error=exc,
                )
                if dead_lettered:
                    result.dead_lettered += 1
            else:
                await self._mark_published(outbox_id=message.outbox_id)
                result.published += 1

        return result

    async def run_forever(self, poll_interval: float = 0.5) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0.")
        self._stop_event.clear()
        while not self._stop_event.is_set():
            result = await self.run_once()
            if result.claimed == 0:
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

    async def _claim_batch(self, now: datetime, claim_until: datetime) -> list[OutboxMessage]:
        async with self.store._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH claim AS (
                  SELECT outbox_id
                  FROM outbox
                  WHERE status = 'pending'
                    AND next_attempt_at <= $1
                  ORDER BY outbox_id ASC
                  LIMIT $2
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE outbox AS o
                SET
                  attempts = o.attempts + 1,
                  next_attempt_at = $3,
                  last_error = NULL
                FROM claim
                WHERE o.outbox_id = claim.outbox_id
                RETURNING
                  o.outbox_id,
                  o.event_id,
                  o.topic,
                  o.payload,
                  o.headers,
                  o.attempts,
                  o.created_at,
                  o.next_attempt_at
                """,
                now,
                self.batch_size,
                claim_until,
            )

        return [
            OutboxMessage(
                outbox_id=int(row["outbox_id"]),
                event_id=row["event_id"],
                topic=str(row["topic"]),
                payload=dict(row["payload"] or {}),
                headers=dict(row["headers"] or {}),
                attempts=int(row["attempts"]),
                created_at=row["created_at"],
                next_attempt_at=row["next_attempt_at"],
            )
            for row in rows
        ]

    async def _mark_published(self, outbox_id: int) -> None:
        async with self.store._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE outbox
                SET
                  status = 'published',
                  published_at = NOW(),
                  last_error = NULL
                WHERE outbox_id = $1
                  AND status = 'pending'
                """,
                outbox_id,
            )

    async def _mark_failure(self, outbox_id: int, attempts: int, error: Exception) -> bool:
        error_text = _truncate_error(error)
        if attempts >= self.max_attempts:
            async with self.store._pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE outbox
                    SET
                      status = 'dead_letter',
                      last_error = $2
                    WHERE outbox_id = $1
                      AND status = 'pending'
                    """,
                    outbox_id,
                    error_text,
                )
            return True

        delay_seconds = self._retry_delay_seconds(attempts=attempts)
        next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        async with self.store._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE outbox
                SET
                  status = 'pending',
                  next_attempt_at = $2,
                  last_error = $3
                WHERE outbox_id = $1
                  AND status = 'pending'
                """,
                outbox_id,
                next_attempt_at,
                error_text,
            )
        return False

    def _retry_delay_seconds(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        delay = self.retry_base_seconds * (2**exponent)
        return min(delay, self.retry_max_seconds)


def _truncate_error(error: Exception, limit: int = 1000) -> str:
    text = str(error).strip() or error.__class__.__name__
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
