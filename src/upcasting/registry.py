from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.models.events import DomainError

UpcasterFn = Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class UpcasterDefinition:
    event_type: str
    from_version: int
    to_version: int
    fn: UpcasterFn


@dataclass(frozen=True, slots=True)
class UpcastResult:
    event_type: str
    original_version: int
    current_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    applied_steps: list[str]


class UpcasterRegistry:
    """Registry that applies version chain upcasters at read time."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int], UpcasterDefinition] = {}

    def register(
        self,
        event_type: str,
        from_version: int,
        to_version: int,
        fn: UpcasterFn,
    ) -> None:
        if from_version < 1 or to_version <= from_version:
            raise DomainError(
                f"Invalid upcaster chain for {event_type}: {from_version} -> {to_version}"
            )
        key = (event_type, from_version)
        if key in self._by_key:
            raise DomainError(f"Upcaster already registered for {event_type} v{from_version}.")
        self._by_key[key] = UpcasterDefinition(
            event_type=event_type,
            from_version=from_version,
            to_version=to_version,
            fn=fn,
        )

    def has_chain(self, event_type: str, version: int) -> bool:
        return (event_type, version) in self._by_key

    def upcast(
        self,
        event_type: str,
        version: int,
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> UpcastResult:
        current_version = version
        current_payload = dict(payload)
        current_metadata = dict(metadata)
        applied_steps: list[str] = []

        while True:
            key = (event_type, current_version)
            definition = self._by_key.get(key)
            if definition is None:
                break

            next_payload, next_metadata = definition.fn(
                dict(current_payload),
                dict(current_metadata),
            )
            applied_steps.append(
                f"{event_type}:v{definition.from_version}->v{definition.to_version}"
            )
            current_payload = next_payload
            current_metadata = next_metadata
            current_version = definition.to_version

        return UpcastResult(
            event_type=event_type,
            original_version=version,
            current_version=current_version,
            payload=current_payload,
            metadata=current_metadata,
            applied_steps=applied_steps,
        )
