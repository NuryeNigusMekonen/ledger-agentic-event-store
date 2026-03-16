from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.event_store import EventStore
from src.models.events import StoredEvent


@dataclass(slots=True)
class ReconstructedContext:
    stream_id: str
    agent_id: str
    session_id: str
    model_version: str | None
    context_source: str | None
    event_replay_from_position: int | None
    included_events: list[StoredEvent]
    estimated_tokens: int
    token_budget: int
    dropped_events: int
    last_stream_position: int
    needs_reconciliation: bool
    reconciliation_reasons: list[str] = field(default_factory=list)


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 4096,
) -> ReconstructedContext:
    if token_budget <= 0:
        raise ValueError("token_budget must be positive.")

    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return ReconstructedContext(
            stream_id=stream_id,
            agent_id=agent_id,
            session_id=session_id,
            model_version=None,
            context_source=None,
            event_replay_from_position=None,
            included_events=[],
            estimated_tokens=0,
            token_budget=token_budget,
            dropped_events=0,
            last_stream_position=0,
            needs_reconciliation=True,
            reconciliation_reasons=["no_events_found"],
        )

    context_loaded = next(
        (event for event in events if event.event_type == "AgentContextLoaded"),
        None,
    )

    reasons: list[str] = []
    if context_loaded is None:
        reasons.append("missing_agent_context_loaded")

    selected, estimated_tokens, dropped = _select_events_with_budget(events, token_budget)

    if dropped > 0:
        reasons.append("token_budget_exhausted_partial_context")
    if selected and selected[0].stream_position > 1:
        reasons.append("context_window_is_partial")

    model_version = None
    context_source = None
    replay_from_position = None
    if context_loaded is not None:
        model_version = context_loaded.payload.get("model_version")
        context_source = context_loaded.payload.get("context_source")
        replay_from_position = context_loaded.payload.get("event_replay_from_position")

    return ReconstructedContext(
        stream_id=stream_id,
        agent_id=agent_id,
        session_id=session_id,
        model_version=model_version,
        context_source=context_source,
        event_replay_from_position=replay_from_position,
        included_events=selected,
        estimated_tokens=estimated_tokens,
        token_budget=token_budget,
        dropped_events=dropped,
        last_stream_position=events[-1].stream_position,
        needs_reconciliation=len(reasons) > 0,
        reconciliation_reasons=reasons,
    )


def _select_events_with_budget(
    events: list[StoredEvent],
    token_budget: int,
) -> tuple[list[StoredEvent], int, int]:
    context_loaded = next(
        (event for event in events if event.event_type == "AgentContextLoaded"),
        None,
    )

    total_tokens = 0
    selected_reversed: list[StoredEvent] = []
    dropped = 0
    mandatory_id = context_loaded.event_id if context_loaded else None

    for event in reversed(events):
        estimate = _estimate_event_tokens(event)
        is_mandatory = mandatory_id is not None and event.event_id == mandatory_id

        if is_mandatory:
            selected_reversed.append(event)
            total_tokens += estimate
            continue

        if total_tokens + estimate <= token_budget:
            selected_reversed.append(event)
            total_tokens += estimate
        else:
            dropped += 1

    selected = list(reversed(selected_reversed))
    return selected, total_tokens, dropped


def _estimate_event_tokens(event: StoredEvent) -> int:
    serialized = json.dumps(
        {
            "event_type": event.event_type,
            "payload": event.payload,
            "metadata": event.metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    # Rough token heuristic for English-ish JSON payloads.
    return max(1, len(serialized) // 4)

