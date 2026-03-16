from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.models.events import DomainError, StoredEvent


@dataclass
class AuditLedgerAggregate:
    entity_type: str | None = None
    entity_id: str | None = None
    version: int = 0
    known_event_ids: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, events: list[StoredEvent]) -> AuditLedgerAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: StoredEvent) -> None:
        self._apply(event.event_type, event.payload, event.metadata)
        self.known_event_ids.add(str(event.event_id))
        self.version = event.stream_position

    def validate_new_integrity_event(
        self,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        correlation_id = metadata.get("correlation_id")
        if not correlation_id:
            raise DomainError("Audit events require metadata.correlation_id.")

        causation_id = metadata.get("causation_id")
        if causation_id and str(causation_id) not in self.known_event_ids:
            raise DomainError(
                "Audit causation_id must reference an existing event in the audit stream."
            )

        verified = int(payload.get("events_verified_count", 0))
        if verified < 0:
            raise DomainError("events_verified_count cannot be negative.")
        if not payload.get("integrity_hash"):
            raise DomainError("AuditIntegrityCheckRun requires integrity_hash.")

    def _apply(self, event_type: str, payload: dict[str, Any], metadata: dict[str, Any]) -> None:
        if event_type != "AuditIntegrityCheckRun":
            return
        self.validate_new_integrity_event(payload=payload, metadata=metadata)
        self.entity_id = payload.get("entity_id", self.entity_id)
