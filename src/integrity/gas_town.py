from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.event_store import EventStore
from src.models.events import StoredEvent


@dataclass(slots=True)
class PendingWorkItem:
    work_type: str
    description: str
    related_event_type: str
    related_stream_position: int


@dataclass(slots=True)
class AgentContext:
    stream_id: str
    agent_id: str
    session_id: str
    context_text: str
    last_event_position: int
    pending_work: list[PendingWorkItem] = field(default_factory=list)
    session_health_status: str = "HEALTHY"


# Backward compatibility alias for existing imports.
ReconstructedContext = AgentContext


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 4096,
) -> AgentContext:
    if token_budget <= 0:
        raise ValueError("token_budget must be positive.")

    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)
    if not events:
        return AgentContext(
            stream_id=stream_id,
            agent_id=agent_id,
            session_id=session_id,
            context_text="No prior events found for this session.",
            last_event_position=0,
            pending_work=[
                PendingWorkItem(
                    work_type="SESSION_INITIALIZATION",
                    description="Session has no events. Start a new agent session.",
                    related_event_type="NONE",
                    related_stream_position=0,
                )
            ],
            session_health_status="NEEDS_RECONCILIATION",
        )

    pending_work = _derive_pending_work(events)
    health_status = _session_health_status(events, pending_work)
    preserved, summarized = _partition_events_for_context(events)
    context_text = _build_context_text(
        stream_id=stream_id,
        events=events,
        preserved_events=preserved,
        summarized_events=summarized,
        pending_work=pending_work,
        token_budget=token_budget,
    )
    return AgentContext(
        stream_id=stream_id,
        agent_id=agent_id,
        session_id=session_id,
        context_text=context_text,
        last_event_position=events[-1].stream_position,
        pending_work=pending_work,
        session_health_status=health_status,
    )


def _derive_pending_work(events: list[StoredEvent]) -> list[PendingWorkItem]:
    pending: list[PendingWorkItem] = []
    open_requests: dict[str, StoredEvent] = {}

    request_to_completion = {
        "CreditAnalysisRequested": "CreditAnalysisCompleted",
        "FraudScreeningRequested": "FraudScreeningCompleted",
        "DecisionRequested": "DecisionGenerated",
        "HumanReviewRequested": "HumanReviewCompleted",
    }
    completion_to_request = {value: key for key, value in request_to_completion.items()}

    for event in events:
        request_event_type = completion_to_request.get(event.event_type)
        if request_event_type:
            open_requests.pop(request_event_type, None)
            continue

        if event.event_type in request_to_completion:
            open_requests[event.event_type] = event

        if _event_is_pending_or_error(event) and event.event_type not in open_requests:
            pending.append(
                PendingWorkItem(
                    work_type="PENDING_OR_ERROR_STATE",
                    description="Event indicates pending/error state requiring follow-up.",
                    related_event_type=event.event_type,
                    related_stream_position=event.stream_position,
                )
            )

    for event_type, request_event in open_requests.items():
        completion_event = request_to_completion[event_type]
        pending.append(
            PendingWorkItem(
                work_type="REQUEST_INCOMPLETE",
                description=f"{event_type} has no {completion_event} completion event.",
                related_event_type=event_type,
                related_stream_position=request_event.stream_position,
            )
        )

    if _last_event_is_unfinished_decision(events):
        last_event = events[-1]
        pending.append(
            PendingWorkItem(
                work_type="DECISION_NOT_FINALIZED",
                description=(
                    "Last event is decision output without downstream completion "
                    "(human review/final outcome)."
                ),
                related_event_type=last_event.event_type,
                related_stream_position=last_event.stream_position,
            )
        )

    return _dedupe_pending(pending)


def _session_health_status(
    events: list[StoredEvent],
    pending_work: list[PendingWorkItem],
) -> str:
    has_context_loaded = any(event.event_type == "AgentContextLoaded" for event in events)
    if not has_context_loaded:
        return "NEEDS_RECONCILIATION"
    if _last_event_is_unfinished_decision(events):
        return "NEEDS_RECONCILIATION"
    if pending_work:
        return "DEGRADED"
    return "HEALTHY"


