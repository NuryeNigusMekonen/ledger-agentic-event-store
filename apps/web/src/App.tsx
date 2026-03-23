import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import {
  bootstrapDemo,
  clearStoredToken,
  fetchAgentPerformance,
  fetchAgentSession,
  fetchApplication,
  fetchApplicationAuditTrail,
  fetchApplicationEvents,
  fetchApplications,
  fetchResources,
  fetchApplicationStates,
  fetchAuthAudit,
  fetchCompliance,
  fetchLedgerHealth,
  fetchMe,
  fetchRecentEvents,
  fetchResourceByUri,
  fetchTools,
  LedgerApiError,
  login,
  runCommand,
  type AuthAuditRow,
  type MeResponse
} from "./api";
import type {
  AgentPerformance,
  AgentSessionReplay,
  AppStateCount,
  ApplicationSummary,
  ComplianceTimelineEvent,
  ComplianceView,
  LedgerHealth,
  RecentEvent,
  ResourceDefinition,
  ToolDefinition
} from "./types";
import apexLogo from "../logo/logo.png";

const DEFAULT_COMMAND_PAYLOAD: Record<string, Record<string, unknown>> = {
  submit_application: {
    application_id: "app-addis-001",
    applicant_id: "et-borrower-001",
    requested_amount_usd: 1250000,
    loan_purpose: "working_capital",
    submission_channel: "addis-branch",
    submitted_at: new Date().toISOString()
  },
  run_integrity_check: {
    entity_type: "application",
    entity_id: "app-addis-001",
    role: "compliance"
  }
};

const OPERATING_FABRIC = [
  {
    name: "CreditAnalysis",
    role: "Credit risk evaluation",
    eventTypes: ["CreditAnalysisRequested", "CreditAnalysisCompleted"]
  },
  {
    name: "FraudDetection",
    role: "Fraud and anomaly screening",
    eventTypes: ["FraudScreeningRequested", "FraudScreeningCompleted"]
  },
  {
    name: "ComplianceAgent",
    role: "Regulation and policy checks",
    eventTypes: [
      "ComplianceCheckRequested",
      "ComplianceRulePassed",
      "ComplianceRuleFailed",
      "ComplianceCheckCompleted"
    ]
  },
  {
    name: "DecisionOrchestrator",
    role: "Machine recommendation synthesis",
    eventTypes: ["DecisionRequested", "DecisionGenerated", "HumanReviewRequested"]
  }
] as const;

type Tone = "ok" | "warning" | "critical" | "neutral" | "info";
type WorkspaceMode = "operations" | "audit" | "system";
type TimelinePhase = "Intake" | "Analysis" | "Compliance" | "Decision" | "Final Outcome";
type EventTimeRange = "all" | "1h" | "24h" | "7d" | "30d";

type LineageItem = {
  key: string;
  globalPosition: number;
  recordedAt: string;
  eventType: string;
  summary: string;
  actor: string;
  lane: string;
  checkpoint: string;
  tone: Tone;
  streamId: string;
  correlationId: string | null;
  causationId: string | null;
};

type ProjectionAssessment = {
  tone: Tone;
  statusLabel: string;
  severityLabel: string;
  explanation: string;
  isSynced: boolean;
};

type EvidencePanelKey = "integrity" | "snapshot" | "lineage" | "activity" | "compliance";

type ResourceQueryHistoryEntry = {
  uri: string;
  preview: string;
  loadedAt: string;
};

type TimelineGroup = {
  key: string;
  phase: TimelinePhase;
  items: LineageItem[];
};

type ExpectedEventGroup = {
  label: string;
  eventTypes: string[];
};

const EVIDENCE_PANEL_KEYS: EvidencePanelKey[] = [
  "integrity",
  "snapshot",
  "lineage",
  "activity",
  "compliance"
];

const EXPECTED_EVENT_GROUPS: ExpectedEventGroup[] = [
  {
    label: "Core Loan Decisioning",
    eventTypes: [
      "ApplicationSubmitted",
      "CreditAnalysisRequested",
      "AgentContextLoaded",
      "CreditAnalysisCompleted",
      "FraudScreeningCompleted",
      "ComplianceCheckRequested",
      "ComplianceRulePassed",
      "ComplianceRuleFailed",
      "DecisionGenerated",
      "HumanReviewCompleted",
      "ApplicationApproved",
      "ApplicationDeclined",
      "AuditIntegrityCheckRun"
    ]
  },
  {
    label: "Lifecycle / Request",
    eventTypes: [
      "DocumentUploadRequested",
      "DocumentUploaded",
      "FraudScreeningRequested",
      "DecisionRequested",
      "HumanReviewRequested",
      "ComplianceCheckCompleted",
      "AgentSessionStarted",
      "AgentSessionRecovered"
    ]
  },
  {
    label: "Document Processing",
    eventTypes: [
      "PackageCreated",
      "DocumentAdded",
      "DocumentFormatValidated",
      "ExtractionStarted",
      "ExtractionCompleted",
      "QualityAssessmentCompleted",
      "PackageReadyForAnalysis"
    ]
  }
];

const EXPECTED_EVENT_TYPES = Array.from(
  new Set(EXPECTED_EVENT_GROUPS.flatMap((group) => group.eventTypes))
);

function prettyDate(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function prettyDateCompact(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  });
}

function formatMoney(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "Pending";
  }
  return new Intl.NumberFormat("en-ET", {
    style: "currency",
    currency: "ETB",
    maximumFractionDigits: 0
  }).format(value);
}

function shortId(value: string | null | undefined, edge = 6): string {
  if (!value) {
    return "Unavailable";
  }
  if (value.length <= edge * 2 + 3) {
    return value;
  }
  return `${value.slice(0, edge)}...${value.slice(-edge)}`;
}

