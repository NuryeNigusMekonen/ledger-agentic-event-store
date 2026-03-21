from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.models.events import DomainError, StoredEvent


@dataclass
class AgentSessionAggregate:
    agent_id: str | None = None
    session_id: str | None = None
    context_loaded: bool = False
    model_version: str | None = None
    version: int = 0
    output_event_count: int = 0

    @classmethod
    def load(cls, events: list[StoredEvent]) -> AgentSessionAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: StoredEvent) -> None:
        self._apply(event.event_type, event.payload)
        self.version = event.stream_position

    def ensure_ready_for_output(self, event_type: str, model_version: str) -> None:
        if not self.context_loaded:
            raise DomainError(f"{event_type} requires AgentContextLoaded first.")
        if not model_version:
            raise DomainError(f"{event_type} requires a model version.")
        if self.model_version is not None and model_version != self.model_version:
            raise DomainError(
                f"{event_type} model version mismatch. "
                f"Expected '{self.model_version}', got '{model_version}'."
            )

    def validate_credit_analysis_submission(
        self,
        *,
        model_version: str,
        confidence_score: float,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> None:
        self.ensure_ready_for_output(
            event_type="CreditAnalysisCompleted",
            model_version=model_version,
        )
        if not (0.0 <= confidence_score <= 1.0):
            raise DomainError("confidence_score must be between 0.0 and 1.0.")
        if recommended_limit_usd <= 0:
            raise DomainError("recommended_limit_usd must be > 0.")
        if analysis_duration_ms <= 0:
            raise DomainError("analysis_duration_ms must be > 0.")
        if not input_data_hash:
            raise DomainError("input_data_hash is required.")

    def validate_fraud_screening_submission(
        self,
        *,
        screening_model_version: str,
        fraud_score: float,
        input_data_hash: str,
    ) -> None:
        self.ensure_ready_for_output(
            event_type="FraudScreeningCompleted",
            model_version=screening_model_version,
        )
        if not (0.0 <= fraud_score <= 1.0):
            raise DomainError("fraud_score must be between 0.0 and 1.0.")
        if not input_data_hash:
            raise DomainError("input_data_hash is required.")

    def _apply(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "AgentContextLoaded":
            self._apply_agent_context_loaded(payload)
            return
        if event_type == "CreditAnalysisCompleted":
            self._apply_credit_analysis_completed(payload)
            return
        if event_type == "FraudScreeningCompleted":
            self._apply_fraud_screening_completed(payload)
            return

    def _apply_agent_context_loaded(self, payload: dict[str, Any]) -> None:
        if self.context_loaded:
            raise DomainError("AgentContextLoaded can only happen once per session stream.")
        self.agent_id = payload["agent_id"]
        self.session_id = payload["session_id"]
        model_version = str(payload.get("model_version", "")).strip()
        if not model_version:
            raise DomainError("AgentContextLoaded requires model_version.")
        self.model_version = model_version
        self.context_loaded = True

    def _apply_credit_analysis_completed(self, payload: dict[str, Any]) -> None:
        model_version = str(payload.get("model_version", "")).strip()
        self.ensure_ready_for_output("CreditAnalysisCompleted", model_version=model_version)
        self.output_event_count += 1

    def _apply_fraud_screening_completed(self, payload: dict[str, Any]) -> None:
        model_version = str(payload.get("screening_model_version", "")).strip()
        self.ensure_ready_for_output("FraudScreeningCompleted", model_version=model_version)
        self.output_event_count += 1
