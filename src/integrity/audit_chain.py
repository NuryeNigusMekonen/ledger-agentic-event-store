from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import asyncpg
from pydantic import BaseModel

from src.event_store import EventStore
from src.models.events import BaseEvent, StoredEvent

GENESIS_HASH = "GENESIS"


@dataclass(slots=True)
class IntegrityViolation:
    stream_position: int
    event_id: str
    reason: str
    expected_previous_hash: str
    actual_previous_hash: str | None
    expected_integrity_hash: str
    actual_integrity_hash: str | None


@dataclass(slots=True)
class IntegrityCheckResult:
    stream_id: str
    events_verified_count: int
    chain_valid: bool
    final_hash: str
    violations: list[IntegrityViolation]


def compute_integrity_hash(
    *,
    stream_id: str,
    stream_position: int,
    event_type: str,
    event_version: int,
    payload: dict[str, Any],
    metadata: dict[str, Any],
    previous_hash: str,
) -> str:
    clean_metadata = {
        k: v
        for k, v in metadata.items()
        if k not in {"integrity_hash", "previous_hash"}
    }
    canonical = {
        "stream_id": stream_id,
        "stream_position": stream_position,
        "event_type": event_type,
        "event_version": event_version,
        "payload": payload,
        "metadata": clean_metadata,
        "previous_hash": previous_hash,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def attach_integrity_chain(
    *,
    stream_id: str,
    expected_version: int,
    events: list[BaseEvent],
    previous_hash: str = GENESIS_HASH,
) -> list[BaseEvent]:
    """Returns event copies with previous_hash and integrity_hash metadata attached."""
    hashed_events: list[BaseEvent] = []
    last_hash = previous_hash
    for offset, event in enumerate(events, start=1):
        stream_position = expected_version + offset
        base_metadata = dict(event.metadata)
        computed = compute_integrity_hash(
            stream_id=stream_id,
            stream_position=stream_position,
            event_type=event.event_type,
            event_version=event.event_version,
            payload=_json_object(event.payload),
            metadata=base_metadata,
            previous_hash=last_hash,
        )
        merged_metadata = dict(base_metadata)
        merged_metadata["previous_hash"] = last_hash
        merged_metadata["integrity_hash"] = computed
        hashed_events.append(event.model_copy(update={"metadata": merged_metadata}))
        last_hash = computed
    return hashed_events


async def run_integrity_check(
    store: EventStore,
    stream_id: str,
    from_position: int = 1,
    to_position: int | None = None,
) -> IntegrityCheckResult:
    events = await _load_raw_stream_events(
        store=store,
        stream_id=stream_id,
        from_position=from_position,
        to_position=to_position,
    )
    previous_hash = GENESIS_HASH
    violations: list[IntegrityViolation] = []

    for event in events:
        computed_hash = compute_integrity_hash(
            stream_id=event.stream_id,
            stream_position=event.stream_position,
            event_type=event.event_type,
            event_version=event.event_version,
            payload=event.payload,
            metadata=event.metadata,
            previous_hash=previous_hash,
        )
        actual_previous_hash = event.metadata.get("previous_hash")
        actual_integrity_hash = event.metadata.get("integrity_hash")

        is_prev_ok = actual_previous_hash == previous_hash
        is_hash_ok = actual_integrity_hash == computed_hash
        if not (is_prev_ok and is_hash_ok):
            reason = []
            if not is_prev_ok:
                reason.append("previous_hash_mismatch")
            if not is_hash_ok:
                reason.append("integrity_hash_mismatch")
            violations.append(
                IntegrityViolation(
                    stream_position=event.stream_position,
                    event_id=str(event.event_id),
                    reason=",".join(reason),
                    expected_previous_hash=previous_hash,
                    actual_previous_hash=actual_previous_hash,
                    expected_integrity_hash=computed_hash,
                    actual_integrity_hash=actual_integrity_hash,
                )
            )

        previous_hash = computed_hash

    return IntegrityCheckResult(
        stream_id=stream_id,
        events_verified_count=len(events),
        chain_valid=len(violations) == 0,
        final_hash=previous_hash,
        violations=violations,
    )


async def _load_raw_stream_events(
    *,
    store: EventStore,
    stream_id: str,
    from_position: int,
    to_position: int | None,
) -> list[StoredEvent]:
    query = """
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
    """
    args: list[Any] = [stream_id, from_position]
    if to_position is not None:
        query += " AND stream_position <= $3"
        args.append(to_position)
    query += " ORDER BY stream_position ASC"

    async with store._pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [_row_to_stored_event(row) for row in rows]


def _row_to_stored_event(row: asyncpg.Record) -> StoredEvent:
    return StoredEvent(
        event_id=row["event_id"],
        stream_id=row["stream_id"],
        stream_position=int(row["stream_position"]),
        global_position=int(row["global_position"]),
        event_type=row["event_type"],
        event_version=int(row["event_version"]),
        payload=dict(row["payload"]),
        metadata=dict(row["metadata"]),
        recorded_at=row["recorded_at"],
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return dict(value)
