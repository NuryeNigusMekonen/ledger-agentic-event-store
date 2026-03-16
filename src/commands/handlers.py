from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.audit_ledger import AuditLedgerAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import LoanApplicationAggregate, LoanStatus
from src.event_store import EventStore
from src.models.events import AppendResult, BaseEvent, DomainError, StoredEvent


@dataclass(slots=True)
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime
    correlation_id: str | None = None


@dataclass(slots=True)
class StartAgentSessionCommand:
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str
    correlation_id: str | None = None


@dataclass(slots=True)
class CreditAnalysisCompletedCommand:
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str
    correlation_id: str | None = None


@dataclass(slots=True)
class FraudScreeningCompletedCommand:
    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float
    anomaly_flags: list[str]
    screening_model_version: str
    input_data_hash: str
    correlation_id: str | None = None


@dataclass(slots=True)
class ComplianceCheckCommand:
    application_id: str
    regulation_set_version: str
    rule_id: str
    rule_version: str
    passed: bool
    checks_required: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    remediation_required: bool = False
    correlation_id: str | None = None


@dataclass(slots=True)
class GenerateDecisionCommand:
    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float
    decision_basis_summary: str
    contributing_agent_sessions: list[str]
    model_versions: dict[str, str]
    correlation_id: str | None = None


@dataclass(slots=True)
class HumanReviewCompletedCommand:
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None
    approved_amount_usd: float | None = None
    interest_rate: float | None = None
    conditions: list[str] = field(default_factory=list)
    effective_date: str | None = None
    decline_reasons: list[str] = field(default_factory=list)
    correlation_id: str | None = None


@dataclass(slots=True)
class RunIntegrityCheckCommand:
    entity_type: str
    entity_id: str
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None
    role: str
    correlation_id: str | None = None
    causation_id: str | None = None