def _partition_events_for_context(
    events: list[StoredEvent],
) -> tuple[list[StoredEvent], list[StoredEvent]]:
    last_three_positions = {event.stream_position for event in events[-3:]}
    preserved: list[StoredEvent] = []
    summarized: list[StoredEvent] = []

    for event in events:
        if event.stream_position in last_three_positions or _event_is_pending_or_error(event):
            preserved.append(event)
        else:
            summarized.append(event)
    return preserved, summarized


def _build_context_text(
    *,
    stream_id: str,
    events: list[StoredEvent],
    preserved_events: list[StoredEvent],
    summarized_events: list[StoredEvent],
    pending_work: list[PendingWorkItem],
    token_budget: int,
) -> str:
    total_events = len(events)
    last_position = events[-1].stream_position if events else 0
    lines: list[str] = [
        f"Agent session reconstruction for {stream_id}.",
        f"Total events: {total_events}; last_event_position: {last_position}.",
    ]

    if summarized_events:
        lines.append(_summarize_older_events(summarized_events))
    else:
        lines.append("Earlier history summary: none (all events preserved verbatim).")

    if pending_work:
        pending_chunks = [
            f"{item.work_type} at position {item.related_stream_position}: {item.description}"
            for item in pending_work
        ]
        lines.append("Pending work: " + "; ".join(pending_chunks))
    else:
        lines.append("Pending work: none.")

    lines.append("Verbatim preserved events:")
    for event in preserved_events:
        lines.append(_event_as_verbatim_line(event))

    context_text = "\n".join(lines)
    if _estimate_tokens(context_text) <= token_budget:
        return context_text

    compact_lines = lines[:3]
    compact_lines.append("Pending work count: " + str(len(pending_work)))
    compact_lines.append("Verbatim preserved events (compact):")
    for event in preserved_events:
        compact_lines.append(
            json.dumps(
                {
                    "stream_position": event.stream_position,
                    "event_type": event.event_type,
                    "payload": event.payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    compact_text = "\n".join(compact_lines)
    if _estimate_tokens(compact_text) <= token_budget:
        return compact_text

    return "\n".join(compact_lines[:4])


def _summarize_older_events(events: list[StoredEvent]) -> str:
    counts: dict[str, int] = {}
    first_at: datetime | None = None
    last_at: datetime | None = None
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        if first_at is None or event.recorded_at < first_at:
            first_at = event.recorded_at
        if last_at is None or event.recorded_at > last_at:
            last_at = event.recorded_at

    ordered = sorted(counts.items(), key=lambda item: item[0])
    parts = [f"{event_type} x{count}" for event_type, count in ordered]
    time_window = ""
    if first_at and last_at:
        time_window = (
            " between "
            + first_at.astimezone(UTC).isoformat()
            + " and "
            + last_at.astimezone(UTC).isoformat()
        )
    return "Earlier history summary: " + ", ".join(parts) + time_window + "."


def _event_as_verbatim_line(event: StoredEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def _last_event_is_unfinished_decision(events: list[StoredEvent]) -> bool:
    if not events:
        return False
    last = events[-1]
    decision_events = {"DecisionGenerated", "DecisionRequested"}
    completion_events = {
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
        "DecisionCompleted",
        "DecisionFinalized",
    }
    if last.event_type not in decision_events:
        return False
    return not any(event.event_type in completion_events for event in events[last.stream_position :])


def _event_is_pending_or_error(event: StoredEvent) -> bool:
    pending_error_markers = {"PENDING", "ERROR", "FAILED"}
    for value in _iter_string_values(event.payload):
        upper = value.upper()
        if any(marker in upper for marker in pending_error_markers):
            return True
    for value in _iter_string_values(event.metadata):
        upper = value.upper()
        if any(marker in upper for marker in pending_error_markers):
            return True

    event_type_upper = event.event_type.upper()
    if "PENDING" in event_type_upper or "ERROR" in event_type_upper:
        return True
    if event.event_type in {"DecisionRequested", "HumanReviewRequested"}:
        return True
    return False


def _iter_string_values(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_string_values(item)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _dedupe_pending(items: list[PendingWorkItem]) -> list[PendingWorkItem]:
    deduped: list[PendingWorkItem] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        key = (item.related_event_type, item.related_stream_position, item.work_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