function titleize(value: string | null | undefined): string {
  if (!value) {
    return "Not set";
  }
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function toTone(value: string | null | undefined): Tone {
  const normalized = String(value ?? "").toLowerCase();
  if (
    normalized.includes("critical") ||
    normalized.includes("failed") ||
    normalized.includes("decline") ||
    normalized.includes("denied")
  ) {
    return "critical";
  }
  if (
    normalized.includes("warning") ||
    normalized.includes("pending") ||
    normalized.includes("lag") ||
    normalized.includes("review") ||
    normalized.includes("refer")
  ) {
    return "warning";
  }
  if (
    normalized.includes("live") ||
    normalized.includes("progress") ||
    normalized.includes("requested") ||
    normalized.includes("running") ||
    normalized.includes("sync")
  ) {
    return "info";
  }
  if (
    normalized.includes("ok") ||
    normalized.includes("healthy") ||
    normalized.includes("cleared") ||
    normalized.includes("approve") ||
    normalized.includes("verified")
  ) {
    return "ok";
  }
  return "neutral";
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((entry): entry is string => typeof entry === "string");
}

function toDateTimeLocalValue(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }

  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = `${date.getHours()}`.padStart(2, "0");
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function fromDateTimeLocalValue(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toISOString();
}

function formatLagMs(value: number): string {
  if (!Number.isFinite(value)) {
    return "n/a";
  }
  return `${Math.round(value).toLocaleString()} ms`;
}

function previewJson(value: unknown): string {
  const serialized =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return serialized.length > 160 ? `${serialized.slice(0, 157)}...` : serialized;
}

function eventPhase(eventType: string): TimelinePhase {
  if (eventType === "ApplicationSubmitted" || eventType.startsWith("Document")) {
    return "Intake";
  }
  if (
    eventType.startsWith("Credit") ||
    eventType.startsWith("Fraud") ||
    eventType.startsWith("AgentSession") ||
    eventType === "AgentContextLoaded"
  ) {
    return "Analysis";
  }
  if (eventType.startsWith("Compliance")) {
    return "Compliance";
  }
  if (
    eventType === "DecisionRequested" ||
    eventType === "DecisionGenerated" ||
    eventType === "HumanReviewRequested"
  ) {
    return "Decision";
  }
  return "Final Outcome";
}

function isFinalDecisionEvent(eventType: string): boolean {
  return (
    eventType === "ApplicationApproved" ||
    eventType === "ApplicationDeclined" ||
    eventType === "HumanReviewCompleted"
  );
}

function matchesTimeRange(recordedAt: string, range: EventTimeRange): boolean {
  if (range === "all") {
    return true;
  }

  const eventTime = new Date(recordedAt).getTime();
  if (Number.isNaN(eventTime)) {
    return false;
  }

  const now = Date.now();
  const durations: Record<Exclude<EventTimeRange, "all">, number> = {
    "1h": 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000
  };

  return now - eventTime <= durations[range];
}

function conciseDescription(value: string | null | undefined): string {
  if (!value) {
    return "No command guidance available.";
  }
  const [firstSentence] = value.split(". ");
  if (!firstSentence) {
    return value;
  }
  return firstSentence.endsWith(".") ? firstSentence : `${firstSentence}.`;
}

function normalizeAuditAction(value: string): string {
  return value
    .replace(/^command:/i, "")
    .replace(/^auth_/i, "auth ")
    .replace(/_/g, " ")
    .trim();
}

function presentAuditAction(value: string): string {
  const normalized = value.toLowerCase().trim();
  if (normalized === "bootstrap_demo") {
    return "Run Governed Lifecycle";
  }
  if (normalized.startsWith("command:")) {
    const command = normalized.replace("command:", "");
    const commandLabels: Record<string, string> = {
      submit_application: "Submit Application",
      start_agent_session: "Start Agent Session",
      record_credit_analysis: "Record Credit Analysis",
      record_fraud_screening: "Record Fraud Screening",
      record_compliance_check: "Record Compliance Check",
      generate_decision: "Generate Decision",
      record_human_review: "Record Human Review",
      run_integrity_check: "Run Integrity Check"
    };
    return commandLabels[command] ?? titleize(normalizeAuditAction(value));
  }
  return titleize(normalizeAuditAction(value));
}

function presentAuditOutcome(row: AuthAuditRow): { label: string; tone: Tone; helper: string } {
  const details = readRecord(row.details);
  const reason = readString(details.reason);
  const errorType = readString(details.error_type);
  const errorMessage = readString(details.message);
  if (row.success) {
    return { label: "Success", tone: "ok", helper: "Authorized action" };
  }
  if (reason === "role_forbidden") {
    return { label: "Policy denied", tone: "warning", helper: "Blocked by role-scoped access" };
  }
  if (reason === "timeout") {
    return { label: "Timed out", tone: "warning", helper: "Command exceeded execution timeout" };
  }
  if (errorType === "PreconditionFailed") {
    return {
      label: "Precondition failed",
      tone: "warning",
      helper: errorMessage ?? "Domain preconditions were not met"
    };
  }
  if (errorType === "ValidationError") {
    return {
      label: "Invalid payload",
      tone: "warning",
      helper: errorMessage ?? "Command input failed validation"
    };
  }
  if (errorType === "DomainError") {
    return {
      label: "Domain blocked",
      tone: "warning",
      helper: errorMessage ?? "Business rule prevented this operation"
    };
  }
  if (errorType) {
    return {
      label: "Failed",
      tone: "critical",
      helper: errorMessage ?? `${titleize(errorType)} returned by command`
    };
  }
  return { label: "Denied", tone: "critical", helper: "Rejected by control policy" };
}

function complianceEvidenceStatus(entry: ComplianceTimelineEvent): { label: string; tone: Tone } {
  if (entry.event_type === "ComplianceCheckRequested") {
    return { label: "Requested", tone: "neutral" };
  }
  if (entry.event_type === "ComplianceRulePassed") {
    return { label: "Passed", tone: "ok" };
  }
  if (entry.event_type === "ComplianceRuleFailed") {
    return { label: "Failed", tone: "critical" };
  }
  return { label: titleize(entry.compliance_status), tone: toTone(entry.compliance_status) };
}

function isAuthFailure(error: unknown): boolean {
  return error instanceof LedgerApiError && (error.status === 401 || error.status === 403);
}

function commandSeed(toolName: string, applicationId: string, role: string | undefined): Record<string, unknown> {
  const seeded = DEFAULT_COMMAND_PAYLOAD[toolName];
  if (seeded) {
    const patched = { ...seeded };
    if (toolName === "run_integrity_check") {
      patched.entity_id = applicationId || String(patched.entity_id);
      patched.role = role ?? patched.role;
    }
    if ("application_id" in patched) {
      patched.application_id = applicationId || String(patched.application_id);
    }
    return patched;
  }

  return {
    application_id: applicationId || "app-addis-001"
  };
}

function eventMatchesApplication(event: RecentEvent, applicationId: string): boolean {
  if (!applicationId) {
    return false;
  }
  const payloadAppId = readString(event.payload.application_id);
  return payloadAppId === applicationId || event.stream_id.includes(applicationId);
}

function extractSessionId(event: RecentEvent): string | null {
  const payloadSession = readString(event.payload.session_id);
  if (payloadSession) {
    return payloadSession;
  }
  const marker = "-session-";
  const markerIndex = event.stream_id.indexOf(marker);
  if (markerIndex < 0) {
    return null;
  }
  return event.stream_id.slice(markerIndex + 1);
}

function actorForEvent(eventType: string, payload: Record<string, unknown>, metadata: Record<string, unknown>): string {
  const actorId = readString(metadata.actor_id);
  if (actorId) {
    return actorId;
  }
  if (eventType === "DocumentUploadRequested" || eventType === "DocumentUploaded") {
    return readString(payload.requested_by) ?? readString(payload.uploaded_by) ?? "Document Intake";
  }
  if (eventType.startsWith("Compliance")) {
    return "ComplianceAgent";
  }
  if (eventType === "AgentSessionStarted" || eventType === "AgentSessionRecovered" || eventType === "AgentContextLoaded") {
    return readString(payload.agent_id) ?? "Agent Session";
  }
  if (eventType.startsWith("Credit")) {
    return readString(payload.agent_id) ?? readString(payload.assigned_agent_id) ?? "CreditAnalysis";
  }
  if (eventType.startsWith("Fraud")) {
    return readString(payload.agent_id) ?? readString(payload.assigned_agent_id) ?? "FraudDetection";
  }
  if (eventType === "DecisionRequested") {
    return readString(payload.requested_by) ?? "DecisionOrchestrator";
  }
  if (eventType === "DecisionGenerated") {
    return readString(payload.orchestrator_agent_id) ?? "DecisionOrchestrator";
  }
  if (eventType === "HumanReviewRequested") {
    return readString(payload.requested_by) ?? "DecisionOrchestrator";
  }
  if (eventType === "HumanReviewCompleted") {
    return readString(payload.reviewer_id) ?? "Loan Officer";
  }
  if (eventType === "ApplicationApproved") {
    return readString(payload.approved_by) ?? "Loan Officer";
  }
  if (eventType === "ApplicationDeclined") {
    return readString(payload.declined_by) ?? "Loan Officer";
  }
  if (eventType === "ApplicationSubmitted") {
    return "Loan Intake";
  }
  if (eventType === "AuditIntegrityCheckRun") {
    return "Compliance Service";
  }
  return "Ledger Control";
}

function laneForEvent(eventType: string): string {
  if (eventType === "ApplicationSubmitted") {
    return "Intake";
  }
  if (eventType === "DocumentUploadRequested" || eventType === "DocumentUploaded") {
    return "Document Intake";
  }
  if (eventType === "AgentSessionStarted" || eventType === "AgentSessionRecovered" || eventType === "AgentContextLoaded") {
    return "Agent Session";
  }
  if (eventType.startsWith("Credit")) {
    return "CreditAnalysis";
  }
  if (eventType.startsWith("Fraud")) {
    return "FraudDetection";
  }
  if (eventType.startsWith("Compliance")) {
    return "ComplianceAgent";
  }
  if (eventType === "DecisionRequested" || eventType === "DecisionGenerated" || eventType === "HumanReviewRequested") {
    return "DecisionOrchestrator";
  }
  if (eventType === "HumanReviewCompleted") {
    return "Human Review";
  }
  if (eventType === "ApplicationApproved" || eventType === "ApplicationDeclined") {
    return "Final Decision";
  }
  if (eventType === "AuditIntegrityCheckRun") {
    return "Integrity";
  }
  return "Ledger";
}

function checkpointForEvent(eventType: string): string {
  if (eventType === "DecisionRequested" || eventType === "DecisionGenerated") {
    return "Machine checkpoint";
  }
  if (eventType === "HumanReviewRequested") {
    return "Awaiting binding review";
  }
  if (eventType === "HumanReviewCompleted") {
    return "Binding review";
  }
  if (eventType === "ApplicationApproved" || eventType === "ApplicationDeclined") {
    return "Final disposition";
  }
  if (eventType.startsWith("Compliance")) {
    return "Regulatory evidence";
  }
  if (eventType === "AuditIntegrityCheckRun") {
    return "Hash-chain attestation";
  }
  return "Operational event";
}

function describeEvent(eventType: string, payload: Record<string, unknown>): string {
  if (eventType === "ApplicationSubmitted") {
    return `Commercial application submitted for ${formatMoney(readNumber(payload.requested_amount_usd))}.`;
  }
  if (eventType === "DocumentUploadRequested") {
    return "Document upload requested for the submitted application package.";
  }
  if (eventType === "DocumentUploaded") {
    return "Application document package uploaded and ready for downstream processing.";
  }
  if (eventType === "AgentSessionStarted") {
    return `Agent session started with model ${readString(payload.model_version) ?? "unknown"}.`;
  }
  if (eventType === "AgentSessionRecovered") {
    return `Agent session recovered context from ${readString(payload.recovered_from_session_id) ?? "prior session"}.`;
  }
  if (eventType === "AgentContextLoaded") {
    return `Agent session loaded with replay context from ledger position ${readNumber(payload.event_replay_from_position) ?? 0}.`;
  }
  if (eventType === "CreditAnalysisRequested") {
    return "Credit analysis requested after intake and document processing.";
  }
  if (eventType === "FraudScreeningRequested") {
    return "Fraud screening requested from anomaly detection workflow.";
  }
  if (eventType === "CreditAnalysisCompleted") {
    return `Credit analysis produced ${readString(payload.risk_tier) ?? "unclassified"} risk with confidence ${readNumber(payload.confidence_score)?.toFixed(2) ?? "n/a"}.`;
  }
  if (eventType === "FraudScreeningCompleted") {
    return `Fraud screening recorded score ${readNumber(payload.fraud_score)?.toFixed(2) ?? "n/a"} with anomaly review complete.`;
  }
  if (eventType === "ComplianceCheckRequested") {
    return `Compliance checks initiated against regulation set ${readString(payload.regulation_set_version) ?? "current"}.`;
  }
  if (eventType === "ComplianceRulePassed") {
    return `Compliance rule ${readString(payload.rule_id) ?? "unknown"} passed under ${readString(payload.rule_version) ?? "active policy"}.`;
  }
  if (eventType === "ComplianceRuleFailed") {
    return `Compliance rule ${readString(payload.rule_id) ?? "unknown"} failed${readString(payload.failure_reason) ? `: ${readString(payload.failure_reason)}` : "."}`;
  }
  if (eventType === "ComplianceCheckCompleted") {
    return `Compliance check set completed with overall verdict ${titleize(readString(payload.overall_verdict) ?? "pending")}.`;
  }
  if (eventType === "DecisionRequested") {
    return "Decision orchestration requested using credit, fraud, and compliance evidence.";
  }
  if (eventType === "DecisionGenerated") {
    return `DecisionOrchestrator recommended ${readString(payload.recommendation) ?? "review"} with confidence ${readNumber(payload.confidence_score)?.toFixed(2) ?? "n/a"}.`;
  }
  if (eventType === "HumanReviewRequested") {
    return "Human loan officer review requested before final binding decision.";
  }
  if (eventType === "HumanReviewCompleted") {
    return `Human loan officer recorded ${readString(payload.final_decision) ?? "review"} as the binding action${payload.override ? " with override applied." : "."}`;
  }
  if (eventType === "ApplicationApproved") {
    return `Application approved for ${formatMoney(readNumber(payload.approved_amount_usd))}.`;
  }
  if (eventType === "ApplicationDeclined") {
    return "Application declined and adverse action workflow prepared.";
  }
  if (eventType === "AuditIntegrityCheckRun") {
    return `SHA-256 integrity check attested ${readNumber(payload.events_verified_count) ?? 0} events for this entity.`;
  }
  return `${titleize(eventType)} recorded in the immutable ledger.`;
}

function buildLineageItemFromRecent(event: RecentEvent): LineageItem {
  const payload = readRecord(event.payload);
  const metadata = readRecord(event.metadata);
  return {
    key: `${event.global_position}-${event.event_type}`,
    globalPosition: event.global_position,
    recordedAt: event.recorded_at,
    eventType: event.event_type,
    summary: describeEvent(event.event_type, payload),
    actor: actorForEvent(event.event_type, payload, metadata),
    lane: laneForEvent(event.event_type),
    checkpoint: checkpointForEvent(event.event_type),
    tone: toTone(
      readString(payload.final_decision) ??
        readString(payload.recommendation) ??
        readString(payload.compliance_status) ??
        event.event_type
    ),
    streamId: event.stream_id,
    correlationId: readString(metadata.correlation_id),
    causationId: readString(metadata.causation_id)
  };
}

function buildLineageItemFromCompliance(entry: ComplianceTimelineEvent): LineageItem {
  const payload = {
    rule_id: entry.rule_id,
    rule_version: entry.rule_version,
    failure_reason: entry.failure_reason,
    compliance_status: entry.compliance_status
  };

  return {
    key: `${entry.global_position}-${entry.event_type}`,
    globalPosition: entry.global_position,
    recordedAt: entry.recorded_at,
    eventType: entry.event_type,
    summary: describeEvent(entry.event_type, payload),
    actor: "ComplianceAgent",
    lane: "ComplianceAgent",
    checkpoint: "Regulatory evidence",
    tone: toTone(entry.compliance_status),
    streamId: "compliance projection",
    correlationId: null,
    causationId: null
  };
}

function MetricTile(props: {
  label: string;
  value: string;
  detail: string;
  tone?: Tone;
}): ReactNode {
  return (
    <article className={`metric-tile tone-${props.tone ?? "neutral"}`} title={props.detail}>
      <span className="metric-label">{props.label}</span>
      <div className="metric-main">
        <strong className="metric-value">{props.value}</strong>
        <span className="metric-detail">{props.detail}</span>
      </div>
    </article>
  );
}

function StatusBadge(props: { label: string; tone?: Tone }): ReactNode {
  return <span className={`status-badge tone-${props.tone ?? "neutral"}`}>{props.label}</span>;
}

function FocusedRecordSummary(props: {
  application: ApplicationSummary | null;
  onCopyApplicationId: (applicationId: string) => void;
}): ReactNode {
  if (!props.application) {
    return null;
  }

  const application = props.application;

  return (
    <section className="panel focus-banner">
      <div className="focus-banner-main">
        <div className="focus-banner-title">
          <p className="section-kicker">Focused record</p>
          <div className="record-id-row">
            <h2 title={application.application_id}>{shortId(application.application_id, 8)}</h2>
            <button
              type="button"
              className="button subtle copy-id-button"
              onClick={() => props.onCopyApplicationId(application.application_id)}
              title={application.application_id}
            >
              Copy ID
            </button>
          </div>
        </div>
        <div className="focus-banner-status">
          <span className={`focus-status tone-${toTone(application.current_state)}`}>
            {titleize(application.current_state)}
          </span>
        </div>
      </div>
      <div className="focus-banner-grid">
        <div className="focus-inline-item">
          <span>Requested</span>
          <strong>{formatMoney(application.requested_amount_usd)}</strong>
        </div>
        <div className="focus-inline-item">
          <span>Recommendation</span>
          <strong>{titleize(application.decision_recommendation ?? "Pending")}</strong>
        </div>
        <div className="focus-inline-item">
          <span>Compliance</span>
          <strong>{titleize(application.compliance_status ?? "Not started")}</strong>
        </div>
        <div className="focus-inline-item">
          <span>Last</span>
          <strong>{titleize(application.last_event_type)}</strong>
        </div>
      </div>
    </section>
  );
}

function AgentPipelineFlow(props: {
  stages: Array<{
    name: string;
    role: string;
    status: string;
    latestEvent: string;
    latestAt: string;
    tone: Tone;
  }>;
  compact?: boolean;
}): ReactNode {
  return (
    <section className={`panel operating-model ${props.compact ? "operating-model-compact" : ""}`}>
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Agent Workflow</p>
          <h2>Decision pipeline</h2>
        </div>
        {!props.compact ? (
          <p className="section-note">CreditAnalysis through human approval with evidence at every stage.</p>
        ) : null}
      </div>

      <div className="pipeline-flow">
        {props.stages.map((stage, index) => (
          <div className="pipeline-segment" key={stage.name}>
            <article
              className={`pipeline-step tone-${stage.tone} ${stage.name === "HumanReview" ? "pipeline-step-human" : ""}`}
            >
              <div className="fabric-header">
                <strong>{stage.name}</strong>
                <StatusBadge label={stage.status} tone={stage.tone} />
              </div>
              {!props.compact ? <span className="fabric-role">{stage.role}</span> : null}
              <div className="fabric-meta">
                <span>{stage.latestEvent}</span>
                <time>{stage.latestAt}</time>
              </div>
            </article>
            {!props.compact && index < props.stages.length - 1 ? (
              <div className="pipeline-arrow" aria-hidden="true">
                <span>&rarr;</span>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function IntegrityStatusCard(props: {
  title: string;
  status: string;
  tone: Tone;
  verified: number;
  total: number;
  evidenceHash: string;
  verifiedAt: string;
  verifiedWindow: string;
  tamperState: string;
  hasIntegrityCommand: boolean;
  busy: boolean;
  onRunVerification: () => void;
  showEvidenceToggle?: boolean;
  evidenceExpanded?: boolean;
  onToggleEvidence?: () => void;
}): ReactNode {
  const progress = props.total > 0 ? Math.min(100, Math.round((props.verified / props.total) * 100)) : 0;

  return (
    <section className="panel integrity-card">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Integrity Verification</p>
          <h2>{props.title}</h2>
        </div>
        <div className="panel-actions">
          <StatusBadge label={props.status} tone={props.tone} />
          {props.showEvidenceToggle && props.onToggleEvidence ? (
            <button type="button" className="text-button" onClick={props.onToggleEvidence}>
              {props.evidenceExpanded ? "Hide evidence" : "Show evidence"}
            </button>
          ) : null}
        </div>
      </div>

      <div className="integrity-overview">
        <div className="integrity-stat">
          <span>Verified vs total</span>
          <strong>
            {props.verified} / {props.total}
          </strong>
        </div>
        <div className="integrity-stat">
          <span>Latest verification</span>
          <strong>{props.verifiedAt}</strong>
        </div>
        <div className="integrity-stat">
          <span>Evidence hash</span>
          <strong>{props.evidenceHash}</strong>
        </div>
      </div>

      <div className="integrity-progress">
        <div className="integrity-progress-head">
          <span>Verification coverage</span>
          <strong>{progress}%</strong>
        </div>
        <div className="progress-track" aria-hidden="true">
          <span className="progress-fill integrity-progress-fill" style={{ width: `${Math.max(progress, props.verified > 0 ? 8 : 0)}%` }} />
        </div>
        <p className="projection-note">{props.verifiedWindow}</p>
      </div>

      <div className="quick-actions">
        <button className="button" type="button" onClick={props.onRunVerification} disabled={!props.hasIntegrityCommand || props.busy}>
          Run verification
        </button>
      </div>

      {props.evidenceExpanded ? (
        <div className="evidence-inline">
          <span className="meta-label">Tamper state</span>
          <p className="field-note">{props.tamperState}</p>
        </div>
      ) : null}
    </section>
  );
}

function ProjectionHealthPanel(props: {
  rows: Array<{
    name: string;
    lag: {
      checkpoint_position: number;
      latest_position: number;
      events_behind: number;
      lag_ms: number;
      status: string;
      updated_at: string;
    };
    assessment: ProjectionAssessment;
  }>;
}): ReactNode {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Projection Status</p>
          <h2>Sync and lag watchdog</h2>
        </div>
      </div>

      <div className="projection-list">
        {props.rows.length === 0 ? (
          <p className="empty-state">Projection checkpoints have not reported yet.</p>
        ) : (
          props.rows.map(({ name, lag, assessment }) => {
            const ratio = lag.latest_position > 0 ? lag.checkpoint_position / lag.latest_position : 1;
            const errorState =
              assessment.tone === "critical"
                ? "Attention required"
                : assessment.tone === "warning"
                  ? "Warning"
                  : "No reported error";

            return (
              <article className={`projection-row projection-${assessment.tone}`} key={name}>
                <div className="projection-head">
                  <strong>{titleize(name)}</strong>
                  <StatusBadge label={assessment.statusLabel} tone={assessment.tone} />
                </div>
                <div className="projection-grid">
                  <div>
                    <span>Status</span>
                    <strong>{assessment.statusLabel}</strong>
                  </div>
                  <div>
                    <span>Lag</span>
                    <strong>{formatLagMs(lag.lag_ms)}</strong>
                  </div>
                  <div>
                    <span>Severity</span>
                    <strong>{assessment.severityLabel}</strong>
                  </div>
                  <div>
                    <span>Last processed position</span>
                    <strong>#{lag.checkpoint_position}</strong>
                  </div>
                  <div>
                    <span>Current state</span>
                    <strong>{errorState}</strong>
                  </div>
                </div>
                <p className="projection-note">{assessment.explanation}</p>
                <div className="progress-track" aria-hidden="true">
                  <span className="progress-fill" style={{ width: `${Math.max(6, ratio * 100)}%` }} />
                </div>
              </article>
            );
          })
        )}
      </div>
    </section>
  );
}

function MCPResourcePanel(props: {
  resourceUri: string;
  onResourceUriChange: (value: string) => void;
  onInspectResource: (target?: string) => void;
  resourceBusy: boolean;
  resourceQuickPicks: Array<{ label: string; uri: string }>;
  resourceHistory: ResourceQueryHistoryEntry[];
  resourceDefinitions: ResourceDefinition[];
  resourceResult: string;
}): ReactNode {
  return (
    <section className="panel mcp-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">MCP Query Output</p>
          <h2>Resource inspector</h2>
        </div>
        <p className="section-note">Quick-select ledger resources and inspect raw responses.</p>
      </div>

      <div className="mcp-shell">
        <div className="mcp-controls">
          <label>
            Resource URI
            <input
              value={props.resourceUri}
              onChange={(event) => props.onResourceUriChange(event.target.value)}
              placeholder="ledger://applications/app-.../compliance"
            />
          </label>
          <div className="quick-actions">
            <button className="button" onClick={() => props.onInspectResource()} disabled={props.resourceBusy}>
              {props.resourceBusy ? "Loading resource..." : "Load resource"}
            </button>
          </div>
          <div className="tag-list">
            {props.resourceQuickPicks.map((item) => (
              <button
                type="button"
                className="button subtle"
                key={item.uri}
                onClick={() => {
                  props.onResourceUriChange(item.uri);
                  props.onInspectResource(item.uri);
                }}
              >
                {item.label}
              </button>
            ))}
          </div>

          <details className="detail-block">
            <summary>Resource catalogue ({props.resourceDefinitions.length})</summary>
            <div className="mini-timeline resource-catalogue">
              {props.resourceDefinitions.map((resource) => (
                <button
                  type="button"
                  className="checkpoint-button"
                  key={resource.uri}
                  onClick={() => props.onResourceUriChange(resource.uri)}
                >
                  <span className="checkpoint-event">{resource.uri}</span>
                  <span className="resource-description">{resource.description}</span>
                </button>
              ))}
            </div>
          </details>
        </div>

        <div className="mcp-results">
          <div className="mcp-history">
            <div className="panel-subhead">
              <strong>Recent queries</strong>
              <span>{props.resourceHistory.length}</span>
            </div>
            {props.resourceHistory.length === 0 ? (
              <p className="empty-state">No MCP resource queries loaded yet.</p>
            ) : (
              <div className="mini-timeline resource-catalogue">
                {props.resourceHistory.map((entry) => (
                  <button
                    type="button"
                    className="checkpoint-button"
                    key={`${entry.uri}-${entry.loadedAt}`}
                    onClick={() => {
                      props.onResourceUriChange(entry.uri);
                      props.onInspectResource(entry.uri);
                    }}
                  >
                    <span className="checkpoint-event">{entry.uri}</span>
                    <span className="resource-description">{entry.preview}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="definition-list compact mcp-preview-grid">
            <div>
              <span>Last response preview</span>
              <strong>{props.resourceHistory[0]?.preview ?? "Not loaded"}</strong>
            </div>
            <div>
              <span>Most recent query</span>
              <strong>{props.resourceHistory[0]?.uri ?? "Not loaded"}</strong>
            </div>
          </div>

          <pre className="result-box">
            {props.resourceResult || "Resource output appears here after loading a ledger URI."}
          </pre>
        </div>
      </div>
    </section>
  );
}

function CommandCenterPanel(props: {
  busy: boolean;
  applicationId: string;
  role: string;
  toolDefinitions: ToolDefinition[];
  selectedTool: string;
  onToolChange: (toolName: string) => void;
  onRunDemo: () => void;
  onPrepareTool: (toolName: string, overrides?: Record<string, unknown>) => void;
  hasIntegrityCommand: boolean;
  onRunCommand: () => void;
  selectedToolDefinition: ToolDefinition | null;
  commandPayload: string;
  onCommandPayloadChange: (value: string) => void;
  commandResult: string;
}): ReactNode {
  return (
    <section className="panel command-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Command Center</p>
          <h2>Controlled actions</h2>
        </div>
        <p className="section-note">Primary lifecycle actions are always visible.</p>
      </div>

      <div className="command-hero">
        <button className="button primary command-primary" onClick={props.onRunDemo} disabled={props.busy}>
          {props.busy ? "Working..." : "Run governed lifecycle"}
        </button>
        <div className="command-secondary">
          <button
            className="button subtle"
            onClick={() =>
              props.onPrepareTool("generate_decision", {
                application_id: props.applicationId || "app-addis-001"
              })
            }
          >
            Queue decision command
          </button>
          {props.hasIntegrityCommand ? (
            <button
              className="button subtle"
              onClick={() =>
                props.onPrepareTool("run_integrity_check", {
                  entity_type: "application",
                  entity_id: props.applicationId || "app-addis-001",
                  role: props.role
                })
              }
            >
              Queue integrity check
            </button>
          ) : null}
        </div>
      </div>

      <details className="detail-block command-advanced">
        <summary>Advanced controls</summary>
        <div className="command-advanced-body">
          <div className="command-builder">
            <label>
              Command
              <select value={props.selectedTool} onChange={(event) => props.onToolChange(event.target.value)}>
                {props.toolDefinitions.map((tool) => (
                  <option value={tool.name} key={tool.name}>
                    {tool.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="button"
              onClick={props.onRunCommand}
              disabled={props.busy || props.toolDefinitions.length === 0}
            >
              Execute command
            </button>
          </div>

          <p className="field-note">{conciseDescription(props.selectedToolDefinition?.description)}</p>
          <details className="detail-inline">
            <summary>Command guidance</summary>
            <p className="field-note">
              {props.selectedToolDefinition?.description ?? "No commands available for this role."}
            </p>
          </details>
          <details className="detail-block">
            <summary>Advanced payload editor</summary>
            <label>
              JSON payload
              <textarea
                className="code-area"
                value={props.commandPayload}
                onChange={(event) => props.onCommandPayloadChange(event.target.value)}
                rows={10}
              />
            </label>
          </details>
          <details className="detail-block">
            <summary>Command receipt</summary>
            <pre className="result-box">
              {props.commandResult || "Command result appears here after execution."}
            </pre>
          </details>
        </div>
      </details>
    </section>
  );
}

function AuditPhaseTimeline(props: {
  timelineGroups: TimelineGroup[];
  collapsedTimelineGroups: Record<string, boolean>;
  onToggleGroup: (key: string, nextValue: boolean) => void;
  eventTypeFilter: string;
  onEventTypeFilterChange: (value: string) => void;
  lineageEventTypes: string[];
  eventActorFilter: string;
  onEventActorFilterChange: (value: string) => void;
  lineageActors: string[];
  eventTimeRange: EventTimeRange;
  onEventTimeRangeChange: (value: EventTimeRange) => void;
  onResetFilters: () => void;
  filteredCount: number;
  applicationId: string;
  showMetadata: boolean;
  onToggleMetadata: () => void;
}): ReactNode {
  return (
    <section className="panel lineage-panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Event Evidence Timeline</p>
          <h2>Phase-grouped audit flow</h2>
        </div>
        <div className="panel-actions">
          <p className="section-note">{props.filteredCount} filtered events</p>
          <button type="button" className="text-button" onClick={props.onToggleMetadata}>
            {props.showMetadata ? "Hide metadata" : "Show metadata"}
          </button>
        </div>
      </div>

      <div className="timeline-toolbar">
        <label>
          Event type
          <select value={props.eventTypeFilter} onChange={(event) => props.onEventTypeFilterChange(event.target.value)}>
            <option value="all">All event types</option>
            {props.lineageEventTypes.map((eventType) => (
              <option key={eventType} value={eventType}>
                {titleize(eventType)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Actor / agent
          <select value={props.eventActorFilter} onChange={(event) => props.onEventActorFilterChange(event.target.value)}>
            <option value="all">All actors</option>
            {props.lineageActors.map((actor) => (
              <option key={actor} value={actor}>
                {actor}
              </option>
            ))}
          </select>
        </label>
        <label>
          Time range
          <select
            value={props.eventTimeRange}
            onChange={(event) => props.onEventTimeRangeChange(event.target.value as EventTimeRange)}
          >
            <option value="all">All time</option>
            <option value="1h">Last 1 hour</option>
            <option value="24h">Last 24 hours</option>
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
          </select>
        </label>
        <button type="button" className="button subtle" onClick={props.onResetFilters}>
          Reset filters
        </button>
      </div>

      {props.timelineGroups.length === 0 ? (
        <p className="empty-state">No lineage for the current filter scope.</p>
      ) : (
        <div className="phase-stack">
          {props.timelineGroups.map((group) => {
            const isCollapsed = props.collapsedTimelineGroups[group.key] ?? false;
            const latestItem = group.items[group.items.length - 1];

            return (
              <section className="phase-group" key={group.key}>
                <div className="phase-header">
                  <div>
                    <span className="phase-label">{group.phase}</span>
                    <strong>{group.items.length} event{group.items.length === 1 ? "" : "s"}</strong>
                  </div>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => props.onToggleGroup(group.key, !isCollapsed)}
                  >
                    {isCollapsed ? "Expand phase" : "Collapse phase"}
                  </button>
                </div>

                {isCollapsed ? (
                  <button
                    type="button"
                    className={`phase-collapsed ${isFinalDecisionEvent(latestItem.eventType) ? "phase-collapsed-final" : ""}`}
                    onClick={() => props.onToggleGroup(group.key, false)}
                  >
                    <div className="phase-collapsed-main">
                      <strong>{titleize(latestItem.eventType)}</strong>
                      <span>{latestItem.actor}</span>
                    </div>
                    <span>{prettyDate(latestItem.recordedAt)}</span>
                  </button>
                ) : (
                  <div className="lineage-list">
                    {group.items.map((item) => (
                      <article
                        className={`lineage-item ${isFinalDecisionEvent(item.eventType) ? "final-decision" : ""}`}
                        key={item.key}
                      >
                        <div className={`lineage-node tone-${item.tone}`} aria-hidden="true" />
                        <div className="lineage-content">
                          <div className="lineage-header">
                            <div>
                              <span className="lineage-lane">{item.lane}</span>
                              <h3>{titleize(item.eventType)}</h3>
                            </div>
                            <StatusBadge label={prettyDate(item.recordedAt)} tone={item.tone} />
                          </div>
                          <div className="lineage-meta">
                            <span>{item.actor}</span>
                            <span>{props.applicationId || "No application selected"}</span>
                            <span>{item.checkpoint}</span>
                          </div>
                          <p className="lineage-summary">{item.summary}</p>
                          {props.showMetadata ? (
                            <div className="evidence-inline">
                              <span className="meta-label">Event metadata</span>
                              <div className="lineage-evidence">
                                <span>Stream {item.streamId}</span>
                                <span>Position #{item.globalPosition}</span>
                                <span>Correlation {shortId(item.correlationId)}</span>
                                <span>Causation {shortId(item.causationId)}</span>
                              </div>
                            </div>
                          ) : null}
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}

function EventCoveragePanel(props: {
  applicationId: string;
  observedEventTypes: string[];
}): ReactNode {
  const observedSet = new Set(props.observedEventTypes);
  const expectedSet = new Set(EXPECTED_EVENT_TYPES);
  const missingTypes = EXPECTED_EVENT_TYPES.filter((eventType) => !observedSet.has(eventType));
  const unexpectedTypes = props.observedEventTypes.filter((eventType) => !expectedSet.has(eventType));

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">Coverage Check</p>
          <h2>Event catalogue coverage</h2>
        </div>
        <p className="section-note">
          {props.applicationId ? props.applicationId : "No focused application"}
        </p>
      </div>

      {!props.applicationId ? (
        <p className="empty-state">Select an application to compare expected and observed events.</p>
      ) : (
        <>
          <div className="definition-list compact">
            <div>
              <span>Expected event types</span>
              <strong>{EXPECTED_EVENT_TYPES.length}</strong>
            </div>
            <div>
              <span>Observed event types</span>
              <strong>{props.observedEventTypes.length}</strong>
            </div>
            <div>
              <span>Missing event types</span>
              <strong>{missingTypes.length}</strong>
            </div>
            <div>
              <span>Unexpected event types</span>
              <strong>{unexpectedTypes.length}</strong>
            </div>
          </div>

          <div className="projection-list">
            {EXPECTED_EVENT_GROUPS.map((group) => {
              const groupObserved = group.eventTypes.filter((eventType) => observedSet.has(eventType));
              const groupMissing = group.eventTypes.filter((eventType) => !observedSet.has(eventType));
              const tone: Tone =
                groupMissing.length === 0
                  ? "ok"
                  : groupObserved.length > 0
                    ? "warning"
                    : "critical";
              return (
                <article className={`projection-row projection-${tone}`} key={group.label}>
                  <div className="projection-head">
                    <strong>{group.label}</strong>
                    <StatusBadge
                      label={`${groupObserved.length}/${group.eventTypes.length} observed`}
                      tone={tone}
                    />
                  </div>
                  <p className="projection-note">
                    {groupMissing.length === 0
                      ? "All expected events observed."
                      : `Missing: ${groupMissing.map((eventType) => titleize(eventType)).join(", ")}`}
                  </p>
                </article>
              );
            })}
          </div>

          {unexpectedTypes.length > 0 ? (
            <div className="evidence-inline">
              <span className="meta-label">Observed outside catalogue</span>
              <p className="field-note">
                {unexpectedTypes.map((eventType) => titleize(eventType)).join(", ")}
              </p>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function countFailedChecks(snapshot: Record<string, unknown> | null | undefined): number {
  if (!snapshot) {
    return 0;
  }
  const failedChecks = readRecord(snapshot.failed_checks);
  return Object.keys(failedChecks).length;
}

function assessProjection(lag: {
  events_behind: number;
  lag_ms: number;
  status: string;
}): ProjectionAssessment {
  const normalized = String(lag.status).toLowerCase();
  const hasBacklog = lag.events_behind > 0;

  if (!hasBacklog) {
    if (normalized.includes("critical") || normalized.includes("warning") || lag.lag_ms > 300_000) {
      return {
        tone: "warning",
        statusLabel: "Synced with warning",
        severityLabel: normalized.includes("critical") || lag.lag_ms > 300_000 ? "Critical lag" : "Warning lag",
        explanation: "No backlog; projection timer elevated.",
        isSynced: true
      };
    }
    return {
      tone: "ok",
      statusLabel: "Synced",
      severityLabel: "Healthy",
      explanation: "Projection is up to date.",
      isSynced: true
    };
  }

  if (lag.events_behind <= 5) {
    return {
      tone: "warning",
      statusLabel: "Backlog",
      severityLabel: "Warning",
      explanation: `${lag.events_behind} events pending projection.`,
      isSynced: false
    };
  }

  return {
    tone: "critical",
    statusLabel: "Backlog",
    severityLabel: "Critical",
    explanation: `${lag.events_behind} events pending projection.`,
    isSynced: false
  };
}

export default function App() {
  const [authReady, setAuthReady] = useState<boolean>(false);
  const [authUser, setAuthUser] = useState<MeResponse | null>(null);
  const [loginUsername, setLoginUsername] = useState<string>("nurye");
  const [loginPassword, setLoginPassword] = useState<string>("nurye@123");

  const [toolDefinitions, setToolDefinitions] = useState<ToolDefinition[]>([]);
  const [selectedTool, setSelectedTool] = useState<string>("submit_application");
  const [commandPayload, setCommandPayload] = useState<string>(
    JSON.stringify(DEFAULT_COMMAND_PAYLOAD.submit_application, null, 2)
  );
  const [commandResult, setCommandResult] = useState<string>("");

  const [applicationId, setApplicationId] = useState<string>("");
  const [agentId, setAgentId] = useState<string>("credit-agent-ethi-01");
  const [temporalAsOf, setTemporalAsOf] = useState<string>("");

  const [states, setStates] = useState<AppStateCount[]>([]);
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [application, setApplication] = useState<ApplicationSummary | null>(null);
  const [compliance, setCompliance] = useState<ComplianceView | null>(null);
  const [temporalCompliance, setTemporalCompliance] = useState<ComplianceView | null>(null);
  const [agentPerformance, setAgentPerformance] = useState<AgentPerformance | null>(null);
  const [health, setHealth] = useState<LedgerHealth | null>(null);
  const [recentEvents, setRecentEvents] = useState<RecentEvent[]>([]);
  const [applicationEvents, setApplicationEvents] = useState<RecentEvent[]>([]);
  const [integrityTrail, setIntegrityTrail] = useState<RecentEvent[]>([]);
  const [auditRows, setAuditRows] = useState<AuthAuditRow[]>([]);
  const [resourceDefinitions, setResourceDefinitions] = useState<ResourceDefinition[]>([]);
  const [resourceUri, setResourceUri] = useState<string>("ledger://ledger/health");
  const [resourceResult, setResourceResult] = useState<string>("");
  const [resourceBusy, setResourceBusy] = useState<boolean>(false);
  const [resourceHistory, setResourceHistory] = useState<ResourceQueryHistoryEntry[]>([]);
  const [sessionIdInput, setSessionIdInput] = useState<string>("");
  const [sessionReplay, setSessionReplay] = useState<AgentSessionReplay | null>(null);
  const [sessionReplayBusy, setSessionReplayBusy] = useState<boolean>(false);
  const [lastIntegrityCheckResult, setLastIntegrityCheckResult] = useState<Record<string, unknown> | null>(null);
  const [eventTypeFilter, setEventTypeFilter] = useState<string>("all");
  const [eventActorFilter, setEventActorFilter] = useState<string>("all");
  const [eventTimeRange, setEventTimeRange] = useState<EventTimeRange>("all");
  const [collapsedTimelineGroups, setCollapsedTimelineGroups] = useState<Record<string, boolean>>({});

  const [busy, setBusy] = useState<boolean>(false);
  const [temporalBusy, setTemporalBusy] = useState<boolean>(false);
  const [notice, setNotice] = useState<string>("");
  const [noticeToastVisible, setNoticeToastVisible] = useState<boolean>(true);
  const [lastRefresh, setLastRefresh] = useState<string>(new Date().toISOString());
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("operations");
  const [evidencePanels, setEvidencePanels] = useState<Record<EvidencePanelKey, boolean>>({
    integrity: false,
    snapshot: false,
    lineage: false,
    activity: false,
    compliance: false
  });

  const projectionRows = useMemo(() => {
    if (!health) {
      return [];
    }
    return Object.entries(health.projections);
  }, [health]);

  const sessionCandidates = useMemo(() => {
    const currentAgentId = agentId.trim();
    const currentApplicationId = applicationId.trim();
    const sourceEvents = currentApplicationId ? applicationEvents : recentEvents;
    const seen = new Set<string>();
    const candidates: string[] = [];

    for (const event of sourceEvents) {
      const payload = readRecord(event.payload);
      const candidate = extractSessionId(event);
      if (!candidate || seen.has(candidate)) {
        continue;
      }

      const payloadAgentId = readString(payload.agent_id);
      if (
        currentAgentId &&
        payloadAgentId &&
        payloadAgentId !== currentAgentId
      ) {
        continue;
      }
      if (
        currentAgentId &&
        !payloadAgentId &&
        !event.stream_id.includes(`agent-${currentAgentId}-`)
      ) {
        continue;
      }

      const payloadApplicationId = readString(payload.application_id);
      if (
        currentApplicationId &&
        payloadApplicationId &&
        payloadApplicationId !== currentApplicationId
      ) {
        continue;
      }

      seen.add(candidate);
      candidates.push(candidate);
    }

    return candidates.slice(0, 10);
  }, [agentId, applicationEvents, applicationId, recentEvents]);

  const resourceQuickPicks = useMemo(() => {
    const picks: Array<{ label: string; uri: string }> = [
      { label: "Ledger Health", uri: "ledger://ledger/health" }
    ];
    const focusApplicationId = applicationId.trim();
    const focusAgentId = agentId.trim();
    const focusSessionId = sessionIdInput.trim() || sessionCandidates[0];

    if (focusApplicationId) {
      picks.push(
        {
          label: "Application Summary",
          uri: `ledger://applications/${focusApplicationId}`
        },
        {
          label: "Compliance (current)",
          uri: `ledger://applications/${focusApplicationId}/compliance`
        },
        {
          label: "Audit Trail",
          uri: `ledger://applications/${focusApplicationId}/audit-trail`
        }
      );
    }

    if (focusAgentId) {
      picks.push({
        label: "Agent Performance",
        uri: `ledger://agents/${focusAgentId}/performance`
      });
    }

    if (focusAgentId && focusSessionId) {
      picks.push({
        label: "Agent Session Replay",
        uri: `ledger://agents/${focusAgentId}/sessions/${focusSessionId}`
      });
    }

    return picks;
  }, [agentId, applicationId, sessionCandidates, sessionIdInput]);

  const canViewAudit = useMemo(() => {
    return authUser?.role === "compliance" || authUser?.role === "admin";
  }, [authUser]);

  const selectedToolDefinition = useMemo(() => {
    return toolDefinitions.find((tool) => tool.name === selectedTool) ?? null;
  }, [selectedTool, toolDefinitions]);

  const projectionStatusRows = useMemo(() => {
    return projectionRows.map(([name, lag]) => ({
      name,
      lag,
      assessment: assessProjection(lag)
    }));
  }, [projectionRows]);

  const projectionSummary = useMemo(() => {
    if (projectionStatusRows.length === 0) {
      return {
        tone: "neutral" as Tone,
        label: "No data",
        detail: "Waiting for projection checkpoints",
        syncedCount: 0
      };
    }

    const syncedCount = projectionStatusRows.filter((item) => item.assessment.isSynced).length;
    const backlogRows = projectionStatusRows.filter((item) => !item.assessment.isSynced);
    const worstBacklog = backlogRows.reduce((max, item) => Math.max(max, item.lag.events_behind), 0);
    const hasCritical = projectionStatusRows.some((item) => item.assessment.tone === "critical");
    const hasWarning = projectionStatusRows.some((item) => item.assessment.tone === "warning");
    const tone: Tone = hasCritical ? "critical" : hasWarning ? "warning" : "ok";

    if (backlogRows.length > 0) {
      return {
        tone,
        label: `${backlogRows.length} with backlog`,
        detail: `Worst backlog ${worstBacklog} events`,
        syncedCount
      };
    }

    return {
      tone,
      label: hasWarning ? "Synced with timer warning" : "All synced",
      detail: `${syncedCount} projections at latest position`,
      syncedCount
    };
  }, [projectionStatusRows]);

  const openComplianceIssues = useMemo(() => {
    return applications.filter((item) => item.compliance_status && item.compliance_status !== "CLEARED").length;
  }, [applications]);

  const applicationLineage = useMemo(() => {
    const rows = applicationEvents.map(buildLineageItemFromRecent);

    const complianceRows = (compliance?.timeline ?? []).map(buildLineageItemFromCompliance);
    const deduped = new Map<string, LineageItem>();

    [...rows, ...complianceRows].forEach((item) => {
      deduped.set(item.key, item);
    });

    return Array.from(deduped.values()).sort((left, right) => left.globalPosition - right.globalPosition);
  }, [applicationEvents, compliance]);

  const lineageEventTypes = useMemo(() => {
    return Array.from(new Set(applicationLineage.map((item) => item.eventType))).sort();
  }, [applicationLineage]);

  const lineageActors = useMemo(() => {
    return Array.from(new Set(applicationLineage.map((item) => item.actor))).sort();
  }, [applicationLineage]);

  const filteredLineage = useMemo(() => {
    return applicationLineage.filter((item) => {
      if (eventTypeFilter !== "all" && item.eventType !== eventTypeFilter) {
        return false;
      }
      if (eventActorFilter !== "all" && item.actor !== eventActorFilter) {
        return false;
      }
      if (!matchesTimeRange(item.recordedAt, eventTimeRange)) {
        return false;
      }
      return true;
    });
  }, [applicationLineage, eventActorFilter, eventTimeRange, eventTypeFilter]);

  const timelineGroups = useMemo(() => {
    const groups: TimelineGroup[] = [];
    for (const item of filteredLineage) {
      const phase = eventPhase(item.eventType);
      const current = groups[groups.length - 1];
      if (!current || current.phase !== phase) {
        groups.push({
          key: `${phase}-${item.globalPosition}`,
          phase,
          items: [item]
        });
        continue;
      }
      current.items.push(item);
    }
    return groups;
  }, [filteredLineage]);

  const integritySummary = useMemo(() => {
    const latest = integrityTrail[integrityTrail.length - 1] ?? null;
    const verifiedCount = readNumber(latest?.payload.events_verified_count);
    const integrityHash = readString(latest?.payload.integrity_hash);
    const chainValid = typeof lastIntegrityCheckResult?.chain_valid === "boolean" ? lastIntegrityCheckResult.chain_valid : null;
    const violationCount = readNumber(lastIntegrityCheckResult?.violation_count);
    const tone =
      chainValid === false
        ? "critical"
        : latest
          ? "ok"
          : applicationId
            ? "warning"
            : "neutral";

    return {
      tone: tone as Tone,
      status:
        chainValid === false
          ? "Failed verification"
          : latest
            ? "Verified"
            : applicationId
              ? "Pending verification"
              : "No focal application",
      tamperState:
        chainValid === false
          ? `${violationCount ?? 0} integrity violation${violationCount === 1 ? "" : "s"} detected in the latest chain run.`
          : latest
            ? "No tamper evidence recorded in the latest chain run."
            : "Not yet evaluated for this focus. Run integrity check to attest the chain.",
      verifiedWindow: latest
        ? `${verifiedCount ?? 0} events sealed in latest verification`
        : "No verification window recorded",
      evidenceHash: integrityHash ? shortId(integrityHash, 8) : "Unavailable",
      verifiedAt: latest ? prettyDate(latest.recorded_at) : "Not yet verified"
    };
  }, [applicationId, integrityTrail, lastIntegrityCheckResult]);

  const integrityCoverage = useMemo(() => {
    const verified = readNumber(integrityTrail[integrityTrail.length - 1]?.payload.events_verified_count) ?? 0;
    const total = applicationLineage.length;
    return {
      verified,
      total
    };
  }, [applicationLineage.length, integrityTrail]);

  const temporalDelta = useMemo(() => {
    const currentStatus =
      application?.compliance_status ??
      readString(compliance?.snapshot.compliance_status) ??
      "NOT_STARTED";
    const temporalStatus = readString(temporalCompliance?.snapshot.compliance_status);
    if (!temporalStatus) {
      return "Select a checkpoint to reconstruct the compliance state.";
    }
    if (temporalStatus === currentStatus) {
      return `State matches the live compliance posture (${titleize(currentStatus)}).`;
    }
    return `State diverges from current posture: ${titleize(temporalStatus)} then, ${titleize(currentStatus)} now.`;
  }, [application, compliance, temporalCompliance]);

  const fabricRows = useMemo(() => {
    const scopedOperationalEvents = applicationId
      ? [...applicationLineage]
          .sort((left, right) => right.globalPosition - left.globalPosition)
          .map((item) => ({
            event_type: item.eventType,
            recorded_at: item.recordedAt
          }))
      : recentEvents.map((event) => ({
          event_type: event.event_type,
          recorded_at: event.recorded_at
        }));

    return OPERATING_FABRIC.map((agent) => {
      const observedEvent = scopedOperationalEvents.find((event) =>
        agent.eventTypes.some((eventType) => eventType === event.event_type)
      );
      const latestEventType = observedEvent?.event_type ?? null;
      const isCompleted =
        latestEventType === "CreditAnalysisCompleted" ||
        latestEventType === "FraudScreeningCompleted" ||
        latestEventType === "ComplianceCheckCompleted" ||
        latestEventType === "DecisionGenerated" ||
        latestEventType === "HumanReviewRequested";
      const isInProgress = Boolean(observedEvent && !isCompleted);
      return {
        ...agent,
        status: isCompleted ? "Complete" : isInProgress ? "Live" : "Pending",
        latestEvent: observedEvent ? titleize(observedEvent.event_type) : "No event",
        latestAt: observedEvent ? prettyDate(observedEvent.recorded_at) : "No timestamp",
        tone: isCompleted ? ("ok" as Tone) : isInProgress ? ("info" as Tone) : ("warning" as Tone)
      };
    });
  }, [applicationId, applicationLineage, recentEvents]);

  const humanReviewState = useMemo(() => {
    const hasHumanReviewRequested = applicationLineage.some((item) => item.eventType === "HumanReviewRequested");
    const hasHumanReviewCompleted = applicationLineage.some(
      (item) =>
        item.eventType === "HumanReviewCompleted" ||
        item.eventType === "ApplicationApproved" ||
        item.eventType === "ApplicationDeclined"
    );
    const latestHumanReviewEvent = [...applicationLineage]
      .reverse()
      .find((item) =>
        item.eventType === "HumanReviewRequested" ||
        item.eventType === "HumanReviewCompleted" ||
        item.eventType === "ApplicationApproved" ||
        item.eventType === "ApplicationDeclined"
      );

    if (application?.final_decision || hasHumanReviewCompleted) {
      const derivedDecision =
        application?.final_decision ??
        (latestHumanReviewEvent?.eventType === "ApplicationApproved"
          ? "approved"
          : latestHumanReviewEvent?.eventType === "ApplicationDeclined"
            ? "declined"
            : "completed");
      return {
        label: "Complete",
        tone: "ok" as Tone,
        detail: titleize(derivedDecision),
        timestamp: latestHumanReviewEvent?.recordedAt ?? application?.updated_at ?? null
      };
    }

    if (hasHumanReviewRequested) {
      return {
        label: "Pending",
        tone: "warning" as Tone,
        detail: "Awaiting officer review",
        timestamp: latestHumanReviewEvent?.recordedAt ?? null
      };
    }

    return {
      label: "Not requested",
      tone: "neutral" as Tone,
      detail: "Review not requested yet",
      timestamp: null
    };
  }, [application?.final_decision, application?.updated_at, applicationLineage]);

  const pipelineStages = useMemo(() => {
    const humanStageStatus =
      humanReviewState.label === "Complete"
        ? "Complete"
        : humanReviewState.label === "Pending"
          ? "Live"
          : "Pending";

    return [
      ...fabricRows,
      {
        name: "HumanReview",
        role: "Binding human decision",
        status: humanStageStatus,
        latestEvent: humanReviewState.detail,
        latestAt: humanReviewState.timestamp ? prettyDate(humanReviewState.timestamp) : "No timestamp",
        tone:
          humanReviewState.label === "Complete"
            ? ("ok" as Tone)
            : humanReviewState.label === "Pending"
              ? ("info" as Tone)
              : ("warning" as Tone)
      }
    ];
  }, [fabricRows, humanReviewState]);

  const currentComplianceSnapshot = readRecord(compliance?.snapshot);
  const temporalSnapshot = readRecord(temporalCompliance?.snapshot);
  const currentFailedChecks = countFailedChecks(currentComplianceSnapshot);
  const temporalFailedChecks = countFailedChecks(temporalSnapshot);
  const hasIntegrityCommand = authUser?.allowed_commands.includes("run_integrity_check") ?? false;
  const primaryPipelineState = states[0];
  const isOperationsView = workspaceMode === "operations";
  const isAuditView = workspaceMode === "audit";
  const isSystemView = workspaceMode === "system";
  const allEvidenceExpanded = useMemo(() => {
    return EVIDENCE_PANEL_KEYS.every((panel) => evidencePanels[panel]);
  }, [evidencePanels]);
  const activityMaxCount = 16;
  const activityWindow = recentEvents.slice(0, activityMaxCount);
  const visibleActivity = activityWindow;
  const applicationRegisterLimit = isOperationsView ? 10 : 12;
  const visibleApplicationRows = applications.slice(0, applicationRegisterLimit);
  const replayCheckpointCount = isOperationsView ? 3 : 6;

  const toggleEvidencePanel = useCallback((panel: EvidencePanelKey) => {
    setEvidencePanels((current) => ({ ...current, [panel]: !current[panel] }));
  }, []);

  const setAllEvidencePanels = useCallback((expanded: boolean) => {
    setEvidencePanels({
      integrity: expanded,
      snapshot: expanded,
      lineage: expanded,
      activity: expanded,
      compliance: expanded
    });
  }, []);

  const doLogout = useCallback((reason?: string) => {
    clearStoredToken();
    setAuthUser(null);
    setToolDefinitions([]);
    setResourceDefinitions([]);
    setResourceResult("");
    setResourceHistory([]);
    setCommandResult("");
    setLastIntegrityCheckResult(null);
    setAuditRows([]);
    setApplicationEvents([]);
    setIntegrityTrail([]);
    setTemporalCompliance(null);
    setSessionReplay(null);
    setSessionIdInput("");
    setNotice(reason ?? "Logged out.");
  }, []);

  const refreshFocused = useCallback(
    async (nextApplicationId = applicationId, nextAgentId = agentId) => {
      const focusApplicationId = nextApplicationId.trim();
      const focusAgentId = nextAgentId.trim();
      const tasks: Promise<void>[] = [];

      if (focusApplicationId) {
        tasks.push(
          (async () => {
            let nextApplication: ApplicationSummary;
            try {
              nextApplication = await fetchApplication(focusApplicationId);
            } catch (error) {
              if (error instanceof LedgerApiError && error.status === 404) {
                // Focused app no longer exists (stale UI state). Clear focus so auto-pick logic can recover.
                setApplicationId("");
                setApplication(null);
                setApplicationEvents([]);
                setCompliance(null);
                setTemporalCompliance(null);
                setIntegrityTrail([]);
                return;
              }
              throw error;
            }
            setApplication(nextApplication);
            const nextAppEvents = await fetchApplicationEvents(focusApplicationId, 5000);
            setApplicationEvents(nextAppEvents);

            try {
              const trail = await fetchApplicationAuditTrail(focusApplicationId);
              setIntegrityTrail(trail.events);
            } catch (error) {
              if (error instanceof LedgerApiError && error.status === 404) {
                setIntegrityTrail([]);
              } else {
                throw error;
              }
            }

            if (!nextApplication.compliance_status) {
              setCompliance(null);
              return;
            }

            try {
              const nextCompliance = await fetchCompliance(focusApplicationId);
              setCompliance(nextCompliance);
            } catch (error) {
              if (error instanceof LedgerApiError && error.status === 404) {
                setCompliance(null);
                return;
              }
              throw error;
            }
          })()
        );
      } else {
        setApplicationEvents([]);
      }

      if (focusAgentId) {
        tasks.push(
          (async () => {
            const nextPerformance = await fetchAgentPerformance(focusAgentId);
            setAgentPerformance(nextPerformance);
          })()
        );
      }

      if (tasks.length > 0) {
        await Promise.all(tasks);
        setLastRefresh(new Date().toISOString());
      }
    },
    [agentId, applicationId]
  );

  const refreshGlobal = useCallback(async () => {
    const [nextStates, nextApps, nextHealth, nextEvents, nextTools, nextResources] = await Promise.all([
      fetchApplicationStates(),
      fetchApplications(40),
      fetchLedgerHealth(),
      fetchRecentEvents(60),
      fetchTools(),
      fetchResources()
    ]);

    setStates(nextStates);
    setApplications(nextApps);
    setHealth(nextHealth);
    setRecentEvents(nextEvents);
    setToolDefinitions(nextTools);
    setResourceDefinitions(nextResources);

    if (nextTools.length > 0 && !nextTools.some((item) => item.name === selectedTool)) {
      const nextTool = nextTools[0].name;
      setSelectedTool(nextTool);
      setCommandPayload(JSON.stringify(commandSeed(nextTool, applicationId, authUser?.role), null, 2));
    }

    if (canViewAudit) {
      const audit = await fetchAuthAudit(40);
      setAuditRows(audit);
    } else {
      setAuditRows([]);
    }

    setLastRefresh(new Date().toISOString());
  }, [applicationId, authUser?.role, canViewAudit, selectedTool]);

  const loadEverything = useCallback(async () => {
    if (!authUser) {
      return;
    }

    try {
      await refreshGlobal();
      await refreshFocused();
      setNotice("");
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setNotice(`${error.message} (${error.errorType ?? "RequestError"})`);
      } else {
        setNotice("Unexpected UI refresh error.");
      }
    }
  }, [authUser, doLogout, refreshFocused, refreshGlobal]);

  useEffect(() => {
    void (async () => {
      try {
        const me = await fetchMe();
        setAuthUser(me);
        setNotice("");
      } catch {
        clearStoredToken();
      } finally {
        setAuthReady(true);
      }
    })();
  }, []);

  useEffect(() => {
    if (!authUser) {
      return;
    }
    setWorkspaceMode("operations");
    setAllEvidencePanels(false);
  }, [authUser, setAllEvidencePanels]);

  useEffect(() => {
    if (!authUser) {
      return;
    }
    void loadEverything();
  }, [authUser, loadEverything]);

  useEffect(() => {
    if (!authUser) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadEverything();
    }, 7000);
    return () => window.clearInterval(timer);
  }, [authUser, loadEverything]);

  useEffect(() => {
    if (!authUser || applicationId.trim() || applications.length === 0) {
      return;
    }
    const latestApplicationId = applications[0].application_id;
    setApplicationId(latestApplicationId);
    void refreshFocused(latestApplicationId, agentId);
  }, [agentId, applications, applicationId, authUser, refreshFocused]);

  useEffect(() => {
    if (!temporalAsOf && compliance?.timeline?.length) {
      const latestCheckpoint = compliance.timeline[compliance.timeline.length - 1];
      setTemporalAsOf(toDateTimeLocalValue(latestCheckpoint.recorded_at));
    }
  }, [compliance, temporalAsOf]);

  useEffect(() => {
    if (sessionIdInput || sessionCandidates.length === 0) {
      return;
    }
    setSessionIdInput(sessionCandidates[0]);
  }, [sessionCandidates, sessionIdInput]);

  useEffect(() => {
    setCollapsedTimelineGroups({});
  }, [applicationId, eventActorFilter, eventTimeRange, eventTypeFilter]);

  useEffect(() => {
    if (workspaceMode === "operations") {
      setAllEvidencePanels(false);
    }
  }, [applicationId, setAllEvidencePanels, workspaceMode]);

  useEffect(() => {
    if (!notice) {
      return;
    }
    setNoticeToastVisible(true);
    const timer = window.setTimeout(() => {
      setNoticeToastVisible(false);
    }, 5500);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    try {
      const session = await login(loginUsername.trim(), loginPassword);
      setAuthUser({
        username: session.user.username,
        role: session.user.role,
        issued_at: session.user.issued_at,
        expires_at: session.expires_at,
        allowed_commands: session.allowed_commands
      });
      setNotice(`Apex session established for ${session.user.username}.`);
      setCommandPayload(
        JSON.stringify(commandSeed(selectedTool, applicationId, session.user.role), null, 2)
      );
    } catch (error) {
      if (error instanceof LedgerApiError) {
        setNotice(error.message);
      } else {
        setNotice("Login failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleRunDemo() {
    setBusy(true);
    const requestedApplicationId = applicationId.trim() || undefined;
    const requestedIdExists = Boolean(
      requestedApplicationId &&
        applications.some((item) => item.application_id === requestedApplicationId)
    );
    const targetApplicationId = requestedIdExists ? undefined : requestedApplicationId;
    setNotice(
      targetApplicationId
        ? `Running governed lifecycle scenario for ${targetApplicationId}...`
        : requestedIdExists
          ? `Application ${requestedApplicationId} already exists. Running governed lifecycle with a new generated ID...`
        : "Running governed lifecycle scenario..."
    );
    try {
      const result = await bootstrapDemo(targetApplicationId);
      const nextApplicationId = String(result.application_id ?? "");
      const nextAgentId = String(result.agent_id ?? "");
      if (nextApplicationId) {
        setApplicationId(nextApplicationId);
      }
      if (nextAgentId) {
        setAgentId(nextAgentId);
      }
      setNotice(`Decision lifecycle bootstrapped for ${nextApplicationId}.`);
      await refreshGlobal();
      await refreshFocused(nextApplicationId, nextAgentId || agentId);
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setNotice(`Governed lifecycle run failed: ${error.message}`);
      } else {
        setNotice("Governed lifecycle run failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleRunCommand() {
    setBusy(true);
    setCommandResult("");
    setNotice(`Submitting ${selectedTool} to the write-side command surface...`);

    try {
      const parsed = JSON.parse(commandPayload) as Record<string, unknown>;
      const result = await runCommand(selectedTool, parsed);
      setCommandResult(JSON.stringify(result, null, 2));
      if (selectedTool === "run_integrity_check") {
        setLastIntegrityCheckResult(result);
      }

      const nextApplicationId =
        readString(parsed.application_id) ??
        (selectedTool === "run_integrity_check" ? readString(parsed.entity_id) : null) ??
        applicationId;
      const nextAgentId = readString(parsed.agent_id) ?? agentId;

      if (nextApplicationId) {
        setApplicationId(nextApplicationId);
      }
      if (nextAgentId) {
        setAgentId(nextAgentId);
      }

      setNotice(`${selectedTool} completed and new ledger evidence is available.`);
      await refreshGlobal();
      await refreshFocused(nextApplicationId, nextAgentId);
    } catch (error) {
      if (isAuthFailure(error)) {
        setCommandResult("");
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof SyntaxError) {
        setCommandResult(
          JSON.stringify(
            {
              ok: false,
              error: {
                error_type: "InvalidJson",
                message: "Payload editor contains invalid JSON."
              }
            },
            null,
            2
          )
        );
        setNotice("Invalid JSON in command payload.");
      } else if (error instanceof LedgerApiError) {
        setCommandResult(
          JSON.stringify(
            {
              ok: false,
              error: {
                error_type: error.errorType ?? "RequestError",
                message: error.message,
                suggested_action: error.suggestedAction ?? null,
                status: error.status
              }
            },
            null,
            2
          )
        );
        setNotice(
          `${error.message}${error.suggestedAction ? ` | Try: ${error.suggestedAction}` : ""}`
        );
      } else {
        setCommandResult(
          JSON.stringify(
            {
              ok: false,
              error: {
                error_type: "UnexpectedError",
                message: "Command execution failed unexpectedly."
              }
            },
            null,
            2
          )
        );
        setNotice("Command execution failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function handleRunVerification() {
    const targetApplicationId = applicationId.trim();
    if (!targetApplicationId || !hasIntegrityCommand) {
      setNotice("Choose an application with verification access before running verification.");
      return;
    }

    const payload = {
      entity_type: "application",
      entity_id: targetApplicationId,
      role: authUser?.role ?? "admin"
    };

    setBusy(true);
    setSelectedTool("run_integrity_check");
    setCommandPayload(JSON.stringify(payload, null, 2));
    setNotice(`Running integrity verification for ${targetApplicationId}...`);

    try {
      const result = await runCommand("run_integrity_check", payload);
      setCommandResult(JSON.stringify(result, null, 2));
      setLastIntegrityCheckResult(result);
      setNotice(`Integrity verification completed for ${targetApplicationId}.`);
      await refreshGlobal();
      await refreshFocused(targetApplicationId, agentId);
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setCommandResult(
          JSON.stringify(
            {
              ok: false,
              error: {
                error_type: error.errorType ?? "RequestError",
                message: error.message,
                suggested_action: error.suggestedAction ?? null,
                status: error.status
              }
            },
            null,
            2
          )
        );
        setNotice(`Verification failed: ${error.message}`);
      } else {
        setNotice("Verification failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  function handleToolChange(nextTool: string) {
    setSelectedTool(nextTool);
    setCommandPayload(JSON.stringify(commandSeed(nextTool, applicationId, authUser?.role), null, 2));
    setCommandResult("");
  }

  function prepareTool(toolName: string, overrides?: Record<string, unknown>) {
    const nextPayload = { ...commandSeed(toolName, applicationId, authUser?.role), ...overrides };
    setSelectedTool(toolName);
    setCommandPayload(JSON.stringify(nextPayload, null, 2));
    setCommandResult("");
  }

  async function handleTemporalInspect() {
    const targetApplicationId = applicationId.trim();
    if (!targetApplicationId || !temporalAsOf) {
      setNotice("Choose an application and timestamp before running temporal reconstruction.");
      return;
    }

    setTemporalBusy(true);
    try {
      const snapshot = await fetchCompliance(targetApplicationId, fromDateTimeLocalValue(temporalAsOf));
      setTemporalCompliance(snapshot);
      setNotice(`Regulatory reconstruction loaded for ${targetApplicationId}.`);
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setNotice(`Temporal reconstruction failed: ${error.message}`);
      } else {
        setNotice("Temporal reconstruction failed unexpectedly.");
      }
    } finally {
      setTemporalBusy(false);
    }
  }

  async function handleLoadSessionReplay() {
    const focusAgentId = agentId.trim();
    const focusSessionId = sessionIdInput.trim();
    if (!focusAgentId || !focusSessionId) {
      setNotice("Set both Agent ID and Session ID before loading replay.");
      return;
    }

    setSessionReplayBusy(true);
    try {
      const replay = await fetchAgentSession(focusAgentId, focusSessionId);
      setSessionReplay(replay);
      setNotice(`Session replay loaded for ${focusSessionId}.`);
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      setSessionReplay(null);
      if (error instanceof LedgerApiError) {
        setNotice(`Session replay failed: ${error.message}`);
      } else {
        setNotice("Session replay failed unexpectedly.");
      }
    } finally {
      setSessionReplayBusy(false);
    }
  }

  async function handleInspectResource(target?: string) {
    const candidate = (target ?? resourceUri).trim();
    if (!candidate) {
      setNotice("Enter a resource URI first.");
      return;
    }

    setResourceBusy(true);
    try {
      const payload = await fetchResourceByUri(candidate);
      const preview = previewJson(payload);
      setResourceUri(candidate);
      setResourceResult(JSON.stringify(payload, null, 2));
      setResourceHistory((current) => [
        {
          uri: candidate,
          preview,
          loadedAt: new Date().toISOString()
        },
        ...current.filter((entry) => entry.uri !== candidate)
      ].slice(0, 6));
      setNotice(`Resource loaded: ${candidate}`);
    } catch (error) {
      if (isAuthFailure(error)) {
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof LedgerApiError) {
        setResourceResult(
          JSON.stringify(
            {
              ok: false,
              error: {
                error_type: error.errorType ?? "RequestError",
                message: error.message,
                suggested_action: error.suggestedAction ?? null
              }
            },
            null,
            2
          )
        );
        setResourceHistory((current) => [
          {
            uri: candidate,
            preview: error.message,
            loadedAt: new Date().toISOString()
          },
          ...current.filter((entry) => entry.uri !== candidate)
        ].slice(0, 6));
        setNotice(`Resource load failed: ${error.message}`);
      } else {
        setNotice("Resource load failed unexpectedly.");
      }
    } finally {
      setResourceBusy(false);
    }
  }

  async function handleSelectApplication(nextApplicationId: string) {
    setApplicationId(nextApplicationId);
    setTemporalCompliance(null);
    setTemporalAsOf("");
    setSessionReplay(null);
    try {
      await refreshFocused(nextApplicationId, agentId);
      setNotice(`Focused control record updated to ${nextApplicationId}.`);
    } catch (error) {
      if (error instanceof LedgerApiError) {
        setNotice(`Unable to load ${nextApplicationId}: ${error.message}`);
      } else {
        setNotice("Unable to update the focused application.");
      }
    }
  }

  async function handleCopyApplicationId(targetApplicationId: string) {
    const candidate = targetApplicationId.trim();
    if (!candidate) {
      setNotice("No application ID is available to copy.");
      return;
    }
    if (!window.isSecureContext || !navigator.clipboard?.writeText) {
      setNotice("Clipboard access is unavailable in this browser context.");
      return;
    }
    try {
      await navigator.clipboard.writeText(candidate);
      setNotice(`Copied application ID: ${candidate}`);
    } catch {
      setNotice("Unable to copy application ID. Copy it manually from the tooltip.");
    }
  }

  if (!authReady) {
    return <div className="auth-shell">Initializing Apex Financial Services control surface...</div>;
  }

  if (!authUser) {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <div className="auth-brand">
            <img className="apex-logo auth-logo" src={apexLogo} alt="Apex Financial Services" />
            <div>
              <p className="brand-overline">Apex Financial Services</p>
              <h1>The Ledger</h1>
              <p className="brand-subtitle">
                Internal governance and evidence platform for multi-agent commercial loan decisions.
              </p>
              <p className="brand-context">
                Global platform. Ethiopia regional deployment (ETB), centrally governed by Apex Financial Services.
              </p>
            </div>
          </div>

          <form className="auth-form" onSubmit={(event) => void handleLogin(event)}>
            <label>
              Username
              <input value={loginUsername} onChange={(event) => setLoginUsername(event.target.value)} />
            </label>
            <label>
              Password
              <input
                type="password"
                value={loginPassword}
                onChange={(event) => setLoginPassword(event.target.value)}
              />
            </label>
            <button className="button primary" type="submit" disabled={busy}>
              {busy ? "Authorizing..." : "Enter The Ledger"}
            </button>
          </form>

          <div className="auth-footnote">
            <span>Authorized internal users only</span>
            <span className="notice-inline">{notice || "JWT and role-scoped controls enforced."}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <div className="page-shell">
        <header className="identity-bar panel">
          <div className="identity-main">
            <img className="apex-logo" src={apexLogo} alt="Apex Financial Services" />
            <div className="identity-copy">
              <p className="brand-overline">Apex Financial Services</p>
              <div className="product-row">
                <h1>The Ledger</h1>
                <span className="product-tag">Internal Governance Platform</span>
              </div>
              <p className="brand-subtitle">
                Governance and oversight for agentic commercial lending.
              </p>
              <p className="brand-context">
                Global platform. Ethiopia regional deployment (ETB), centrally governed by Apex Financial Services.
              </p>
            </div>
          </div>

          <div className="identity-meta">
            <div className="identity-meta-top">
              <div className="badge-row">
                <StatusBadge label="Internal" tone="neutral" />
                <StatusBadge label="RBAC active" tone="ok" />
                <StatusBadge
                  label={`Projection ${projectionSummary.label}`}
                  tone={projectionSummary.tone}
                />
              </div>
              <div className="meta-actions meta-actions-inline">
                <button className="button subtle button-compact" onClick={() => void loadEverything()} disabled={busy}>
                  Refresh
                </button>
                <button className="button button-compact" onClick={() => doLogout("Logged out.")}>
                  Log Out
                </button>
              </div>
            </div>
            <div className="meta-strip">
              <div className="meta-item meta-item-inline">
                <span className="meta-label">System status</span>
                <strong>{projectionSummary.label}</strong>
              </div>
              <div className="meta-item meta-item-inline">
                <span className="meta-label">Last refresh</span>
                <strong>{prettyDate(lastRefresh)}</strong>
              </div>
              <div className="meta-item meta-item-inline">
                <span className="meta-label">Session</span>
                <strong>
                  {authUser.username} <span className="meta-inline">({authUser.role})</span>
                </strong>
              </div>
            </div>
          </div>
        </header>

        <section className="metric-strip">
          <MetricTile
            label="Tracked Applications"
            value={`${applications.length}`}
            detail="Governed in event store"
            tone="neutral"
          />
          <MetricTile
            label="Pipeline State"
            value={primaryPipelineState ? titleize(primaryPipelineState.state) : "No data"}
            detail={
              primaryPipelineState
                ? `${primaryPipelineState.count} application${primaryPipelineState.count === 1 ? "" : "s"}`
                : "Awaiting projection data"
            }
            tone="neutral"
          />
          <MetricTile
            label="Projection Health"
            value={projectionSummary.label}
            detail={projectionSummary.detail}
            tone={projectionSummary.tone}
          />
          <MetricTile label="Integrity" value={integritySummary.status} detail={integritySummary.verifiedWindow} tone={integritySummary.tone} />
          <MetricTile
            label="Compliance Exceptions"
            value={`${openComplianceIssues}`}
            detail={
              openComplianceIssues > 0
                ? "Applications not in cleared posture"
                : "All tracked applications cleared"
            }
            tone={openComplianceIssues > 0 ? "warning" : "ok"}
          />
        </section>

        <AgentPipelineFlow stages={pipelineStages} compact />

        <section className="panel workspace-nav workspace-strip">
          <div className="workspace-strip-main">
            <p className="section-kicker">Workspace</p>
            <h2
              title={
                isOperationsView
                  ? "Operational view for focused applications, command execution, and pipeline progress."
                  : isAuditView
                    ? "Audit view for evidence, verification, and temporal reconstruction."
                    : "System view for projections, MCP resources, and session replay."
              }
            >
              The Ledger Workspace
            </h2>
          </div>
          <div className="workspace-controls">
            <div className="workspace-tabs" role="tablist" aria-label="Ledger views">
              {(["operations", "audit", "system"] as WorkspaceMode[]).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  role="tab"
                  aria-selected={workspaceMode === tab}
                  className={`tab-button ${workspaceMode === tab ? "active" : ""}`}
                  onClick={() => {
                    setWorkspaceMode(tab);
                    if (tab !== "audit") {
                      setAllEvidencePanels(false);
                    }
                  }}
                >
                  {titleize(tab)}
                </button>
              ))}
            </div>
            {isAuditView ? (
              <button
                className="button subtle evidence-toggle"
                type="button"
                onClick={() => setAllEvidencePanels(!allEvidenceExpanded)}
              >
                {allEvidenceExpanded ? "Collapse all evidence" : "Expand all evidence"}
              </button>
            ) : (
              <button
                className="button subtle evidence-toggle workspace-action-placeholder"
                type="button"
                disabled
                tabIndex={-1}
                aria-hidden="true"
              >
                Expand all evidence
              </button>
            )}
          </div>
        </section>

        <FocusedRecordSummary
          application={application}
          onCopyApplicationId={(nextApplicationId) => void handleCopyApplicationId(nextApplicationId)}
        />

        <div className="workspace-grid dashboard-grid">
          <aside className="workspace-column left-column sticky-column">
            <CommandCenterPanel
              busy={busy}
              applicationId={applicationId}
              role={authUser.role}
              toolDefinitions={toolDefinitions}
              selectedTool={selectedTool}
              onToolChange={handleToolChange}
              onRunDemo={() => void handleRunDemo()}
              onPrepareTool={prepareTool}
              hasIntegrityCommand={hasIntegrityCommand}
              onRunCommand={() => void handleRunCommand()}
              selectedToolDefinition={selectedToolDefinition}
              commandPayload={commandPayload}
              onCommandPayloadChange={setCommandPayload}
              commandResult={commandResult}
            />

            <section className="panel panel-support">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Role-Scoped Access</p>
                  <h2>Operator entitlements</h2>
                </div>
              </div>

              <div className="definition-list compact">
                <div>
                  <span>Role</span>
                  <strong>{titleize(authUser.role)}</strong>
                </div>
                <div>
                  <span>Command entitlements</span>
                  <strong>{authUser.allowed_commands.length}</strong>
                </div>
                <div>
                  <span>Auth audit visibility</span>
                  <strong>{canViewAudit ? "Granted" : "Restricted"}</strong>
                </div>
                <div>
                  <span>Active tab</span>
                  <strong>{titleize(workspaceMode)}</strong>
                </div>
              </div>
              <details className="detail-block">
                <summary>Allowed commands</summary>
                <div className="tag-list">
                  {authUser.allowed_commands.map((command) => (
                    <span className="role-chip" key={command}>
                      {command}
                    </span>
                  ))}
                </div>
              </details>
            </section>
          </aside>

          <main className="workspace-column center-column">
            {isOperationsView ? (
              <>
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Application Snapshot</p>
                      <h2>Focused application</h2>
                    </div>
                    <div className="focus-controls">
                      <label>
                        Agent ID
                        <input
                          value={agentId}
                          onChange={(event) => setAgentId(event.target.value)}
                          placeholder="credit-agent-ethi-01"
                        />
                      </label>
                      <button className="button subtle" onClick={() => void refreshFocused()} disabled={busy}>
                        Refresh focus
                      </button>
                    </div>
                  </div>

                  {!application ? (
                    <p className="empty-state">Select an application or run a governed lifecycle.</p>
                  ) : (
                    <div className="snapshot-grid">
                      <div className="snapshot-main">
                        <div className="snapshot-title-row">
                          <div>
                            <span className="meta-label">Application</span>
                            <div className="record-id-row">
                              <h3 title={application.application_id}>{shortId(application.application_id, 8)}</h3>
                              <button
                                type="button"
                                className="button subtle copy-id-button"
                                onClick={() => void handleCopyApplicationId(application.application_id)}
                                title={application.application_id}
                              >
                                Copy ID
                              </button>
                            </div>
                          </div>
                          <StatusBadge label={titleize(application.current_state)} tone={toTone(application.current_state)} />
                        </div>

                        <div className="definition-list compact">
                          <div>
                            <span>Requested (ETB)</span>
                            <strong>{formatMoney(application.requested_amount_usd)}</strong>
                          </div>
                          <div>
                            <span>Recommendation</span>
                            <strong>{titleize(application.decision_recommendation ?? "Pending")}</strong>
                          </div>
                          <div>
                            <span>Final decision</span>
                            <strong>{titleize(application.final_decision ?? "Pending")}</strong>
                          </div>
                          <div>
                            <span>Approved (ETB)</span>
                            <strong>{formatMoney(application.approved_amount_usd)}</strong>
                          </div>
                          <div>
                            <span>Max limit (ETB)</span>
                            <strong>{formatMoney(application.assessed_max_limit_usd)}</strong>
                          </div>
                          <div>
                            <span>Compliance posture</span>
                            <strong>{titleize(application.compliance_status ?? "Not started")}</strong>
                          </div>
                        </div>
                      </div>

                      <div className="snapshot-side">
                        <div className="side-card">
                          <span className="meta-label">Event evidence</span>
                          <strong>{titleize(application.last_event_type)}</strong>
                          <p>Global #{application.last_global_position}</p>
                        </div>
                        <div className="side-card">
                          <span className="meta-label">Projection update</span>
                          <strong>{prettyDate(application.updated_at)}</strong>
                          <p>State derived from immutable events.</p>
                        </div>
                        <div className="side-card">
                          <span className="meta-label">Compliance posture</span>
                          <strong>{titleize(readString(currentComplianceSnapshot.compliance_status) ?? "Not available")}</strong>
                          <p>Regulation set {readString(currentComplianceSnapshot.regulation_set_version) ?? "Not set"}</p>
                        </div>
                      </div>
                    </div>
                  )}
                </section>

                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Tracked Pipeline</p>
                      <h2>Application register</h2>
                    </div>
                    <p className="section-note">{visibleApplicationRows.length} rows in view</p>
                  </div>

                  <div className="application-list">
                    <div className="list-head">
                      <span>Application</span>
                      <span>Pipeline</span>
                      <span>Compliance</span>
                    </div>
                    {visibleApplicationRows.map((item) => {
                      const isFocused = application?.application_id === item.application_id;
                      const isDimmed = Boolean(application?.application_id) && !isFocused;
                      return (
                        <button
                          key={item.application_id}
                          className={`application-row ${isFocused ? "active" : ""} ${isDimmed ? "dimmed" : ""}`}
                          onClick={() => void handleSelectApplication(item.application_id)}
                        >
                          <div>
                            <strong title={item.application_id}>{shortId(item.application_id, 8)}</strong>
                            <span>Commercial application</span>
                          </div>
                          <div>
                            <strong>{titleize(item.current_state)}</strong>
                            <span>{prettyDate(item.updated_at)}</span>
                          </div>
                          <div className="row-end">
                            <StatusBadge
                              label={titleize(item.compliance_status ?? "Not started")}
                              tone={toTone(item.compliance_status)}
                            />
                            <span className="row-muted">{isFocused ? "Focused" : "Select"}</span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </section>
              </>
            ) : null}

            {isAuditView ? (
              <>
                <AuditPhaseTimeline
                  timelineGroups={timelineGroups}
                  collapsedTimelineGroups={collapsedTimelineGroups}
                  onToggleGroup={(key, nextValue) =>
                    setCollapsedTimelineGroups((current) => ({ ...current, [key]: nextValue }))
                  }
                  eventTypeFilter={eventTypeFilter}
                  onEventTypeFilterChange={setEventTypeFilter}
                  lineageEventTypes={lineageEventTypes}
                  eventActorFilter={eventActorFilter}
                  onEventActorFilterChange={setEventActorFilter}
                  lineageActors={lineageActors}
                  eventTimeRange={eventTimeRange}
                  onEventTimeRangeChange={setEventTimeRange}
                  onResetFilters={() => {
                    setEventTypeFilter("all");
                    setEventActorFilter("all");
                    setEventTimeRange("all");
                  }}
                  filteredCount={filteredLineage.length}
                  applicationId={applicationId}
                  showMetadata={evidencePanels.lineage}
                  onToggleMetadata={() => toggleEvidencePanel("lineage")}
                />
                <EventCoveragePanel
                  applicationId={applicationId.trim()}
                  observedEventTypes={lineageEventTypes}
                />
              </>
            ) : null}

            {isSystemView ? (
              <>
                <ProjectionHealthPanel rows={projectionStatusRows} />
                <MCPResourcePanel
                  resourceUri={resourceUri}
                  onResourceUriChange={setResourceUri}
                  onInspectResource={(target) => void handleInspectResource(target)}
                  resourceBusy={resourceBusy}
                  resourceQuickPicks={resourceQuickPicks}
                  resourceHistory={resourceHistory}
                  resourceDefinitions={resourceDefinitions}
                  resourceResult={resourceResult}
                />
              </>
            ) : null}
          </main>

          <aside className="workspace-column right-column">
            {isOperationsView ? (
              <>
                <section className="panel panel-support">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Compliance Posture</p>
                      <h2>Current status</h2>
                    </div>
                  </div>

                  {compliance ? (
                    <div className="definition-list compact">
                      <div>
                        <span>Status</span>
                        <strong>{titleize(readString(currentComplianceSnapshot.compliance_status) ?? "Unknown")}</strong>
                      </div>
                      <div>
                        <span>Regulation set</span>
                        <strong>{readString(currentComplianceSnapshot.regulation_set_version) ?? "Unavailable"}</strong>
                      </div>
                      <div>
                        <span>Failed checks</span>
                        <strong>{currentFailedChecks}</strong>
                      </div>
                      <div>
                        <span>Projection updated</span>
                        <strong>{prettyDate(readString(currentComplianceSnapshot.updated_at))}</strong>
                      </div>
                    </div>
                  ) : (
                    <p className="empty-state">No compliance view for the current application.</p>
                  )}
                </section>

                <IntegrityStatusCard
                  title="Verification status"
                  status={integritySummary.status}
                  tone={integritySummary.tone}
                  verified={integrityCoverage.verified}
                  total={integrityCoverage.total || 0}
                  evidenceHash={integritySummary.evidenceHash}
                  verifiedAt={integritySummary.verifiedAt}
                  verifiedWindow={integritySummary.verifiedWindow}
                  tamperState={integritySummary.tamperState}
                  hasIntegrityCommand={hasIntegrityCommand}
                  busy={busy}
                  onRunVerification={() => void handleRunVerification()}
                />
              </>
            ) : null}

            {isAuditView ? (
              <>
                <IntegrityStatusCard
                  title="Audit evidence"
                  status={integritySummary.status}
                  tone={integritySummary.tone}
                  verified={integrityCoverage.verified}
                  total={integrityCoverage.total || 0}
                  evidenceHash={integritySummary.evidenceHash}
                  verifiedAt={integritySummary.verifiedAt}
                  verifiedWindow={integritySummary.verifiedWindow}
                  tamperState={integritySummary.tamperState}
                  hasIntegrityCommand={hasIntegrityCommand}
                  busy={busy}
                  onRunVerification={() => void handleRunVerification()}
                  showEvidenceToggle
                  evidenceExpanded={evidencePanels.integrity}
                  onToggleEvidence={() => toggleEvidencePanel("integrity")}
                />

                <section className="panel temporal-panel">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Temporal Replay</p>
                      <h2>Regulatory reconstruction</h2>
                    </div>
                  </div>

                  <div className="temporal-toolbar">
                    <div className="temporal-inputs">
                      <label>
                        Application
                        <input
                          value={applicationId}
                          onChange={(event) => setApplicationId(event.target.value)}
                          placeholder="app-..."
                        />
                      </label>
                      <label>
                        As of
                        <input
                          type="datetime-local"
                          value={temporalAsOf}
                          onChange={(event) => setTemporalAsOf(event.target.value)}
                        />
                      </label>
                    </div>
                    <button
                      className="button temporal-run"
                      onClick={() => void handleTemporalInspect()}
                      disabled={temporalBusy}
                    >
                      {temporalBusy ? "Reconstructing..." : "Run reconstruction"}
                    </button>
                  </div>

                  <div className="checkpoint-list">
                    {applicationLineage.length === 0 ? (
                      <p className="empty-state">No checkpoints available for this application.</p>
                    ) : (
                      applicationLineage.slice(-replayCheckpointCount).map((item) => (
                        <button
                          className="checkpoint-button"
                          key={item.key}
                          onClick={() => setTemporalAsOf(toDateTimeLocalValue(item.recordedAt))}
                        >
                          <span className="checkpoint-event" title={titleize(item.eventType)}>
                            {titleize(item.eventType)}
                          </span>
                          <span className="checkpoint-time">{prettyDateCompact(item.recordedAt)}</span>
                        </button>
                      ))
                    )}
                  </div>

                  <div className="temporal-summary">
                    <div className="comparison-grid">
                      <div>
                        <span>Current posture</span>
                        <strong>
                          {titleize(
                            application?.compliance_status ??
                              readString(currentComplianceSnapshot.compliance_status) ??
                              "NOT_STARTED"
                          )}
                        </strong>
                      </div>
                      <div>
                        <span>Historical posture</span>
                        <strong>{titleize(readString(temporalSnapshot.compliance_status) ?? "Not loaded")}</strong>
                      </div>
                      <div>
                        <span>Failed checks now</span>
                        <strong>{currentFailedChecks}</strong>
                      </div>
                      <div>
                        <span>Failed checks then</span>
                        <strong>{temporalFailedChecks}</strong>
                      </div>
                    </div>
                    <strong className="temporal-delta">{temporalDelta}</strong>
                    <div className="definition-list compact">
                      <div>
                        <span>As of</span>
                        <strong>{temporalAsOf ? prettyDate(fromDateTimeLocalValue(temporalAsOf)) : "Not set"}</strong>
                      </div>
                      <div>
                        <span>Application</span>
                        <strong>{applicationId || "Not set"}</strong>
                      </div>
                    </div>
                  </div>
                </section>

                <section className="panel panel-raw-activity">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Recent Activity</p>
                      <h2>Latest append stream</h2>
                    </div>
                    <div className="panel-actions">
                      <p className="section-note">Raw append order from the ledger.</p>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => toggleEvidencePanel("activity")}
                      >
                        {evidencePanels.activity ? "Hide evidence" : "Show evidence"}
                      </button>
                    </div>
                  </div>

                  {visibleActivity.length === 0 ? (
                    <p className="empty-state">No ledger events available for this scope.</p>
                  ) : (
                    <div className="activity-table">
                      <div className="list-head">
                        <span>Event</span>
                        <span>Application</span>
                        <span>Lane</span>
                      </div>
                      {visibleActivity.map((event) => {
                        const payload = readRecord(event.payload);
                        const metadata = readRecord(event.metadata);
                        const rowApplicationId = readString(payload.application_id);
                        return (
                          <article className="event-row-wrap" key={event.event_id}>
                            <button
                              className="activity-row"
                              onClick={() => {
                                const nextApplicationId = readString(payload.application_id);
                                if (nextApplicationId) {
                                  void handleSelectApplication(nextApplicationId);
                                }
                              }}
                            >
                              <div>
                                <strong title={titleize(event.event_type)}>{titleize(event.event_type)}</strong>
                                <span title={actorForEvent(event.event_type, payload, metadata)}>
                                  {actorForEvent(event.event_type, payload, metadata)}
                                </span>
                              </div>
                              <div>
                                <strong title={rowApplicationId ?? "Unlinked event"}>
                                  {!rowApplicationId ? "Unlinked event" : shortId(rowApplicationId, 8)}
                                </strong>
                                <span title={prettyDate(event.recorded_at)}>{prettyDateCompact(event.recorded_at)}</span>
                              </div>
                              <div>
                                <strong title={titleize(laneForEvent(event.event_type))}>
                                  {titleize(laneForEvent(event.event_type))}
                                </strong>
                                <span>Pos #{event.global_position}</span>
                              </div>
                            </button>
                            {evidencePanels.activity ? (
                              <div className="evidence-inline">
                                <span className="meta-label">Event IDs</span>
                                <div className="lineage-evidence">
                                  <span>Application {rowApplicationId ?? "Unlinked event"}</span>
                                  <span>Stream {event.stream_id}</span>
                                  <span>Event {shortId(event.event_id)}</span>
                                  <span>Global #{event.global_position}</span>
                                </div>
                              </div>
                            ) : null}
                          </article>
                        );
                      })}
                    </div>
                  )}
                </section>

                <section className="panel panel-support">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Compliance Posture</p>
                      <h2>Current regulatory state</h2>
                    </div>
                    <div className="panel-actions">
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => toggleEvidencePanel("compliance")}
                      >
                        {evidencePanels.compliance ? "Hide evidence" : "Show evidence"}
                      </button>
                    </div>
                  </div>

                  {compliance ? (
                    <>
                      <div className="definition-list compact">
                        <div>
                          <span>Status</span>
                          <strong>{titleize(readString(currentComplianceSnapshot.compliance_status) ?? "Unknown")}</strong>
                        </div>
                        <div>
                          <span>Regulation set</span>
                          <strong>{readString(currentComplianceSnapshot.regulation_set_version) ?? "Unavailable"}</strong>
                        </div>
                        <div>
                          <span>Mandatory checks</span>
                          <strong>{readStringList(currentComplianceSnapshot.mandatory_checks).length}</strong>
                        </div>
                        <div>
                          <span>Passed checks</span>
                          <strong>{readStringList(currentComplianceSnapshot.passed_checks).length}</strong>
                        </div>
                        <div>
                          <span>Failed checks</span>
                          <strong>{currentFailedChecks}</strong>
                        </div>
                        <div>
                          <span>Projection updated</span>
                          <strong>{prettyDate(readString(currentComplianceSnapshot.updated_at))}</strong>
                        </div>
                      </div>

                      {evidencePanels.compliance ? (
                        <div className="evidence-block">
                          <span className="meta-label">Compliance event evidence</span>
                          <div className="mini-timeline">
                            {compliance.timeline.slice(-6).map((entry) => {
                              const evidenceStatus = complianceEvidenceStatus(entry);
                              return (
                                <article className="mini-timeline-row" key={`${entry.global_position}-${entry.event_type}`}>
                                  <div>
                                    <strong>{titleize(entry.event_type)}</strong>
                                    <span>
                                      {entry.rule_id
                                        ? `${entry.rule_id} · ${entry.rule_version ?? "active"}`
                                        : "Policy evaluation"}
                                    </span>
                                  </div>
                                  <div>
                                    <strong className={`tone-${evidenceStatus.tone}`}>{evidenceStatus.label}</strong>
                                    <span>{prettyDate(entry.recorded_at)}</span>
                                  </div>
                                </article>
                              );
                            })}
                          </div>
                        </div>
                      ) : null}
                    </>
                  ) : (
                    <p className="empty-state">No compliance view for the current application.</p>
                  )}
                </section>

                <section className="panel panel-support">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Audit Access</p>
                      <h2>Authentication evidence</h2>
                    </div>
                    <p className="section-note">Latest 10 policy outcomes</p>
                  </div>

                  {canViewAudit ? (
                    <div className="activity-table compact">
                      <div className="list-head">
                        <span>Action</span>
                        <span>Outcome</span>
                        <span>Recorded</span>
                      </div>
                      {auditRows.slice(0, 10).map((row) => {
                        const outcome = presentAuditOutcome(row);
                        return (
                          <div className="activity-row static audit-row" key={row.audit_id}>
                            <div>
                              <strong>{presentAuditAction(row.action)}</strong>
                              <span>{row.username ?? "anonymous"}</span>
                            </div>
                            <div>
                              <StatusBadge label={outcome.label} tone={outcome.tone} />
                              <span>{outcome.helper}</span>
                            </div>
                            <div>
                              <strong>{prettyDate(row.created_at)}</strong>
                              <span>{row.ip_address ?? "IP unavailable"}</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <p className="empty-state">Role-scoped access: auth audit log restricted.</p>
                  )}
                </section>
              </>
            ) : null}

            {isSystemView ? (
              <>
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Agent Session Resource</p>
                      <h2>Session replay</h2>
                    </div>
                    <p className="section-note">Replay a selected agent session from the ledger.</p>
                  </div>

                  <div className="definition-list compact">
                    <div>
                      <span>Agent</span>
                      <strong>{agentId || "Not set"}</strong>
                    </div>
                    <div>
                      <span>Discovered sessions</span>
                      <strong>{sessionCandidates.length}</strong>
                    </div>
                  </div>

                  <label>
                    Session ID
                    <input
                      value={sessionIdInput}
                      onChange={(event) => setSessionIdInput(event.target.value)}
                      placeholder="session-..."
                      list="session-candidate-list"
                    />
                    <datalist id="session-candidate-list">
                      {sessionCandidates.map((candidate) => (
                        <option key={candidate} value={candidate} />
                      ))}
                    </datalist>
                  </label>

                  <div className="quick-actions">
                    <button className="button" onClick={() => void handleLoadSessionReplay()} disabled={sessionReplayBusy}>
                      {sessionReplayBusy ? "Loading session..." : "Load session replay"}
                    </button>
                    {sessionCandidates.length > 0 ? (
                      <button
                        className="button subtle"
                        onClick={() => setSessionIdInput(sessionCandidates[0])}
                        type="button"
                      >
                        Use latest discovered session
                      </button>
                    ) : null}
                  </div>

                  {!sessionReplay ? (
                    <p className="empty-state">Choose a session to load replay evidence.</p>
                  ) : (
                    <>
                      <div className="definition-list compact session-summary">
                        <div>
                          <span>Loaded session</span>
                          <strong>{sessionIdInput || "Not set"}</strong>
                        </div>
                        <div>
                          <span>Stream</span>
                          <strong>{sessionReplay.stream_id}</strong>
                        </div>
                        <div>
                          <span>Events</span>
                          <strong>{sessionReplay.events.length}</strong>
                        </div>
                      </div>
                      <div className="activity-table compact">
                        <div className="list-head">
                          <span>Event</span>
                          <span>Position</span>
                          <span>Recorded</span>
                        </div>
                        {[...sessionReplay.events]
                          .slice(-8)
                          .reverse()
                          .map((event) => (
                            <div className="activity-row static" key={event.event_id}>
                              <div>
                                <strong>{titleize(event.event_type)}</strong>
                                <span>{actorForEvent(event.event_type, readRecord(event.payload), readRecord(event.metadata))}</span>
                              </div>
                              <div>
                                <strong>#{event.stream_position}</strong>
                                <span>Global #{event.global_position}</span>
                              </div>
                              <div>
                                <strong>{prettyDate(event.recorded_at)}</strong>
                                <span>{shortId(event.event_id)}</span>
                              </div>
                            </div>
                          ))}
                      </div>
                    </>
                  )}
                </section>

                <section className="panel panel-support">
                  <div className="panel-heading">
                    <div>
                      <p className="section-kicker">Model Oversight</p>
                      <h2>Agent performance</h2>
                    </div>
                  </div>

                  {!agentPerformance || agentPerformance.models.length === 0 ? (
                    <p className="empty-state">No model projection for the current agent.</p>
                  ) : (
                    <div className="activity-table compact">
                      {agentPerformance.models.map((model) => (
                        <div className="activity-row static" key={`${model.agent_id}-${model.model_version}`}>
                          <div>
                            <strong>{model.model_version}</strong>
                            <span>{model.agent_id}</span>
                          </div>
                          <div>
                            <strong>{model.analyses_completed} analyses</strong>
                            <span>{model.fraud_screenings_completed} fraud screenings</span>
                          </div>
                          <div>
                            <strong>{model.avg_confidence_score.toFixed(2)}</strong>
                            <span>{model.decisions_recorded} decisions</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </>
            ) : null}
          </aside>
        </div>

        {noticeToastVisible ? (
          <div className="notice-toast" role="status" aria-live="polite">
            <span className="notice-label">System notice</span>
            <strong>{notice || "The Ledger is synchronized and ready for review."}</strong>
            <button
              type="button"
              className="toast-close"
              aria-label="Dismiss system notice"
              onClick={() => setNoticeToastVisible(false)}
            >
              Close
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