class WriteCommandHandlers:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    async def handle_submit_application(self, command: SubmitApplicationCommand) -> AppendResult:
        # 1) Load
        stream_id = _loan_stream_id(command.application_id)
        stream_events = await self.store.load_stream(stream_id)
        loan = LoanApplicationAggregate.load(stream_events)

        # 2) Validate
        if not loan.can_submit():
            raise DomainError(f"Application '{command.application_id}' already exists.")
        if command.requested_amount_usd <= 0:
            raise DomainError("requested_amount_usd must be positive.")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events = [
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={
                    "application_id": command.application_id,
                    "applicant_id": command.applicant_id,
                    "requested_amount_usd": command.requested_amount_usd,
                    "loan_purpose": command.loan_purpose,
                    "submission_channel": command.submission_channel,
                    "submitted_at": command.submitted_at.isoformat(),
                },
                metadata=_metadata(correlation_id, actor_id=command.applicant_id),
            ),
            BaseEvent(
                event_type="CreditAnalysisRequested",
                payload={
                    "application_id": command.application_id,
                    "assigned_agent_id": "credit-analysis-router",
                    "requested_at": datetime.now(UTC).isoformat(),
                    "priority": "normal",
                },
                metadata=_metadata(
                    correlation_id,
                    actor_id=command.applicant_id,
                ),
            ),
        ]

        # 4) Append
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
            stream_metadata={"application_id": command.application_id},
        )

    async def handle_start_agent_session(self, command: StartAgentSessionCommand) -> AppendResult:
        # 1) Load
        stream_id = _agent_stream_id(command.agent_id, command.session_id)
        stream_events = await self.store.load_stream(stream_id)
        session = AgentSessionAggregate.load(stream_events)

        # 2) Validate
        if session.context_loaded:
            raise DomainError("Agent session is already initialized with context.")
        if command.context_token_count <= 0:
            raise DomainError("context_token_count must be > 0.")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events = [
            BaseEvent(
                event_type="AgentContextLoaded",
                payload={
                    "agent_id": command.agent_id,
                    "session_id": command.session_id,
                    "context_source": command.context_source,
                    "event_replay_from_position": command.event_replay_from_position,
                    "context_token_count": command.context_token_count,
                    "model_version": command.model_version,
                },
                metadata=_metadata(correlation_id, actor_id=command.agent_id),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="AgentSession",
            events=decided_events,
            expected_version=session.version,
            stream_metadata={
                "agent_id": command.agent_id,
                "session_id": command.session_id,
            },
        )

    async def handle_credit_analysis_completed(
        self,
        command: CreditAnalysisCompletedCommand,
    ) -> AppendResult:
        # 1) Load
        session_stream_id = _agent_stream_id(command.agent_id, command.session_id)
        session_events = await self.store.load_stream(session_stream_id)
        session = AgentSessionAggregate.load(session_events)

        loan_stream_id = _loan_stream_id(command.application_id)
        loan_events = await self.store.load_stream(loan_stream_id)
        loan = LoanApplicationAggregate.load(loan_events)

        # 2) Validate
        if loan.status == LoanStatus.EMPTY:
            raise DomainError(f"Loan application '{command.application_id}' does not exist.")
        loan.ensure_mutable()
        if not (0.0 <= command.confidence_score <= 1.0):
            raise DomainError("confidence_score must be between 0.0 and 1.0.")
        if command.recommended_limit_usd <= 0:
            raise DomainError("recommended_limit_usd must be > 0.")
        session.ensure_ready_for_output(
            event_type="CreditAnalysisCompleted",
            model_version=command.model_version,
        )

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events = [
            BaseEvent(
                event_type="CreditAnalysisCompleted",
                event_version=2,
                payload={
                    "application_id": command.application_id,
                    "agent_id": command.agent_id,
                    "session_id": command.session_id,
                    "model_version": command.model_version,
                    "confidence_score": command.confidence_score,
                    "risk_tier": command.risk_tier,
                    "recommended_limit_usd": command.recommended_limit_usd,
                    "analysis_duration_ms": command.analysis_duration_ms,
                    "input_data_hash": command.input_data_hash,
                },
                metadata=_metadata(correlation_id, actor_id=command.agent_id),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=session_stream_id,
            aggregate_type="AgentSession",
            events=decided_events,
            expected_version=session.version,
        )

    async def handle_fraud_screening_completed(
        self,
        command: FraudScreeningCompletedCommand,
    ) -> AppendResult:
        # 1) Load
        session_stream_id = _agent_stream_id(command.agent_id, command.session_id)
        session_events = await self.store.load_stream(session_stream_id)
        session = AgentSessionAggregate.load(session_events)

        loan_stream_id = _loan_stream_id(command.application_id)
        loan_events = await self.store.load_stream(loan_stream_id)
        loan = LoanApplicationAggregate.load(loan_events)

        # 2) Validate
        if loan.status == LoanStatus.EMPTY:
            raise DomainError(f"Loan application '{command.application_id}' does not exist.")
        loan.ensure_mutable()
        if not (0.0 <= command.fraud_score <= 1.0):
            raise DomainError("fraud_score must be between 0.0 and 1.0.")
        session.ensure_ready_for_output(
            event_type="FraudScreeningCompleted",
            model_version=command.screening_model_version,
        )

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events = [
            BaseEvent(
                event_type="FraudScreeningCompleted",
                payload={
                    "application_id": command.application_id,
                    "agent_id": command.agent_id,
                    "fraud_score": command.fraud_score,
                    "anomaly_flags": command.anomaly_flags,
                    "screening_model_version": command.screening_model_version,
                    "input_data_hash": command.input_data_hash,
                },
                metadata=_metadata(correlation_id, actor_id=command.agent_id),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=session_stream_id,
            aggregate_type="AgentSession",
            events=decided_events,
            expected_version=session.version,
        )

    async def handle_compliance_check(self, command: ComplianceCheckCommand) -> AppendResult:
        # 1) Load
        stream_id = _compliance_stream_id(command.application_id)
        events = await self.store.load_stream(stream_id)
        compliance = ComplianceRecordAggregate.load(events)

        # 2) Validate
        if not command.rule_version:
            raise DomainError("rule_version is required.")
        if compliance.status == "NOT_STARTED" and not command.checks_required:
            raise DomainError("checks_required is required when initializing compliance stream.")
        if compliance.regulation_set_version and (
            compliance.regulation_set_version != command.regulation_set_version
        ):
            raise DomainError("regulation_set_version mismatch for existing compliance stream.")
        if not command.passed and not command.failure_reason:
            raise DomainError("failure_reason is required when passed=False.")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events: list[BaseEvent] = []

        if compliance.status == "NOT_STARTED":
            decided_events.append(
                BaseEvent(
                    event_type="ComplianceCheckRequested",
                    payload={
                        "application_id": command.application_id,
                        "regulation_set_version": command.regulation_set_version,
                        "checks_required": command.checks_required,
                    },
                    metadata=_metadata(correlation_id, actor_id="compliance-engine"),
                )
            )

        if command.passed:
            decided_events.append(
                BaseEvent(
                    event_type="ComplianceRulePassed",
                    payload={
                        "application_id": command.application_id,
                        "rule_id": command.rule_id,
                        "rule_version": command.rule_version,
                        "evaluation_timestamp": datetime.now(UTC).isoformat(),
                        "evidence_hash": f"evidence:{command.rule_id}",
                    },
                    metadata=_metadata(correlation_id, actor_id="compliance-engine"),
                )
            )
        else:
            decided_events.append(
                BaseEvent(
                    event_type="ComplianceRuleFailed",
                    payload={
                        "application_id": command.application_id,
                        "rule_id": command.rule_id,
                        "rule_version": command.rule_version,
                        "failure_reason": command.failure_reason,
                        "remediation_required": command.remediation_required,
                    },
                    metadata=_metadata(correlation_id, actor_id="compliance-engine"),
                )
            )

        # 4) Append
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="ComplianceRecord",
            events=decided_events,
            expected_version=compliance.version,
            stream_metadata={"application_id": command.application_id},
        )

    async def handle_generate_decision(self, command: GenerateDecisionCommand) -> AppendResult:
        # 1) Load
        loan_stream_id = _loan_stream_id(command.application_id)
        loan_events = await self.store.load_stream(loan_stream_id)
        loan = LoanApplicationAggregate.load(loan_events)

        compliance_stream_id = _compliance_stream_id(command.application_id)
        compliance_events = await self.store.load_stream(compliance_stream_id)
        compliance = ComplianceRecordAggregate.load(compliance_events)

        contributing_events = await self._load_contributing_agent_events(
            command.contributing_agent_sessions
        )

        # 2) Validate
        if loan.status == LoanStatus.EMPTY:
            raise DomainError(f"Loan application '{command.application_id}' does not exist.")
        loan.ensure_mutable()
        if not contributing_events:
            raise DomainError("At least one contributing agent session is required.")
        if compliance.status == "NOT_STARTED":
            raise DomainError("Compliance stream not initialized for this application.")
        if compliance.is_pending:
            raise DomainError("Cannot generate decision while compliance is pending.")

        recommendation = command.recommendation.upper()
        if recommendation == "APPROVE" and not compliance.is_cleared:
            raise DomainError("Cannot recommend APPROVE when compliance is not CLEARED.")
        if not (0.0 <= command.confidence_score <= 1.0):
            raise DomainError("confidence_score must be between 0.0 and 1.0.")

        assessed_max_limit = _extract_assessed_max_limit(contributing_events)
        if recommendation == "APPROVE" and assessed_max_limit is None:
            raise DomainError("APPROVE decision requires at least one credit analysis result.")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events = [
            BaseEvent(
                event_type="DecisionGenerated",
                event_version=2,
                payload={
                    "application_id": command.application_id,
                    "orchestrator_agent_id": command.orchestrator_agent_id,
                    "recommendation": recommendation,
                    "confidence_score": command.confidence_score,
                    "contributing_agent_sessions": command.contributing_agent_sessions,
                    "decision_basis_summary": command.decision_basis_summary,
                    "model_versions": command.model_versions,
                    "compliance_status": compliance.status,
                    "assessed_max_limit_usd": assessed_max_limit,
                },
                metadata=_metadata(correlation_id, actor_id=command.orchestrator_agent_id),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=loan_stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
        )

    async def handle_human_review_completed(
        self,
        command: HumanReviewCompletedCommand,
    ) -> AppendResult:
        # 1) Load
        loan_stream_id = _loan_stream_id(command.application_id)
        loan_events = await self.store.load_stream(loan_stream_id)
        loan = LoanApplicationAggregate.load(loan_events)

        compliance_stream_id = _compliance_stream_id(command.application_id)
        compliance_events = await self.store.load_stream(compliance_stream_id)
        compliance = ComplianceRecordAggregate.load(compliance_events)

        # 2) Validate
        if loan.status == LoanStatus.EMPTY:
            raise DomainError(f"Loan application '{command.application_id}' does not exist.")
        loan.ensure_mutable()
        final_decision = command.final_decision.upper()
        if final_decision not in {"APPROVE", "DECLINE"}:
            raise DomainError("final_decision must be APPROVE or DECLINE.")
        if command.override and not command.override_reason:
            raise DomainError("override_reason is required when override=True.")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        decided_events: list[BaseEvent] = [
            BaseEvent(
                event_type="HumanReviewCompleted",
                payload={
                    "application_id": command.application_id,
                    "reviewer_id": command.reviewer_id,
                    "override": command.override,
                    "final_decision": final_decision,
                    "override_reason": command.override_reason,
                },
                metadata=_metadata(correlation_id, actor_id=command.reviewer_id),
            )
        ]

        if final_decision == "APPROVE":
            if command.approved_amount_usd is None:
                raise DomainError("approved_amount_usd is required for APPROVE decision.")
            loan.compliance_status = compliance.status
            loan.validate_application_approval(command.approved_amount_usd)
            decided_events.append(
                BaseEvent(
                    event_type="ApplicationApproved",
                    payload={
                        "application_id": command.application_id,
                        "approved_amount_usd": command.approved_amount_usd,
                        "interest_rate": command.interest_rate,
                        "conditions": command.conditions,
                        "approved_by": command.reviewer_id,
                        "effective_date": command.effective_date,
                    },
                    metadata=_metadata(correlation_id, actor_id=command.reviewer_id),
                )
            )
        else:
            decided_events.append(
                BaseEvent(
                    event_type="ApplicationDeclined",
                    payload={
                        "application_id": command.application_id,
                        "decline_reasons": command.decline_reasons,
                        "declined_by": command.reviewer_id,
                        "adverse_action_notice_required": True,
                    },
                    metadata=_metadata(correlation_id, actor_id=command.reviewer_id),
                )
            )

        # 4) Append
        return await self.store.append(
            stream_id=loan_stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
        )

    async def handle_run_integrity_check(
        self,
        command: RunIntegrityCheckCommand,
    ) -> AppendResult:
        # 1) Load
        stream_id = _audit_stream_id(command.entity_type, command.entity_id)
        events = await self.store.load_stream(stream_id)
        audit = AuditLedgerAggregate.load(events)

        # 2) Validate
        if command.role.lower() != "compliance":
            raise DomainError("Only compliance role can run integrity checks.")
        correlation_id = command.correlation_id or _new_correlation_id()
        metadata = _metadata(
            correlation_id=correlation_id,
            causation_id=command.causation_id,
            actor_id="compliance-service",
        )
        payload = {
            "entity_id": command.entity_id,
            "check_timestamp": datetime.now(UTC).isoformat(),
            "events_verified_count": command.events_verified_count,
            "integrity_hash": command.integrity_hash,
            "previous_hash": command.previous_hash,
        }
        audit.validate_new_integrity_event(payload=payload, metadata=metadata)

        # 3) Decide
        decided_events = [
            BaseEvent(
                event_type="AuditIntegrityCheckRun",
                payload=payload,
                metadata=metadata,
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="AuditLedger",
            events=decided_events,
            expected_version=audit.version,
            stream_metadata={
                "entity_type": command.entity_type,
                "entity_id": command.entity_id,
            },
        )

    async def _load_contributing_agent_events(
        self,
        contributing_agent_sessions: list[str],
    ) -> list[StoredEvent]:
        events: list[StoredEvent] = []
        for stream_id in contributing_agent_sessions:
            stream_events = await self.store.load_stream(stream_id)
            events.extend(stream_events)
        return events


def _loan_stream_id(application_id: str) -> str:
    return f"loan-{application_id}"


def _agent_stream_id(agent_id: str, session_id: str) -> str:
    return f"agent-{agent_id}-{session_id}"


def _compliance_stream_id(application_id: str) -> str:
    return f"compliance-{application_id}"


def _audit_stream_id(entity_type: str, entity_id: str) -> str:
    return f"audit-{entity_type}-{entity_id}"


def _new_correlation_id() -> str:
    return str(uuid4())


def _metadata(
    correlation_id: str,
    causation_id: str | None = None,
    actor_id: str | None = None,
) -> dict[str, str]:
    metadata: dict[str, str] = {"correlation_id": correlation_id}
    if causation_id:
        metadata["causation_id"] = causation_id
    if actor_id:
        metadata["actor_id"] = actor_id
    return metadata


def _extract_assessed_max_limit(events: list[StoredEvent]) -> float | None:
    limits: list[float] = []
    for event in events:
        if event.event_type != "CreditAnalysisCompleted":
            continue
        recommended = event.payload.get("recommended_limit_usd")
        if recommended is None:
            continue
        limits.append(float(recommended))
    if not limits:
        return None
    return max(limits)

