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

    def validate_application_approval(self, approved_amount_usd: float) -> None:
        if self.compliance_status == "PENDING":
            raise DomainError("Cannot approve application while compliance checks are pending.")
        if (
            self.assessed_max_limit_usd is not None
            and approved_amount_usd > self.assessed_max_limit_usd
        ):
            raise DomainError(
                "Approved amount exceeds assessed maximum limit from agent analysis."
            )

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
