from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.upcasting.registry import UpcasterRegistry


def create_default_upcaster_registry() -> UpcasterRegistry:
    registry = UpcasterRegistry()

    @registry.upcaster(
        event_type="CreditAnalysisCompleted",
        from_version=1,
        to_version=2,
    )
    def _credit_analysis_completed_v1_to_v2(
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return upcast_credit_analysis_completed_v1_to_v2(payload, metadata)

    @registry.upcaster(
        event_type="DecisionGenerated",
        from_version=1,
        to_version=2,
    )
    def _decision_generated_v1_to_v2(
        payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return upcast_decision_generated_v1_to_v2(payload, metadata)

    return registry


def upcast_credit_analysis_completed_v1_to_v2(
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    recorded_at = _recorded_at(metadata)

    payload_model = payload.get("model_version")
    metadata_model = metadata.get("model_version")
    if isinstance(payload_model, str) and payload_model:
        inferred_model = payload_model
        model_inference_method = "payload:model_version"
        model_inference_confidence = "high"
    elif isinstance(metadata_model, str) and metadata_model:
        inferred_model = metadata_model
        model_inference_method = "metadata:model_version"
        model_inference_confidence = "high"
    else:
        inferred_model = _infer_credit_model_version_from_timestamp(recorded_at)
        model_inference_method = "timestamp:release_window"
        model_inference_confidence = "medium" if recorded_at else "low"

    raw_confidence = payload.get("confidence_score")
    confidence_score = _normalized_confidence(raw_confidence)
    confidence_inference = "payload:confidence_score" if confidence_score is not None else "null:unknown"

    regulatory_basis, regulatory_inference = _infer_regulatory_basis(
        payload=payload,
        metadata=metadata,
        recorded_at=recorded_at,
    )

    upcasted = {
        "application_id": payload.get("application_id"),
        "agent_id": payload.get("agent_id"),
        "session_id": payload.get("session_id"),
        "model_version": inferred_model,
        # Legacy v1 did not reliably contain confidence scoring in all paths.
        # Use null when it is genuinely unknown rather than inventing a value.
        "confidence_score": confidence_score,
        "risk_tier": payload.get("risk_tier"),
        "recommended_limit_usd": payload.get("recommended_limit_usd"),
        "analysis_duration_ms": payload.get("analysis_duration_ms"),
        "input_data_hash": payload.get("input_data_hash"),
        "regulatory_basis": regulatory_basis,
    }
    merged_metadata = dict(metadata)
    merged_metadata["upcast_notes"] = {
        "chain": "CreditAnalysisCompleted:v1->v2",
        "model_version_inference_method": model_inference_method,
        "model_version_inference_confidence": model_inference_confidence,
        "confidence_score_inference_method": confidence_inference,
        "regulatory_basis_inference_method": regulatory_inference,
    }
    return upcasted, merged_metadata


def upcast_decision_generated_v1_to_v2(
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    orchestrator = payload.get("orchestrator_agent_id")
    model_versions = _reconstruct_model_versions(payload=payload, metadata=metadata)
    inferred_model_version = metadata.get("orchestrator_model_version")
    if (
        orchestrator
        and isinstance(orchestrator, str)
        and orchestrator not in model_versions
    ):
        model_versions[orchestrator] = (
            str(inferred_model_version) if inferred_model_version else "legacy-unknown"
        )

    upcasted = {
        "application_id": payload.get("application_id"),
        "orchestrator_agent_id": orchestrator,
        "recommendation": payload.get("recommendation"),
        "confidence_score": payload.get("confidence_score"),
        "contributing_agent_sessions": payload.get("contributing_agent_sessions", []),
        "decision_basis_summary": payload.get(
            "decision_basis_summary",
            "LEGACY_EVENT_NO_SUMMARY",
        ),
        "model_versions": model_versions,
        "compliance_status": payload.get("compliance_status"),
        "assessed_max_limit_usd": payload.get("assessed_max_limit_usd"),
    }
    merged_metadata = dict(metadata)
    merged_metadata["upcast_notes"] = {
        "chain": "DecisionGenerated:v1->v2",
        "model_versions_inference": (
            "reconstructed_from_contributing_sessions_then_orchestrator_fallback"
        ),
        "model_versions_reconstructed_count": len(model_versions),
    }
    return upcasted, merged_metadata


def _recorded_at(metadata: dict[str, Any]) -> datetime | None:
    raw = (
        metadata.get("__recorded_at")
        or metadata.get("recorded_at")
        or metadata.get("event_recorded_at")
    )
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _infer_credit_model_version_from_timestamp(recorded_at: datetime | None) -> str:
    if recorded_at is None:
        return "legacy-unknown"
    if recorded_at < datetime(2025, 1, 1, tzinfo=UTC):
        return "credit-v1"
    if recorded_at < datetime(2025, 9, 1, tzinfo=UTC):
        return "credit-v1.5"
    return "credit-v2"


def _normalized_confidence(raw_confidence: Any) -> float | None:
    if raw_confidence is None:
        return None
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _infer_regulatory_basis(
    *,
    payload: dict[str, Any],
    metadata: dict[str, Any],
    recorded_at: datetime | None,
) -> tuple[str | None, str]:
    payload_basis = payload.get("regulatory_basis")
    if isinstance(payload_basis, str) and payload_basis:
        return payload_basis, "payload:regulatory_basis"

    metadata_ruleset = metadata.get("regulation_set_version")
    if isinstance(metadata_ruleset, str) and metadata_ruleset:
        return metadata_ruleset, "metadata:regulation_set_version"

    if recorded_at is None:
        return None, "null:insufficient_context"
    if recorded_at < datetime(2025, 7, 1, tzinfo=UTC):
        return "regset-2025.1", "timestamp:active_rule_versions"
    if recorded_at < datetime(2026, 1, 1, tzinfo=UTC):
        return "regset-2025.2", "timestamp:active_rule_versions"
    return "regset-2026.1", "timestamp:active_rule_versions"


def _reconstruct_model_versions(
    *,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, str]:
    existing = payload.get("model_versions")
    model_versions: dict[str, str] = {}
    if isinstance(existing, dict):
        for agent_id, model_version in existing.items():
            if not isinstance(agent_id, str) or not agent_id:
                continue
            if isinstance(model_version, str) and model_version:
                model_versions[agent_id] = model_version

    session_version_map = metadata.get("agent_session_model_versions")
    if not isinstance(session_version_map, dict):
        session_version_map = metadata.get("session_model_versions")
    if not isinstance(session_version_map, dict):
        session_version_map = {}

    agent_version_map = metadata.get("agent_model_versions")
    if not isinstance(agent_version_map, dict):
        agent_version_map = {}

    sessions = payload.get("contributing_agent_sessions")
    if not isinstance(sessions, list):
        sessions = []
    for stream_id in sessions:
        if not isinstance(stream_id, str):
            continue
        agent_id = _agent_id_from_session_stream(stream_id)
        if not agent_id or agent_id in model_versions:
            continue
        candidate = session_version_map.get(stream_id) or agent_version_map.get(agent_id)
        if isinstance(candidate, str) and candidate:
            model_versions[agent_id] = candidate
        else:
            model_versions[agent_id] = "legacy-unknown"
    return model_versions


def _agent_id_from_session_stream(stream_id: str) -> str | None:
    # Expected format: agent-<agent_id>-<session_id>
    if not stream_id.startswith("agent-"):
        return None
    remainder = stream_id[len("agent-") :]
    if "-" not in remainder:
        return None
    agent_id, _session_id = remainder.rsplit("-", 1)
    return agent_id or None
