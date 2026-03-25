from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncpg
from pydantic import BaseModel

from src.event_store import EventStore
from src.models.events import AuditIntegrityCheckRunEvent, BaseEvent, DomainError, StoredEvent

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
    tamper_detected: bool
    final_hash: str
    violations: list[IntegrityViolation]
    audit_stream_id: str | None = None
    audit_event_id: str | None = None


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
    *,
    append_audit_event: bool = False,
    audit_entity_type: str | None = None,
    audit_entity_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> IntegrityCheckResult:
    events = await _load_raw_stream_events(
        store=store,
        stream_id=stream_id,
        from_position=from_position,
        to_position=to_position,
    )
    previous_hash = GENESIS_HASH
    rolling_chain_hash = GENESIS_HASH
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
        rolling_chain_hash = hashlib.sha256(
            f"{rolling_chain_hash}{computed_hash}".encode("utf-8")
        ).hexdigest()

    result = IntegrityCheckResult(
        stream_id=stream_id,
        events_verified_count=len(events),
        chain_valid=len(violations) == 0,
        tamper_detected=len(violations) > 0,
        final_hash=rolling_chain_hash,
        violations=violations,
    )
    if not append_audit_event:
        return result

    entity_type = audit_entity_type
    entity_id = audit_entity_id
    if entity_type is None or entity_id is None:
        inferred = _infer_entity_from_stream_id(stream_id)
        if inferred is None:
            raise DomainError(
                "append_audit_event=True requires audit_entity_type/entity_id "
                "when stream_id cannot be inferred."
            )
        entity_type, entity_id = inferred

    audit_stream_id = f"audit-{entity_type}-{entity_id}"
    previous_audit_hash = await _latest_audit_hash(store=store, audit_stream_id=audit_stream_id)
    audit_append = await store.append(
        stream_id=audit_stream_id,
        aggregate_type="AuditLedger",
        expected_version=await store.stream_version(audit_stream_id),
        events=[
            AuditIntegrityCheckRunEvent(
                payload={
                    "entity_id": entity_id,
                    "check_timestamp": datetime.now(UTC).isoformat(),
                    "events_verified_count": result.events_verified_count,
                    "integrity_hash": result.final_hash,
                    "previous_hash": previous_audit_hash,
                    "chain_valid": result.chain_valid,
                    "tamper_detected": result.tamper_detected,
                },
                metadata={
                    "correlation_id": correlation_id or str(uuid4()),
                    **({"causation_id": causation_id} if causation_id else {}),
                    "actor_id": "compliance-service",
                },
            )
        ],
        stream_metadata={
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
        correlation_id=correlation_id,
        causation_id=causation_id,
    )
    result.audit_stream_id = audit_stream_id
    result.audit_event_id = str(audit_append.events[-1].event_id)
    return result


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


def _infer_entity_from_stream_id(stream_id: str) -> tuple[str, str] | None:
    if stream_id.startswith("loan-"):
        return "application", stream_id[len("loan-") :]
    if stream_id.startswith("compliance-"):
        return "compliance_record", stream_id[len("compliance-") :]
    if stream_id.startswith("agent-"):
        return "agent_session", stream_id
    if stream_id.startswith("audit-"):
        return "audit_ledger", stream_id[len("audit-") :]
    return None


async def _latest_audit_hash(store: EventStore, audit_stream_id: str) -> str | None:
    audit_events = await store.load_stream(audit_stream_id)
    for event in reversed(audit_events):
        if event.event_type != "AuditIntegrityCheckRun":
            continue
        value = event.payload.get("integrity_hash")
        if isinstance(value, str) and value:
            return value
    return None
