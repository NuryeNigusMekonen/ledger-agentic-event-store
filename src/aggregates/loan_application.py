from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.models.events import DomainError, StoredEvent


class LoanStatus(StrEnum):
    EMPTY = "EMPTY"
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


TERMINAL_STATUSES = {LoanStatus.FINAL_APPROVED, LoanStatus.FINAL_DECLINED}


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
    def load(cls, events: list[StoredEvent]) -> LoanApplicationAggregate:
        aggregate = cls()
        for event in events:
            aggregate.apply(event)
        return aggregate

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def can_submit(self) -> bool:
        return self.status == LoanStatus.EMPTY

    def ensure_mutable(self) -> None:
        if self.is_terminal:
            raise DomainError(f"Loan '{self.application_id}' is already final: {self.status}.")

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
        if self.status != LoanStatus.EMPTY:
            raise DomainError("ApplicationSubmitted can only occur on an empty aggregate.")
        requested = float(payload["requested_amount_usd"])
        if requested <= 0:
            raise DomainError("requested_amount_usd must be greater than zero.")
        self.application_id = payload["application_id"]
        self.requested_amount_usd = requested
        self.status = LoanStatus.SUBMITTED

    def _apply_credit_analysis_requested(self) -> None:
        self.ensure_mutable()
        if self.status not in {LoanStatus.SUBMITTED, LoanStatus.AWAITING_ANALYSIS}:
            raise DomainError(
                f"CreditAnalysisRequested invalid in current state '{self.status.value}'."
            )
        self.status = LoanStatus.AWAITING_ANALYSIS

    def _apply_decision_generated(self, payload: dict[str, Any]) -> None:
        self.ensure_mutable()
        recommendation = str(payload["recommendation"]).upper()
        if recommendation not in {"APPROVE", "DECLINE", "REFER"}:
            raise DomainError(
                "DecisionGenerated recommendation must be APPROVE, DECLINE, or REFER."
            )

        self.decision_recommendation = recommendation
        self.compliance_status = str(payload.get("compliance_status", self.compliance_status))

        assessed_max = payload.get("assessed_max_limit_usd")
        if assessed_max is not None:
            self.assessed_max_limit_usd = float(assessed_max)

        sessions = payload.get("contributing_agent_sessions", [])
        for session_ref in sessions:
            self._known_agent_sessions.add(str(session_ref))

        if recommendation == "APPROVE":
            self.status = LoanStatus.APPROVED_PENDING_HUMAN
        elif recommendation == "DECLINE":
            self.status = LoanStatus.DECLINED_PENDING_HUMAN
        else:
            self.status = LoanStatus.PENDING_DECISION

    def _apply_human_review_completed(self, payload: dict[str, Any]) -> None:
        self.ensure_mutable()
        if self.status not in {
            LoanStatus.APPROVED_PENDING_HUMAN,
            LoanStatus.DECLINED_PENDING_HUMAN,
            LoanStatus.PENDING_DECISION,
        }:
            raise DomainError(
                f"HumanReviewCompleted invalid in current state '{self.status.value}'."
            )

        override = bool(payload.get("override", False))
        if override and not payload.get("override_reason"):
            raise DomainError("override_reason is required when override=True.")

        self.final_decision = str(payload["final_decision"]).upper()
        self._seen_review_event = True

    def _apply_application_approved(self, payload: dict[str, Any]) -> None:
        self.ensure_mutable()
        if not self._seen_review_event:
            raise DomainError("ApplicationApproved requires a prior HumanReviewCompleted event.")
        approved_amount = float(payload["approved_amount_usd"])
        self.validate_application_approval(approved_amount)
        self.final_decision = "APPROVE"
        self.status = LoanStatus.FINAL_APPROVED

    def _apply_application_declined(self, payload: dict[str, Any]) -> None:
        self.ensure_mutable()
        if not self._seen_review_event:
            raise DomainError("ApplicationDeclined requires a prior HumanReviewCompleted event.")
        _ = payload
        self.final_decision = "DECLINE"
        self.status = LoanStatus.FINAL_DECLINED
