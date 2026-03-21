from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.audit_ledger import AuditLedgerAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import LoanApplicationAggregate, LoanStatus
from src.event_store import EventStore
from src.models.events import AppendResult, BaseEvent, DomainError, StoredEvent
from src.refinery.pipeline import extract_financial_facts


@dataclass(slots=True)
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime
    document_path: str | None = None
    process_documents_after_submit: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass(slots=True)
class StartAgentSessionCommand:
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str
    correlation_id: str | None = None
    causation_id: str | None = None


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
    causation_id: str | None = None


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
    causation_id: str | None = None


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
    causation_id: str | None = None


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
    causation_id: str | None = None


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
    causation_id: str | None = None


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
        if command.process_documents_after_submit and not command.document_path:
            raise DomainError(
                "process_documents_after_submit requires document_path."
            )
        if command.document_path:
            doc_path = Path(command.document_path)
            if not doc_path.exists():
                raise DomainError(f"document_path does not exist: {command.document_path}")
            if not doc_path.is_file():
                raise DomainError(f"document_path is not a file: {command.document_path}")

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        causation_id = command.causation_id
        decided_events: list[BaseEvent] = [
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.applicant_id,
                ),
            )
        ]
        if not command.process_documents_after_submit:
            decided_events.append(
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
                        causation_id=causation_id,
                        actor_id=command.applicant_id,
                    ),
                )
            )

        # 4) Append
        submit_result = await self.store.append(
            stream_id=stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
            stream_metadata={"application_id": command.application_id},
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        if not command.process_documents_after_submit:
            return submit_result

        package_result = await self._append_document_package_events(
            application_id=command.application_id,
            document_path=command.document_path or "",
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        package_causation = (
            str(package_result.events[-1].event_id) if package_result.events else None
        )
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="LoanApplication",
            events=[
                BaseEvent(
                    event_type="CreditAnalysisRequested",
                    payload={
                        "application_id": command.application_id,
                        "assigned_agent_id": "credit-analysis-router",
                        "requested_at": datetime.now(UTC).isoformat(),
                        "priority": "normal",
                        "source": "document_package_ready",
                        "docpkg_stream_id": _docpkg_stream_id(command.application_id),
                    },
                    metadata=_metadata(
                        correlation_id,
                        causation_id=package_causation,
                        actor_id="document-processing-agent",
                    ),
                )
            ],
            expected_version=submit_result.new_stream_version,
            correlation_id=correlation_id,
            causation_id=package_causation or causation_id,
        )

    async def _append_document_package_events(
        self,
        *,
        application_id: str,
        document_path: str,
        correlation_id: str,
        causation_id: str | None,
    ) -> AppendResult:
        doc_stream_id = _docpkg_stream_id(application_id)
        current_doc_events = await self.store.load_stream(doc_stream_id)
        current_version = len(current_doc_events)

        facts = extract_financial_facts(document_path)
        critical_fields = [
            "total_revenue",
            "net_income",
            "ebitda",
            "total_assets",
            "total_liabilities",
        ]
        field_confidence = {
            field: 1.0 if facts.get(field) is not None else 0.0 for field in critical_fields
        }
        critical_missing_fields = [
            field for field, confidence in field_confidence.items() if confidence == 0.0
        ]
        extraction_notes = [field for field in critical_missing_fields]
        confidence_values = list(field_confidence.values())
        overall_confidence = (
            sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
        )

        is_coherent = True
        anomalies: list[str] = []
        total_assets = facts.get("total_assets")
        total_liabilities = facts.get("total_liabilities")
        total_revenue = facts.get("total_revenue")
        net_income = facts.get("net_income")
        if (
            total_assets is not None
            and total_liabilities is not None
            and total_assets < total_liabilities
        ):
            is_coherent = False
            anomalies.append("assets_below_liabilities")
        if (
            total_revenue is not None
            and net_income is not None
            and abs(net_income) > total_revenue * 1.5
        ):
            anomalies.append("net_income_implausible_relative_to_revenue")

        extension = Path(document_path).suffix.lower().lstrip(".") or "unknown"
        now = datetime.now(UTC).isoformat()
        events: list[BaseEvent] = []
        if current_version == 0:
            events.append(
                BaseEvent(
                    event_type="PackageCreated",
                    payload={
                        "application_id": application_id,
                        "package_id": application_id,
                        "created_at": now,
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                )
            )
        events.extend(
            [
                BaseEvent(
                    event_type="DocumentAdded",
                    payload={
                        "application_id": application_id,
                        "document_path": document_path,
                        "document_type": extension,
                        "added_at": now,
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
                BaseEvent(
                    event_type="DocumentFormatValidated",
                    payload={
                        "application_id": application_id,
                        "document_path": document_path,
                        "format": extension,
                        "is_supported": True,
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
                BaseEvent(
                    event_type="ExtractionStarted",
                    payload={
                        "application_id": application_id,
                        "document_path": document_path,
                        "started_at": now,
                        "pipeline": "document_refinery",
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
                BaseEvent(
                    event_type="ExtractionCompleted",
                    payload={
                        "application_id": application_id,
                        "document_path": document_path,
                        "facts": facts,
                        "field_confidence": field_confidence,
                        "extraction_notes": extraction_notes,
                        "completed_at": now,
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
                BaseEvent(
                    event_type="QualityAssessmentCompleted",
                    payload={
                        "application_id": application_id,
                        "overall_confidence": round(overall_confidence, 3),
                        "is_coherent": is_coherent,
                        "anomalies": anomalies,
                        "critical_missing_fields": critical_missing_fields,
                        "reextraction_recommended": len(critical_missing_fields) > 0,
                        "auditor_notes": "Automated quality assessment completed.",
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
                BaseEvent(
                    event_type="PackageReadyForAnalysis",
                    payload={
                        "application_id": application_id,
                        "package_id": application_id,
                        "ready_at": now,
                    },
                    metadata=_metadata(correlation_id, actor_id="document-processing-agent"),
                ),
            ]
        )

        return await self.store.append(
            stream_id=doc_stream_id,
            aggregate_type="DocumentPackage",
            events=events,
            expected_version=current_version,
            stream_metadata={"application_id": application_id},
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        causation_id = command.causation_id
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.agent_id,
                ),
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
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        loan.ensure_can_record_agent_analysis(command.application_id)
        session.validate_credit_analysis_submission(
            model_version=command.model_version,
            confidence_score=command.confidence_score,
            recommended_limit_usd=command.recommended_limit_usd,
            analysis_duration_ms=command.analysis_duration_ms,
            input_data_hash=command.input_data_hash,
        )

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        causation_id = command.causation_id
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.agent_id,
                ),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=session_stream_id,
            aggregate_type="AgentSession",
            events=decided_events,
            expected_version=session.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        loan.ensure_can_record_agent_analysis(command.application_id)
        session.validate_fraud_screening_submission(
            screening_model_version=command.screening_model_version,
            fraud_score=command.fraud_score,
            input_data_hash=command.input_data_hash,
        )

        # 3) Decide
        correlation_id = command.correlation_id or _new_correlation_id()
        causation_id = command.causation_id
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.agent_id,
                ),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=session_stream_id,
            aggregate_type="AgentSession",
            events=decided_events,
            expected_version=session.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        causation_id = command.causation_id
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
                    metadata=_metadata(
                        correlation_id,
                        causation_id=causation_id,
                        actor_id="compliance-engine",
                    ),
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
                    metadata=_metadata(
                        correlation_id,
                        causation_id=causation_id,
                        actor_id="compliance-engine",
                    ),
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
                    metadata=_metadata(
                        correlation_id,
                        causation_id=causation_id,
                        actor_id="compliance-engine",
                    ),
                )
            )

        # 4) Append
        return await self.store.append(
            stream_id=stream_id,
            aggregate_type="ComplianceRecord",
            events=decided_events,
            expected_version=compliance.version,
            stream_metadata={"application_id": command.application_id},
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        causation_id = command.causation_id
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.orchestrator_agent_id,
                ),
            )
        ]

        # 4) Append
        return await self.store.append(
            stream_id=loan_stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        causation_id = command.causation_id
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
                metadata=_metadata(
                    correlation_id,
                    causation_id=causation_id,
                    actor_id=command.reviewer_id,
                ),
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
                    metadata=_metadata(
                        correlation_id,
                        causation_id=causation_id,
                        actor_id=command.reviewer_id,
                    ),
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
                    metadata=_metadata(
                        correlation_id,
                        causation_id=causation_id,
                        actor_id=command.reviewer_id,
                    ),
                )
            )

        # 4) Append
        return await self.store.append(
            stream_id=loan_stream_id,
            aggregate_type="LoanApplication",
            events=decided_events,
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
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
        if command.role.lower() not in {"compliance", "admin"}:
            raise DomainError("Only compliance or admin role can run integrity checks.")
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
            correlation_id=correlation_id,
            causation_id=command.causation_id,
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


def _docpkg_stream_id(application_id: str) -> str:
    return f"docpkg-{application_id}"


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
