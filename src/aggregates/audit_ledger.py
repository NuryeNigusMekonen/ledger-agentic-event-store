from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.models.events import AuditIntegrityCheckRunPayload, DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


@dataclass
class AuditLedgerAggregate:
    entity_type: str | None = None
    entity_id: str | None = None
    version: int = 0
    known_event_ids: set[str] = field(default_factory=set)

    @classmethod
    def replay(cls, events: list[StoredEvent]) -> AuditLedgerAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    @classmethod
    async def load(cls, store: EventStore, stream_id: str) -> AuditLedgerAggregate:
        events = await store.load_stream(stream_id)
        return cls.replay(events)

    def apply(self, event: StoredEvent) -> None:
        self._apply(event.event_type, event.payload, event.metadata)
        self.known_event_ids.add(str(event.event_id))
        self.version = event.stream_position

    def validate_new_integrity_event(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        typed = AuditIntegrityCheckRunPayload.model_validate(payload)
        correlation_id = metadata.get("correlation_id")
        if not correlation_id:
            raise DomainError("Audit events require metadata.correlation_id.")

        causation_id = metadata.get("causation_id")
        if causation_id and str(causation_id) not in self.known_event_ids:
            raise DomainError(
                "Audit causation_id must reference an existing event in the audit stream."
            )

        verified = int(typed.events_verified_count)
        if verified < 0:
            raise DomainError("events_verified_count cannot be negative.")
        if not typed.integrity_hash:
            raise DomainError("AuditIntegrityCheckRun requires integrity_hash.")

    def _apply(self, event_type: str, payload: dict[str, Any], metadata: dict[str, Any]) -> None:
        if event_type != "AuditIntegrityCheckRun":
            return
        self.validate_new_integrity_event(payload=payload, metadata=metadata)
        typed = AuditIntegrityCheckRunPayload.model_validate(payload)
        self.entity_id = typed.entity_id or self.entity_id
