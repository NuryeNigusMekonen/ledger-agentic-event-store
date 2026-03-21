from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventStoreError(Exception):
    """Base class for event store errors."""


class DomainError(EventStoreError):
    """Raised when domain invariants or stream contracts are violated."""


class StreamNotFoundError(EventStoreError):
    def __init__(self, stream_id: str) -> None:
        super().__init__(f"Stream '{stream_id}' was not found.")
        self.stream_id = stream_id


class StreamArchivedError(EventStoreError):
    def __init__(self, stream_id: str) -> None:
        super().__init__(f"Stream '{stream_id}' is archived and does not accept new events.")
        self.stream_id = stream_id


class OptimisticConcurrencyError(EventStoreError):
    def __init__(self, stream_id: str, expected_version: int, actual_version: int) -> None:
        message = (
            f"Optimistic concurrency conflict for stream '{stream_id}': "
            f"expected_version={expected_version}, actual_version={actual_version}."
        )
        super().__init__(message)
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.suggested_action = "reload_stream_and_retry"


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    event_version: int = Field(default=1, ge=1)
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApplicationSubmittedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    requested_amount_usd: float = Field(gt=0)
    applicant_id: str | None = None
    loan_purpose: str | None = None
    submission_channel: str | None = None
    submitted_at: str | None = None


class CreditAnalysisRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    assigned_agent_id: str | None = None
    requested_at: str | None = None
    priority: str | None = None
    source: str | None = None
    docpkg_stream_id: str | None = None


class AgentContextLoadedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_id: str
    model_version: str
    context_source: str | None = None
    event_replay_from_position: int | None = Field(default=None, ge=0)
    context_token_count: int | None = Field(default=None, gt=0)


class CreditAnalysisCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    agent_id: str
    session_id: str
    recommended_limit_usd: float | None = Field(default=None, gt=0)
    input_data_hash: str | None = None
    model_version: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_tier: str | None = None
    analysis_duration_ms: int | None = Field(default=None, ge=0)


class FraudScreeningCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    agent_id: str
    fraud_score: float = Field(ge=0.0, le=1.0)
    anomaly_flags: list[str] = Field(default_factory=list)
    screening_model_version: str
    input_data_hash: str
    session_id: str | None = None


class ComplianceCheckRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: str | None = None
    evidence_hash: str | None = None


class ComplianceRuleFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool = False


class DecisionGeneratedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    recommendation: str
    orchestrator_agent_id: str | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    contributing_agent_sessions: list[str] = Field(default_factory=list)
    decision_basis_summary: str | None = None
    model_versions: dict[str, str] = Field(default_factory=dict)
    compliance_status: str | None = None
    assessed_max_limit_usd: float | None = None


class HumanReviewCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    reviewer_id: str
    final_decision: str
    override: bool = False
    override_reason: str | None = None


class ApplicationApprovedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    approved_amount_usd: float = Field(gt=0)
    interest_rate: float | None = None
    conditions: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    effective_date: str | None = None


class ApplicationDeclinedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    decline_reasons: list[str] = Field(default_factory=list)
    declined_by: str | None = None
    adverse_action_notice_required: bool = True


class AuditIntegrityCheckRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    events_verified_count: int = Field(ge=0)
    integrity_hash: str
    check_timestamp: str | None = None
    previous_hash: str | None = None


class ApplicationSubmittedEvent(BaseEvent):
    event_type: Literal["ApplicationSubmitted"] = "ApplicationSubmitted"
    payload: ApplicationSubmittedPayload


class CreditAnalysisRequestedEvent(BaseEvent):
    event_type: Literal["CreditAnalysisRequested"] = "CreditAnalysisRequested"
    payload: CreditAnalysisRequestedPayload


class AgentContextLoadedEvent(BaseEvent):
    event_type: Literal["AgentContextLoaded"] = "AgentContextLoaded"
    payload: AgentContextLoadedPayload


class CreditAnalysisCompletedEvent(BaseEvent):
    event_type: Literal["CreditAnalysisCompleted"] = "CreditAnalysisCompleted"
    payload: CreditAnalysisCompletedPayload


class FraudScreeningCompletedEvent(BaseEvent):
    event_type: Literal["FraudScreeningCompleted"] = "FraudScreeningCompleted"
    payload: FraudScreeningCompletedPayload


class ComplianceCheckRequestedEvent(BaseEvent):
    event_type: Literal["ComplianceCheckRequested"] = "ComplianceCheckRequested"
    payload: ComplianceCheckRequestedPayload


class ComplianceRulePassedEvent(BaseEvent):
    event_type: Literal["ComplianceRulePassed"] = "ComplianceRulePassed"
    payload: ComplianceRulePassedPayload


class ComplianceRuleFailedEvent(BaseEvent):
    event_type: Literal["ComplianceRuleFailed"] = "ComplianceRuleFailed"
    payload: ComplianceRuleFailedPayload


class DecisionGeneratedEvent(BaseEvent):
    event_type: Literal["DecisionGenerated"] = "DecisionGenerated"
    event_version: int = 2
    payload: DecisionGeneratedPayload


class HumanReviewCompletedEvent(BaseEvent):
    event_type: Literal["HumanReviewCompleted"] = "HumanReviewCompleted"
    payload: HumanReviewCompletedPayload


class ApplicationApprovedEvent(BaseEvent):
    event_type: Literal["ApplicationApproved"] = "ApplicationApproved"
    payload: ApplicationApprovedPayload


class ApplicationDeclinedEvent(BaseEvent):
    event_type: Literal["ApplicationDeclined"] = "ApplicationDeclined"
    payload: ApplicationDeclinedPayload


class AuditIntegrityCheckRunEvent(BaseEvent):
    event_type: Literal["AuditIntegrityCheckRun"] = "AuditIntegrityCheckRun"
    payload: AuditIntegrityCheckRunPayload


class StoredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any]
    recorded_at: datetime


class StreamMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream_id: str
    new_stream_version: int
    events: list[StoredEvent]
