from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from src.models.events import (
    ApplicationApprovedPayload,
    ApplicationDeclinedPayload,
    ApplicationSubmittedPayload,
    DecisionGeneratedPayload,
    DomainError,
    HumanReviewCompletedPayload,
    StoredEvent,
)

if TYPE_CHECKING:
    from src.event_store import EventStore


class LoanStatus(StrEnum):
    EMPTY = "EMPTY"
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


TERMINAL_STATUSES = {LoanStatus.FINAL_APPROVED, LoanStatus.FINAL_DECLINED}
CANONICAL_LOAN_STATES = {
    LoanStatus.SUBMITTED,
    LoanStatus.AWAITING_ANALYSIS,
    LoanStatus.PENDING_DECISION,
    LoanStatus.APPROVED_PENDING_HUMAN,
    LoanStatus.DECLINED_PENDING_HUMAN,
    LoanStatus.FINAL_APPROVED,
    LoanStatus.FINAL_DECLINED,
}


@dataclass
class LoanApplicationAggregate:
    application_id: str | None = None
    status: LoanStatus = LoanStatus.EMPTY
    requested_amount_usd: float | None = None
    assessed_max_limit_usd: float | None = None
    compliance_status: str = "UNKNOWN"
    decision_recommendation: str | None = None
    final_decision: str | None = None
    version: int = 0
    _seen_review_event: bool = False
    _known_agent_sessions: set[str] = field(default_factory=set)

    @classmethod
    def replay(cls, events: list[StoredEvent]) -> LoanApplicationAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    @classmethod
    async def load(cls, store: EventStore, stream_id: str) -> LoanApplicationAggregate:
        events = await store.load_stream(stream_id)
        return cls.replay(events)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def can_submit(self) -> bool:
        return self.status == LoanStatus.EMPTY

    def ensure_exists(self, application_id: str) -> None:
        if self.status == LoanStatus.EMPTY:
            raise DomainError(f"Loan application '{application_id}' does not exist.")

    def ensure_mutable(self) -> None:
        if self.is_terminal:
            raise DomainError(f"Loan '{self.application_id}' is already final: {self.status}.")

    def ensure_can_record_agent_analysis(self, application_id: str) -> None:
        self.ensure_exists(application_id)
        self.ensure_mutable()

    def apply(self, event: StoredEvent) -> None:
        self._apply(event.event_type, event.payload, event.metadata)
        self.version = event.stream_position

    def validate_application_approval(
        self,
        approved_amount_usd: float,
        *,
        compliance_status: str | None = None,
    ) -> None:
        effective_compliance_status = (compliance_status or self.compliance_status).upper()
        if effective_compliance_status != "CLEARED":
            raise DomainError("Cannot approve application unless compliance is CLEARED.")
        self.compliance_status = effective_compliance_status
        if (
            self.assessed_max_limit_usd is not None
            and approved_amount_usd > self.assessed_max_limit_usd
        ):
            raise DomainError(
                "Approved amount exceeds assessed maximum limit from agent analysis."
            )

    def validate_decision_generation(
        self,
        *,
        recommendation: str,
        confidence_score: float,
        compliance_status: str,
        contributing_agent_sessions: list[str],
        contributing_session_events: dict[str, list[StoredEvent]],
        assessed_max_limit_usd: float | None,
    ) -> str:
        self.ensure_exists(self.application_id or "unknown")
        self.ensure_mutable()

        normalized_recommendation = recommendation.upper()
        if normalized_recommendation not in {"APPROVE", "DECLINE", "REFER"}:
            raise DomainError(
                "DecisionGenerated recommendation must be APPROVE, DECLINE, or REFER."
            )
        if not (0.0 <= confidence_score <= 1.0):
            raise DomainError("confidence_score must be between 0.0 and 1.0.")
        if confidence_score < 0.6:
            normalized_recommendation = "REFER"

        normalized_compliance = compliance_status.upper()
        if normalized_compliance in {"NOT_STARTED", "PENDING"}:
            raise DomainError("Cannot generate decision while compliance is incomplete.")
        if normalized_recommendation == "APPROVE":
            if normalized_compliance != "CLEARED":
                raise DomainError("Cannot recommend APPROVE when compliance is not CLEARED.")
            if assessed_max_limit_usd is None:
                raise DomainError("APPROVE decision requires at least one credit analysis result.")

        self._validate_contributing_sessions(
            contributing_agent_sessions=contributing_agent_sessions,
            contributing_session_events=contributing_session_events,
        )
        return normalized_recommendation

    def validate_human_review_completion(
        self,
        *,
        final_decision: str,
        override: bool,
        override_reason: str | None,
        approved_amount_usd: float | None,
        compliance_status: str,
    ) -> str:
        self.ensure_exists(self.application_id or "unknown")
        self.ensure_mutable()

        normalized_decision = final_decision.upper()
        if normalized_decision not in {"APPROVE", "DECLINE"}:
            raise DomainError("final_decision must be APPROVE or DECLINE.")
        if override and not override_reason:
            raise DomainError("override_reason is required when override=True.")
        if normalized_decision == "APPROVE":
            if approved_amount_usd is None:
                raise DomainError("approved_amount_usd is required for APPROVE decision.")
            self.validate_application_approval(
                approved_amount_usd,
                compliance_status=compliance_status,
            )
        return normalized_decision

    def _apply(self, event_type: str, payload: dict[str, Any], metadata: dict[str, Any]) -> None:
        if event_type == "ApplicationSubmitted":
            self._apply_application_submitted(payload)
            return
        if event_type == "CreditAnalysisRequested":
            self._apply_credit_analysis_requested()
            return
        if event_type == "DecisionGenerated":
            self._apply_decision_generated(payload)
            return
        if event_type == "HumanReviewCompleted":
            self._apply_human_review_completed(payload)
            return
        if event_type == "ApplicationApproved":
            self._apply_application_approved(payload)
            return
        if event_type == "ApplicationDeclined":
            self._apply_application_declined(payload)
            return

        # Unknown event types are ignored for forward compatibility.
        _ = metadata

    def _apply_application_submitted(self, payload: dict[str, Any]) -> None:
        self._require_state("ApplicationSubmitted", {LoanStatus.EMPTY})
        typed = ApplicationSubmittedPayload.model_validate(payload)
        requested = float(typed.requested_amount_usd)
        if requested <= 0:
            raise DomainError("requested_amount_usd must be greater than zero.")
        self.application_id = typed.application_id
        self.requested_amount_usd = requested
        self.status = LoanStatus.SUBMITTED

    def _apply_credit_analysis_requested(self) -> None:
        self._require_state(
            "CreditAnalysisRequested",
            {LoanStatus.SUBMITTED, LoanStatus.AWAITING_ANALYSIS},
        )
        self.status = LoanStatus.AWAITING_ANALYSIS

    def _apply_decision_generated(self, payload: dict[str, Any]) -> None:
        self._require_state(
            "DecisionGenerated",
            {
                LoanStatus.AWAITING_ANALYSIS,
                LoanStatus.PENDING_DECISION,
                LoanStatus.APPROVED_PENDING_HUMAN,
                LoanStatus.DECLINED_PENDING_HUMAN,
            },
        )
        typed = DecisionGeneratedPayload.model_validate(payload)
        recommendation = typed.recommendation.upper()
        if recommendation not in {"APPROVE", "DECLINE", "REFER"}:
            raise DomainError(
                "DecisionGenerated recommendation must be APPROVE, DECLINE, or REFER."
            )
        if typed.confidence_score is not None and typed.confidence_score < 0.6:
            if recommendation != "REFER":
                raise DomainError(
                    "DecisionGenerated with confidence_score < 0.6 must use REFER."
                )
        if not typed.contributing_agent_sessions:
            raise DomainError("DecisionGenerated requires contributing_agent_sessions.")
        if recommendation == "APPROVE":
            compliance = (typed.compliance_status or self.compliance_status).upper()
            if compliance != "CLEARED":
                raise DomainError("DecisionGenerated APPROVE requires compliance_status CLEARED.")

        self.decision_recommendation = recommendation
        if typed.compliance_status is not None:
            self.compliance_status = typed.compliance_status

        assessed_max = typed.assessed_max_limit_usd
        if assessed_max is not None:
            self.assessed_max_limit_usd = float(assessed_max)

        sessions = typed.contributing_agent_sessions
        for session_ref in sessions:
            self._known_agent_sessions.add(str(session_ref))

        if recommendation == "APPROVE":
            self.status = LoanStatus.APPROVED_PENDING_HUMAN
        elif recommendation == "DECLINE":
            self.status = LoanStatus.DECLINED_PENDING_HUMAN
        else:
            self.status = LoanStatus.PENDING_DECISION

    def _apply_human_review_completed(self, payload: dict[str, Any]) -> None:
        self._require_state(
            "HumanReviewCompleted",
            {
                LoanStatus.APPROVED_PENDING_HUMAN,
                LoanStatus.DECLINED_PENDING_HUMAN,
                LoanStatus.PENDING_DECISION,
            },
        )
        typed = HumanReviewCompletedPayload.model_validate(payload)

        if typed.override and not typed.override_reason:
            raise DomainError("override_reason is required when override=True.")

        self.final_decision = typed.final_decision.upper()
        self._seen_review_event = True

    def _apply_application_approved(self, payload: dict[str, Any]) -> None:
        self._require_state(
            "ApplicationApproved",
            {
                LoanStatus.APPROVED_PENDING_HUMAN,
                LoanStatus.DECLINED_PENDING_HUMAN,
                LoanStatus.PENDING_DECISION,
            },
        )
        typed = ApplicationApprovedPayload.model_validate(payload)
        if not self._seen_review_event:
            raise DomainError("ApplicationApproved requires a prior HumanReviewCompleted event.")
        approved_amount = float(typed.approved_amount_usd)
        self.validate_application_approval(approved_amount)
        self.final_decision = "APPROVE"
        self.status = LoanStatus.FINAL_APPROVED

    def _apply_application_declined(self, payload: dict[str, Any]) -> None:
        self._require_state(
            "ApplicationDeclined",
            {
                LoanStatus.APPROVED_PENDING_HUMAN,
                LoanStatus.DECLINED_PENDING_HUMAN,
                LoanStatus.PENDING_DECISION,
            },
        )
        typed = ApplicationDeclinedPayload.model_validate(payload)
        if not self._seen_review_event:
            raise DomainError("ApplicationDeclined requires a prior HumanReviewCompleted event.")
        _ = typed
        self.final_decision = "DECLINE"
        self.status = LoanStatus.FINAL_DECLINED

    def _require_state(self, event_name: str, allowed: set[LoanStatus]) -> None:
        if self.status not in allowed:
            allowed_states = ", ".join(sorted(state.value for state in allowed))
            raise DomainError(
                f"{event_name} invalid in current state '{self.status.value}'. "
                f"Allowed: {allowed_states}."
            )

    def _validate_contributing_sessions(
        self,
        *,
        contributing_agent_sessions: list[str],
        contributing_session_events: dict[str, list[StoredEvent]],
    ) -> None:
        if not contributing_agent_sessions:
            raise DomainError("At least one contributing agent session is required.")
        if len(set(contributing_agent_sessions)) != len(contributing_agent_sessions):
            raise DomainError("contributing_agent_sessions must be unique.")

        current_application_id = str(self.application_id or "")
        for stream_id in contributing_agent_sessions:
            events = contributing_session_events.get(stream_id, [])
            if not events:
                raise DomainError(
                    f"Contributing session '{stream_id}' does not exist or has no events."
                )

            has_context_loaded = any(
                event.event_type == "AgentContextLoaded"
                for event in events
            )
            if not has_context_loaded:
                raise DomainError(
                    f"Contributing session '{stream_id}' is missing AgentContextLoaded."
                )

            has_relevant_output = any(
                event.event_type in {"CreditAnalysisCompleted", "FraudScreeningCompleted"}
                and str(event.payload.get("application_id", "")) == current_application_id
                for event in events
            )
            if not has_relevant_output:
                raise DomainError(
                    f"Contributing session '{stream_id}' has no analysis output for "
                    f"application '{current_application_id}'."
                )
