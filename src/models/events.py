from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventStoreError(Exception):
    """Base class for event store errors."""


class DomainError(EventStoreError):
    """Raised when domain invariants or stream contracts are violated."""


class StreamNotFoundError(EventStoreError):
    def __init__(self, stream_id: str) -> None:
        super().__init__(f"Stream '{stream_id}' was not found.")
        self.stream_id = stream_id


class StreamArchivedError(EventStoreError):
    def __init__(self, stream_id: str) -> None:
        super().__init__(f"Stream '{stream_id}' is archived and does not accept new events.")
        self.stream_id = stream_id


class OptimisticConcurrencyError(EventStoreError):
    def __init__(self, stream_id: str, expected_version: int, actual_version: int) -> None:
        message = (
            f"Optimistic concurrency conflict for stream '{stream_id}': "
            f"expected_version={expected_version}, actual_version={actual_version}."
        )
        super().__init__(message)
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.suggested_action = "reload_stream_and_retry"


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    event_version: int = Field(default=1, ge=1)
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime


class StreamMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str
    new_stream_version: int
    events: list[StoredEvent]

