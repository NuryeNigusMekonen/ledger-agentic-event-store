from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.commands.handlers import (
    ComplianceCheckCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    RunIntegrityCheckCommand,
    StartAgentSessionCommand,
    SubmitApplicationCommand,
    WriteCommandHandlers,
)
from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check
from src.models.events import DomainError, OptimisticConcurrencyError


class SubmitApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    applicant_id: str
    requested_amount_usd: float = Field(gt=0)
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime
    document_path: str | None = None
    process_documents_after_submit: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None


class StartAgentSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int = Field(ge=0)
    context_token_count: int = Field(gt=0)
    model_version: str
    correlation_id: str | None = None
    causation_id: str | None = None


class RecordCreditAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    risk_tier: str
    recommended_limit_usd: float = Field(gt=0)
    analysis_duration_ms: int = Field(ge=0)
    input_data_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None


class RecordFraudScreeningInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    agent_id: str
    session_id: str
    fraud_score: float = Field(ge=0.0, le=1.0)
    anomaly_flags: list[str]
    screening_model_version: str
    input_data_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None


class RecordComplianceCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    regulation_set_version: str
    rule_id: str
    rule_version: str
    passed: bool
    checks_required: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    remediation_required: bool = False
    correlation_id: str | None = None
    causation_id: str | None = None


class GenerateDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    decision_basis_summary: str
    contributing_agent_sessions: list[str]
    model_versions: dict[str, str]
    correlation_id: str | None = None
    causation_id: str | None = None


class RecordHumanReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    reviewer_id: str
    override: bool = False
    final_decision: str
    override_reason: str | None = None
    approved_amount_usd: float | None = None
    interest_rate: float | None = None
    conditions: list[str] = Field(default_factory=list)
    effective_date: str | None = None
    decline_reasons: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    causation_id: str | None = None


class RunIntegrityCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    entity_id: str
    role: str
    correlation_id: str | None = None
    causation_id: str | None = None


