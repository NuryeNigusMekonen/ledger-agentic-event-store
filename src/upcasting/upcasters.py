from __future__ import annotations

from typing import Any

from src.upcasting.registry import UpcasterRegistry


def create_default_upcaster_registry() -> UpcasterRegistry:
    registry = UpcasterRegistry()
    registry.register(
        event_type="CreditAnalysisCompleted",
        from_version=1,
        to_version=2,
        fn=upcast_credit_analysis_completed_v1_to_v2,
    )
    registry.register(
        event_type="DecisionGenerated",
        from_version=1,
        to_version=2,
        fn=upcast_decision_generated_v1_to_v2,
    )
    return registry


def upcast_credit_analysis_completed_v1_to_v2(
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    inferred_model = metadata.get("model_version")
    inference_method = "metadata:model_version" if inferred_model else "fallback:legacy_unknown"
    if not inferred_model:
        inferred_model = "legacy-unknown"

    upcasted = {
        "application_id": payload.get("application_id"),
        "agent_id": payload.get("agent_id"),
        "session_id": payload.get("session_id"),
        "model_version": inferred_model,
        "confidence_score": payload.get("confidence_score"),
        "risk_tier": payload.get("risk_tier"),
        "recommended_limit_usd": payload.get("recommended_limit_usd"),
        "analysis_duration_ms": payload.get("analysis_duration_ms", 0),
        "input_data_hash": payload.get("input_data_hash"),
    }
    merged_metadata = dict(metadata)
    merged_metadata["upcast_notes"] = {
        "chain": "CreditAnalysisCompleted:v1->v2",
        "model_version_inference_method": inference_method,
    }
    return upcasted, merged_metadata


def upcast_decision_generated_v1_to_v2(
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    orchestrator = payload.get("orchestrator_agent_id")
    inferred_model_version = metadata.get("orchestrator_model_version")
    model_versions = payload.get("model_versions")
    if not isinstance(model_versions, dict):
        model_versions = {}
    if orchestrator and orchestrator not in model_versions:
        model_versions[orchestrator] = inferred_model_version or "legacy-unknown"

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
    }
    merged_metadata = dict(metadata)
    merged_metadata["upcast_notes"] = {
        "chain": "DecisionGenerated:v1->v2",
        "model_versions_inference": "orchestrator_model_version_metadata_or_legacy_unknown",
    }
    return upcasted, merged_metadata

