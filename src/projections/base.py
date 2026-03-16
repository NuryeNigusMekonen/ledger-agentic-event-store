from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg

from src.models.events import StoredEvent


class Projection(Protocol):
    name: str

    async def ensure_schema(self, conn: asyncpg.Connection) -> None:
        ...

    async def reset(self, conn: asyncpg.Connection) -> None:
        ...

    async def apply(self, conn: asyncpg.Connection, event: StoredEvent) -> None:
        ...


@dataclass(slots=True)
class ProjectionLag:
    projection_name: str
    checkpoint_position: int
    latest_position: int
    events_behind: int
    lag_ms: float
    status: str
    updated_at: datetime