class LedgerMCPTools:
    def __init__(
        self,
        store: EventStore,
        handlers: WriteCommandHandlers,
        after_write: callable | None = None,
    ) -> None:
        self.store = store
        self.handlers = handlers
        self.after_write = after_write
        self._integrity_rate_limit: dict[str, datetime] = {}

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "submit_application",
                "description": (
                    "Create ApplicationSubmitted event. Precondition: application_id must not "
                    "already exist; duplicate IDs return PreconditionFailed. Optional: provide "
                    "document_path + process_documents_after_submit=true to run refinery "
                    "extraction, append docpkg lifecycle events, and then trigger "
                    "CreditAnalysisRequested."
                ),
                "input_schema": SubmitApplicationInput.model_json_schema(),
            },
            {
                "name": "start_agent_session",
                "description": (
                    "Create AgentContextLoaded event. Precondition: must be called before "
                    "record_credit_analysis or record_fraud_screening for the same session."
                ),
                "input_schema": StartAgentSessionInput.model_json_schema(),
            },
            {
                "name": "record_credit_analysis",
                "description": (
                    "Create CreditAnalysisCompleted event. Precondition: active agent session with "
                    "context loaded and matching model_version."
                ),
                "input_schema": RecordCreditAnalysisInput.model_json_schema(),
            },
            {
                "name": "record_fraud_screening",
                "description": (
                    "Create FraudScreeningCompleted event. Precondition: active agent session with "
                    "context loaded and 0.0 <= fraud_score <= 1.0."
                ),
                "input_schema": RecordFraudScreeningInput.model_json_schema(),
            },
            {
                "name": "record_compliance_check",
                "description": (
                    "Create ComplianceRulePassed or ComplianceRuleFailed. Precondition: rule must "
                    "belong to active regulation set for initialized streams."
                ),
                "input_schema": RecordComplianceCheckInput.model_json_schema(),
            },
            {
                "name": "generate_decision",
                "description": (
                    "Create DecisionGenerated event. Preconditions: required analyses present, "
                    "compliance not pending, and confidence_score >= 0.50."
                ),
                "input_schema": GenerateDecisionInput.model_json_schema(),
            },
            {
                "name": "record_human_review",
                "description": (
                    "Create HumanReviewCompleted and final approval/decline event. Precondition: "
                    "if override=true then override_reason is required."
                ),
                "input_schema": RecordHumanReviewInput.model_json_schema(),
            },
            {
                "name": "run_integrity_check",
                "description": (
                    "Run SHA-256 audit chain verification and append AuditIntegrityCheckRun. "
                    "Precondition: role must be compliance or admin; rate limit 1/minute/entity."
                ),
                "input_schema": RunIntegrityCheckInput.model_json_schema(),
            },
        ]

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        dispatch = {
            "submit_application": self.submit_application,
            "start_agent_session": self.start_agent_session,
            "record_credit_analysis": self.record_credit_analysis,
            "record_fraud_screening": self.record_fraud_screening,
            "record_compliance_check": self.record_compliance_check,
            "generate_decision": self.generate_decision,
            "record_human_review": self.record_human_review,
            "run_integrity_check": self.run_integrity_check,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            return _error(
                error_type="UnknownTool",
                message=f"Unknown tool '{tool_name}'.",
                suggested_action="use_list_tools",
            )
        try:
            return await handler(arguments)
        except ValidationError as exc:
            return _error(
                error_type="ValidationError",
                message="Input schema validation failed.",
                suggested_action="fix_input_and_retry",
                details=exc.errors(),
            )
        except OptimisticConcurrencyError as exc:
            return _error(
                error_type="OptimisticConcurrencyError",
                message=str(exc),
                suggested_action=exc.suggested_action,
                details={
                    "stream_id": exc.stream_id,
                    "expected_version": exc.expected_version,
                    "actual_version": exc.actual_version,
                },
            )
        except DomainError as exc:
            return _error(
                error_type="DomainError",
                message=str(exc),
                suggested_action="review_preconditions_and_retry",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return _error(
                error_type="InternalError",
                message=f"Unexpected tool failure: {exc}",
                suggested_action="inspect_logs_and_retry",
            )

    async def submit_application(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = SubmitApplicationInput.model_validate(arguments)
        stream_id = f"loan-{params.application_id}"

        if await self.store.stream_version(stream_id) > 0:
            return _error(
                error_type="PreconditionFailed",
                message=f"Application '{params.application_id}' already exists.",
                suggested_action="use_unique_application_id",
                details={"stream_id": stream_id},
            )

        result = await self.handlers.handle_submit_application(
            SubmitApplicationCommand(
                application_id=params.application_id,
                applicant_id=params.applicant_id,
                requested_amount_usd=params.requested_amount_usd,
                loan_purpose=params.loan_purpose,
                submission_channel=params.submission_channel,
                submitted_at=params.submitted_at,
                document_path=params.document_path,
                process_documents_after_submit=params.process_documents_after_submit,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "stream_id": result.stream_id,
                "initial_version": result.new_stream_version,
            },
        }

    async def start_agent_session(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = StartAgentSessionInput.model_validate(arguments)
        result = await self.handlers.handle_start_agent_session(
            StartAgentSessionCommand(
                agent_id=params.agent_id,
                session_id=params.session_id,
                context_source=params.context_source,
                event_replay_from_position=params.event_replay_from_position,
                context_token_count=params.context_token_count,
                model_version=params.model_version,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "session_id": params.session_id,
                "context_position": result.new_stream_version,
            },
        }

    async def record_credit_analysis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = RecordCreditAnalysisInput.model_validate(arguments)
        session_stream = f"agent-{params.agent_id}-{params.session_id}"
        session = AgentSessionAggregate.load(await self.store.load_stream(session_stream))

        if not session.context_loaded:
            return _error(
                error_type="PreconditionFailed",
                message="No active session context. Call start_agent_session first.",
                suggested_action="call_start_agent_session_then_retry",
                details={"stream_id": session_stream},
            )

        result = await self.handlers.handle_credit_analysis_completed(
            CreditAnalysisCompletedCommand(
                application_id=params.application_id,
                agent_id=params.agent_id,
                session_id=params.session_id,
                model_version=params.model_version,
                confidence_score=params.confidence_score,
                risk_tier=params.risk_tier,
                recommended_limit_usd=params.recommended_limit_usd,
                analysis_duration_ms=params.analysis_duration_ms,
                input_data_hash=params.input_data_hash,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "event_id": str(result.events[-1].event_id),
                "new_stream_version": result.new_stream_version,
            },
        }

    async def record_fraud_screening(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = RecordFraudScreeningInput.model_validate(arguments)
        session_stream = f"agent-{params.agent_id}-{params.session_id}"
        session = AgentSessionAggregate.load(await self.store.load_stream(session_stream))
        if not session.context_loaded:
            return _error(
                error_type="PreconditionFailed",
                message="No active session context. Call start_agent_session first.",
                suggested_action="call_start_agent_session_then_retry",
                details={"stream_id": session_stream},
            )

        result = await self.handlers.handle_fraud_screening_completed(
            FraudScreeningCompletedCommand(
                application_id=params.application_id,
                agent_id=params.agent_id,
                session_id=params.session_id,
                fraud_score=params.fraud_score,
                anomaly_flags=params.anomaly_flags,
                screening_model_version=params.screening_model_version,
                input_data_hash=params.input_data_hash,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "event_id": str(result.events[-1].event_id),
                "new_stream_version": result.new_stream_version,
            },
        }

    async def record_compliance_check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = RecordComplianceCheckInput.model_validate(arguments)
        stream_id = f"compliance-{params.application_id}"
        compliance = ComplianceRecordAggregate.load(await self.store.load_stream(stream_id))
        if compliance.status != "NOT_STARTED" and params.rule_id not in compliance.mandatory_checks:
            return _error(
                error_type="PreconditionFailed",
                message=f"Rule '{params.rule_id}' is not in active regulation check set.",
                suggested_action="use_valid_rule_id_for_regulation_set",
                details={"mandatory_checks": sorted(compliance.mandatory_checks)},
            )

        result = await self.handlers.handle_compliance_check(
            ComplianceCheckCommand(
                application_id=params.application_id,
                regulation_set_version=params.regulation_set_version,
                rule_id=params.rule_id,
                rule_version=params.rule_version,
                passed=params.passed,
                checks_required=params.checks_required,
                failure_reason=params.failure_reason,
                remediation_required=params.remediation_required,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        latest_event = result.events[-1]
        return {
            "ok": True,
            "result": {
                "check_id": str(latest_event.event_id),
                "compliance_status": latest_event.event_type,
            },
        }

    async def generate_decision(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = GenerateDecisionInput.model_validate(arguments)

        if params.confidence_score < 0.50:
            return _error(
                error_type="PreconditionFailed",
                message="confidence_score must be >= 0.50 for generate_decision.",
                suggested_action="raise_confidence_or_use_REFER",
            )

        has_credit = False
        has_fraud = False
        for stream_id in params.contributing_agent_sessions:
            for event in await self.store.load_stream(stream_id):
                if event.event_type == "CreditAnalysisCompleted":
                    has_credit = True
                if event.event_type == "FraudScreeningCompleted":
                    has_fraud = True
        if not has_credit or not has_fraud:
            return _error(
                error_type="PreconditionFailed",
                message="Required analyses are missing (credit and fraud are required).",
                suggested_action="record_missing_analyses_then_retry",
                details={"has_credit": has_credit, "has_fraud": has_fraud},
            )

        result = await self.handlers.handle_generate_decision(
            GenerateDecisionCommand(
                application_id=params.application_id,
                orchestrator_agent_id=params.orchestrator_agent_id,
                recommendation=params.recommendation,
                confidence_score=params.confidence_score,
                decision_basis_summary=params.decision_basis_summary,
                contributing_agent_sessions=params.contributing_agent_sessions,
                model_versions=params.model_versions,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "decision_id": str(result.events[-1].event_id),
                "recommendation": params.recommendation.upper(),
            },
        }

    async def record_human_review(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = RecordHumanReviewInput.model_validate(arguments)
        if params.override and not params.override_reason:
            return _error(
                error_type="PreconditionFailed",
                message="override_reason is required when override=true.",
                suggested_action="provide_override_reason_and_retry",
            )

        result = await self.handlers.handle_human_review_completed(
            HumanReviewCompletedCommand(
                application_id=params.application_id,
                reviewer_id=params.reviewer_id,
                override=params.override,
                final_decision=params.final_decision,
                override_reason=params.override_reason,
                approved_amount_usd=params.approved_amount_usd,
                interest_rate=params.interest_rate,
                conditions=params.conditions,
                effective_date=params.effective_date,
                decline_reasons=params.decline_reasons,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "final_decision": params.final_decision.upper(),
                "application_state": result.events[-1].event_type,
            },
        }

    async def run_integrity_check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = RunIntegrityCheckInput.model_validate(arguments)
        if params.role.lower() not in {"compliance", "admin"}:
            return _error(
                error_type="AuthorizationError",
                message="run_integrity_check requires compliance or admin role.",
                suggested_action="use_admin_or_compliance_credentials",
            )

        entity_key = f"{params.entity_type}:{params.entity_id}"
        now = datetime.now(UTC)
        previous_time = self._integrity_rate_limit.get(entity_key)
        if previous_time and now - previous_time < timedelta(minutes=1):
            return _error(
                error_type="RateLimitExceeded",
                message="run_integrity_check is limited to 1 call/minute per entity.",
                suggested_action="retry_after_cooldown",
            )

        target_stream = _entity_stream_id(params.entity_type, params.entity_id)
        check_result = await run_integrity_check(self.store, target_stream)

        audit_stream = f"audit-{params.entity_type}-{params.entity_id}"
        audit_events = await self.store.load_stream(audit_stream)
        previous_hash = None
        for event in reversed(audit_events):
            if event.event_type == "AuditIntegrityCheckRun":
                previous_hash = event.payload.get("integrity_hash")
                break

        append_result = await self.handlers.handle_run_integrity_check(
            RunIntegrityCheckCommand(
                entity_type=params.entity_type,
                entity_id=params.entity_id,
                events_verified_count=check_result.events_verified_count,
                integrity_hash=check_result.final_hash,
                previous_hash=previous_hash,
                role=params.role,
                correlation_id=params.correlation_id,
                causation_id=params.causation_id,
            )
        )
        self._integrity_rate_limit[entity_key] = now
        await self._after_write()
        return {
            "ok": True,
            "result": {
                "check_result": "valid" if check_result.chain_valid else "invalid",
                "chain_valid": check_result.chain_valid,
                "events_verified_count": check_result.events_verified_count,
                "violation_count": len(check_result.violations),
                "audit_event_id": str(append_result.events[-1].event_id),
            },
        }

    async def _after_write(self) -> None:
        if self.after_write is None:
            return
        await self.after_write()


def _entity_stream_id(entity_type: str, entity_id: str) -> str:
    normalized = entity_type.lower()
    if normalized in {"application", "loan"}:
        return f"loan-{entity_id}"
    if normalized in {"agent_session", "agent"}:
        return entity_id if entity_id.startswith("agent-") else f"agent-{entity_id}"
    if normalized in {"compliance", "compliance_record"}:
        return f"compliance-{entity_id}"
    if normalized in {"audit", "audit_ledger"}:
        return f"audit-{normalized}-{entity_id}"
    return entity_id


def _error(
    *,
    error_type: str,
    message: str,
    suggested_action: str,
    details: Any | None = None,
) -> dict[str, Any]:
    error = {
        "error_type": error_type,
        "message": message,
        "suggested_action": suggested_action,
    }
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}
