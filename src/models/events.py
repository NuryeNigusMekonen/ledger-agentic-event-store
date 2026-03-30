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


class DocumentUploadRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    requested_at: str
    requested_by: str | None = None
    document_path: str | None = None


class DocumentUploadedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    uploaded_at: str
    uploaded_by: str | None = None
    document_path: str | None = None


class PackageCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    package_id: str
    created_at: str


class DocumentAddedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    document_path: str
    document_type: str
    added_at: str


class DocumentFormatValidatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    document_path: str
    format: str
    is_supported: bool


class ExtractionStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    document_path: str
    started_at: str
    pipeline: str


class ExtractionCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    document_path: str
    facts: dict[str, Any]
    fact_provenance: dict[str, Any] = Field(default_factory=dict)
    extraction_context: dict[str, Any] = Field(default_factory=dict)
    field_confidence: dict[str, float]
    extraction_notes: list[str] = Field(default_factory=list)
    completed_at: str


class QualityAssessmentCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    overall_confidence: float = Field(ge=0.0, le=1.0)
    is_coherent: bool
    anomalies: list[str] = Field(default_factory=list)
    critical_missing_fields: list[str] = Field(default_factory=list)
    reextraction_recommended: bool
    auditor_notes: str | None = None


class PackageReadyForAnalysisPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    package_id: str
    ready_at: str


class CreditAnalysisRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    assigned_agent_id: str | None = None
    requested_at: str | None = None
    priority: str | None = None
    source: str | None = None
    docpkg_stream_id: str | None = None


class FraudScreeningRequestedPayload(BaseModel):
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


class AgentSessionStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_id: str
    model_version: str
    context_source: str
    context_token_count: int = Field(gt=0)
    started_at: str | None = None


class AgentSessionRecoveredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_id: str
    recovered_from_session_id: str
    context_source: str
    recovered_at: str | None = None


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
    regulatory_basis: str | None = None


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


class ComplianceCheckCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    overall_verdict: str
    completed_checks: int = Field(ge=0)
    total_checks: int = Field(ge=0)
    failed_rule_ids: list[str] = Field(default_factory=list)
    completed_at: str | None = None


class DecisionRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    requested_at: str
    requested_by: str | None = None
    required_inputs: list[str] = Field(default_factory=list)


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


class HumanReviewRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    requested_at: str
    requested_by: str | None = None
    recommendation: str | None = None


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
    chain_valid: bool | None = None
    tamper_detected: bool | None = None


class ApplicationSubmittedEvent(BaseEvent):
    event_type: Literal["ApplicationSubmitted"] = "ApplicationSubmitted"
    payload: ApplicationSubmittedPayload


class DocumentUploadRequestedEvent(BaseEvent):
    event_type: Literal["DocumentUploadRequested"] = "DocumentUploadRequested"
    payload: DocumentUploadRequestedPayload


class DocumentUploadedEvent(BaseEvent):
    event_type: Literal["DocumentUploaded"] = "DocumentUploaded"
    payload: DocumentUploadedPayload


class PackageCreatedEvent(BaseEvent):
    event_type: Literal["PackageCreated"] = "PackageCreated"
    payload: PackageCreatedPayload


class DocumentAddedEvent(BaseEvent):
    event_type: Literal["DocumentAdded"] = "DocumentAdded"
    payload: DocumentAddedPayload


class DocumentFormatValidatedEvent(BaseEvent):
    event_type: Literal["DocumentFormatValidated"] = "DocumentFormatValidated"
    payload: DocumentFormatValidatedPayload


class ExtractionStartedEvent(BaseEvent):
    event_type: Literal["ExtractionStarted"] = "ExtractionStarted"
    payload: ExtractionStartedPayload


class ExtractionCompletedEvent(BaseEvent):
    event_type: Literal["ExtractionCompleted"] = "ExtractionCompleted"
    payload: ExtractionCompletedPayload


class QualityAssessmentCompletedEvent(BaseEvent):
    event_type: Literal["QualityAssessmentCompleted"] = "QualityAssessmentCompleted"
    payload: QualityAssessmentCompletedPayload


class PackageReadyForAnalysisEvent(BaseEvent):
    event_type: Literal["PackageReadyForAnalysis"] = "PackageReadyForAnalysis"
    payload: PackageReadyForAnalysisPayload


class CreditAnalysisRequestedEvent(BaseEvent):
    event_type: Literal["CreditAnalysisRequested"] = "CreditAnalysisRequested"
    payload: CreditAnalysisRequestedPayload


class FraudScreeningRequestedEvent(BaseEvent):
    event_type: Literal["FraudScreeningRequested"] = "FraudScreeningRequested"
    payload: FraudScreeningRequestedPayload


class AgentSessionStartedEvent(BaseEvent):
    event_type: Literal["AgentSessionStarted"] = "AgentSessionStarted"
    payload: AgentSessionStartedPayload


class AgentSessionRecoveredEvent(BaseEvent):
    event_type: Literal["AgentSessionRecovered"] = "AgentSessionRecovered"
    payload: AgentSessionRecoveredPayload


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


class ComplianceCheckCompletedEvent(BaseEvent):
    event_type: Literal["ComplianceCheckCompleted"] = "ComplianceCheckCompleted"
    payload: ComplianceCheckCompletedPayload


class DecisionRequestedEvent(BaseEvent):
    event_type: Literal["DecisionRequested"] = "DecisionRequested"
    payload: DecisionRequestedPayload


class DecisionGeneratedEvent(BaseEvent):
    event_type: Literal["DecisionGenerated"] = "DecisionGenerated"
    event_version: int = 2
    payload: DecisionGeneratedPayload


class HumanReviewRequestedEvent(BaseEvent):
    event_type: Literal["HumanReviewRequested"] = "HumanReviewRequested"
    payload: HumanReviewRequestedPayload


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
