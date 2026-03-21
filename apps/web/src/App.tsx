import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import {
  bootstrapDemo,
  clearStoredToken,
  fetchAgentPerformance,
  fetchApplication,
  fetchApplicationAuditTrail,
  fetchApplications,
  fetchApplicationStates,
  fetchAuthAudit,
  fetchCompliance,
  fetchLedgerHealth,
  fetchMe,
  fetchRecentEvents,
  fetchTools,
  LedgerApiError,
  login,
  runCommand,
  type AuthAuditRow,
  type MeResponse
} from "./api";
import type {
  AgentPerformance,
  AppStateCount,
  ApplicationSummary,
  ComplianceTimelineEvent,
  ComplianceView,
  LedgerHealth,
  RecentEvent,
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
    eventTypes: ["FraudScreeningCompleted"]
  },
  {
    name: "ComplianceAgent",
    role: "Regulation and policy checks",
    eventTypes: ["ComplianceCheckRequested", "ComplianceRulePassed", "ComplianceRuleFailed"]
  },
  {
    name: "DecisionOrchestrator",
    role: "Machine recommendation synthesis",
    eventTypes: ["DecisionGenerated"]
  }
] as const;

type Tone = "ok" | "warning" | "critical" | "neutral";

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
  label: string;
  helper: string;
  isSynced: boolean;
};

type WorkspaceMode = "operations" | "audit";
type EvidencePanelKey = "integrity" | "snapshot" | "lineage" | "activity" | "compliance";

const EVIDENCE_PANEL_KEYS: EvidencePanelKey[] = [
  "integrity",
  "snapshot",
  "lineage",
  "activity",
  "compliance"
];

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
      run_compliance_check: "Run Compliance Check",
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
  if (row.success) {
    return { label: "Success", tone: "ok", helper: "Authorized action" };
  }
  if (reason === "role_forbidden") {
    return { label: "Policy denied", tone: "warning", helper: "Blocked by role-scoped access" };
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

function actorForEvent(eventType: string, payload: Record<string, unknown>, metadata: Record<string, unknown>): string {
  const actorId = readString(metadata.actor_id);
  if (actorId) {
    return actorId;
  }
  if (eventType.startsWith("Compliance")) {
    return "ComplianceAgent";
  }
  if (eventType.startsWith("Credit")) {
    return readString(payload.agent_id) ?? "CreditAnalysis";
  }
  if (eventType.startsWith("Fraud")) {
    return readString(payload.agent_id) ?? "FraudDetection";
  }
  if (eventType === "DecisionGenerated") {
    return readString(payload.orchestrator_agent_id) ?? "DecisionOrchestrator";
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
  if (eventType.startsWith("Credit")) {
    return "CreditAnalysis";
  }
  if (eventType.startsWith("Fraud")) {
    return "FraudDetection";
  }
  if (eventType.startsWith("Compliance")) {
    return "ComplianceAgent";
  }
  if (eventType === "DecisionGenerated") {
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
  if (eventType === "DecisionGenerated") {
    return "Machine checkpoint";
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
  if (eventType === "AgentContextLoaded") {
    return `Agent session loaded with replay context from ledger position ${readNumber(payload.event_replay_from_position) ?? 0}.`;
  }
  if (eventType === "CreditAnalysisRequested") {
    return "Credit analysis requested after intake and document processing.";
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
  if (eventType === "DecisionGenerated") {
    return `DecisionOrchestrator recommended ${readString(payload.recommendation) ?? "review"} with confidence ${readNumber(payload.confidence_score)?.toFixed(2) ?? "n/a"}.`;
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
    <article className={`metric-tile tone-${props.tone ?? "neutral"}`}>
      <span className="metric-label">{props.label}</span>
      <strong className="metric-value">{props.value}</strong>
      <span className="metric-detail">{props.detail}</span>
    </article>
  );
}

function StatusBadge(props: { label: string; tone?: Tone }): ReactNode {
  return <span className={`status-badge tone-${props.tone ?? "neutral"}`}>{props.label}</span>;
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
        label: "Synced",
        helper: "No backlog; projection timer is elevated.",
        isSynced: true
      };
    }
    return {
      tone: "ok",
      label: "Synced",
      helper: "Projection is up to date.",
      isSynced: true
    };
  }

  if (lag.events_behind <= 5) {
    return {
      tone: "warning",
      label: "Backlog",
      helper: `${lag.events_behind} events pending projection.`,
      isSynced: false
    };
  }

  return {
    tone: "critical",
    label: "Backlog",
    helper: `${lag.events_behind} events pending projection.`,
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
  const [integrityTrail, setIntegrityTrail] = useState<RecentEvent[]>([]);
  const [auditRows, setAuditRows] = useState<AuthAuditRow[]>([]);

  const [busy, setBusy] = useState<boolean>(false);
  const [temporalBusy, setTemporalBusy] = useState<boolean>(false);
  const [notice, setNotice] = useState<string>("");
  const [lastRefresh, setLastRefresh] = useState<string>(new Date().toISOString());
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("operations");
  const [showAllLineage, setShowAllLineage] = useState<boolean>(false);
  const [showAllActivity, setShowAllActivity] = useState<boolean>(false);
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
    const rows = recentEvents
      .filter((event) => eventMatchesApplication(event, applicationId))
      .map(buildLineageItemFromRecent);

    const complianceRows = (compliance?.timeline ?? []).map(buildLineageItemFromCompliance);
    const deduped = new Map<string, LineageItem>();

    [...rows, ...complianceRows].forEach((item) => {
      deduped.set(item.key, item);
    });

    return Array.from(deduped.values()).sort((left, right) => left.globalPosition - right.globalPosition);
  }, [applicationId, compliance, recentEvents]);

  const integritySummary = useMemo(() => {
    const latest = integrityTrail[integrityTrail.length - 1] ?? null;
    const verifiedCount = readNumber(latest?.payload.events_verified_count);
    const integrityHash = readString(latest?.payload.integrity_hash);
    const tone = latest ? "ok" : applicationId ? "warning" : "neutral";

    return {
      tone: tone as Tone,
      status: latest ? "Verified" : applicationId ? "Pending verification" : "No focal application",
      tamperState: latest
        ? "No tamper evidence recorded in the latest chain run."
        : "Not yet evaluated for this focus. Run integrity check to attest the chain.",
      verifiedWindow: latest
        ? `${verifiedCount ?? 0} events sealed in latest verification`
        : "No verification window recorded",
      evidenceHash: integrityHash ? shortId(integrityHash, 8) : "Unavailable",
      verifiedAt: latest ? prettyDate(latest.recorded_at) : "Not yet verified"
    };
  }, [applicationId, integrityTrail]);

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
    return OPERATING_FABRIC.map((agent) => {
      const observedEvent = recentEvents.find((event) =>
        agent.eventTypes.some((eventType) => eventType === event.event_type)
      );
      return {
        ...agent,
        status: observedEvent ? "Live" : "Waiting",
        latestEvent: observedEvent ? titleize(observedEvent.event_type) : "No event",
        latestAt: observedEvent ? prettyDate(observedEvent.recorded_at) : "No timestamp",
        tone: observedEvent ? ("ok" as Tone) : ("neutral" as Tone)
      };
    });
  }, [recentEvents]);

  const currentComplianceSnapshot = readRecord(compliance?.snapshot);
  const temporalSnapshot = readRecord(temporalCompliance?.snapshot);
  const currentFailedChecks = countFailedChecks(currentComplianceSnapshot);
  const temporalFailedChecks = countFailedChecks(temporalSnapshot);
  const hasIntegrityCommand = authUser?.allowed_commands.includes("run_integrity_check") ?? false;
  const primaryPipelineState = states[0];
  const isOperationsView = workspaceMode === "operations";
  const isAuditView = workspaceMode === "audit";
  const allEvidenceExpanded = useMemo(() => {
    return EVIDENCE_PANEL_KEYS.every((panel) => evidencePanels[panel]);
  }, [evidencePanels]);
  const lineageDefaultCount = 4;
  const activityDefaultCount = 5;
  const activityMaxCount = 16;
  const activityWindow = recentEvents.slice(0, activityMaxCount);
  const lineageTotalCount = applicationLineage.length;
  const activityTotalCount = activityWindow.length;
  const visibleLineage =
    isOperationsView
      ? showAllLineage
        ? applicationLineage
        : applicationLineage.slice(-lineageDefaultCount)
      : applicationLineage;
  const visibleActivity =
    isOperationsView
      ? showAllActivity
        ? activityWindow
        : activityWindow.slice(0, activityDefaultCount)
      : activityWindow;
  const applicationRegisterLimit = isOperationsView ? 6 : 12;
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
    setCommandResult("");
    setAuditRows([]);
    setIntegrityTrail([]);
    setTemporalCompliance(null);
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
                setCompliance(null);
                setTemporalCompliance(null);
                setIntegrityTrail([]);
                return;
              }
              throw error;
            }
            setApplication(nextApplication);

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
    const [nextStates, nextApps, nextHealth, nextEvents, nextTools] = await Promise.all([
      fetchApplicationStates(),
      fetchApplications(40),
      fetchLedgerHealth(),
      fetchRecentEvents(60),
      fetchTools()
    ]);

    setStates(nextStates);
    setApplications(nextApps);
    setHealth(nextHealth);
    setRecentEvents(nextEvents);
    setToolDefinitions(nextTools);

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
    setShowAllLineage(false);
    setShowAllActivity(false);
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
    setShowAllLineage(false);
    setShowAllActivity(false);
    if (workspaceMode === "operations") {
      setAllEvidencePanels(false);
    }
  }, [applicationId, setAllEvidencePanels, workspaceMode]);

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
    setNotice("Running governed lifecycle scenario...");
    try {
      const result = await bootstrapDemo();
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
    setNotice(`Submitting ${selectedTool} to the write-side command surface...`);

    try {
      const parsed = JSON.parse(commandPayload) as Record<string, unknown>;
      const result = await runCommand(selectedTool, parsed);
      setCommandResult(JSON.stringify(result, null, 2));

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
        doLogout("Session expired. Please login again.");
        return;
      }
      if (error instanceof SyntaxError) {
        setNotice("Invalid JSON in command payload.");
      } else if (error instanceof LedgerApiError) {
        setNotice(
          `${error.message}${error.suggestedAction ? ` | Try: ${error.suggestedAction}` : ""}`
        );
      } else {
        setNotice("Command execution failed unexpectedly.");
      }
    } finally {
      setBusy(false);
    }
  }

  function handleToolChange(nextTool: string) {
    setSelectedTool(nextTool);
    setCommandPayload(JSON.stringify(commandSeed(nextTool, applicationId, authUser?.role), null, 2));
  }

  function prepareTool(toolName: string, overrides?: Record<string, unknown>) {
    const nextPayload = { ...commandSeed(toolName, applicationId, authUser?.role), ...overrides };
    setSelectedTool(toolName);
    setCommandPayload(JSON.stringify(nextPayload, null, 2));
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

  async function handleSelectApplication(nextApplicationId: string) {
    setApplicationId(nextApplicationId);
    setTemporalCompliance(null);
    setTemporalAsOf("");
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
            <div className="badge-row">
              <StatusBadge label="Internal" tone="neutral" />
              <StatusBadge label="RBAC active" tone="ok" />
              <StatusBadge
                label={`Projection ${projectionSummary.label}`}
                tone={projectionSummary.tone}
              />
            </div>
            <div className="meta-grid">
              <div className="meta-item">
                <span className="meta-label">Last refresh</span>
                <strong>{prettyDate(lastRefresh)}</strong>
              </div>
              <div className="meta-item">
                <span className="meta-label">Session</span>
                <strong>
                  {authUser.username} <span className="meta-inline">({authUser.role})</span>
                </strong>
              </div>
              <div className="meta-actions">
                <button className="button subtle" onClick={() => void loadEverything()} disabled={busy}>
                  Refresh
                </button>
                <button className="button" onClick={() => doLogout("Logged out.")}>
                  Log Out
                </button>
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

        <section className="panel workspace-nav">
          <div>
            <p className="section-kicker">Workspace</p>
            <h2>The Ledger Workspace</h2>
            <p className="section-note">
              {workspaceMode === "operations"
                ? "Operations focus active. Switch to Audit Depth for full evidence fields."
                : "Audit depth active. Full evidence fields and metadata are visible."}
            </p>
          </div>
          <div className="detail-actions">
            <div className="detail-level">
              <span className="detail-label">Audit details</span>
              <button
                className={`toggle-btn ${isAuditView ? "active" : ""}`}
                onClick={() => {
                  setAllEvidencePanels(false);
                  setWorkspaceMode(isAuditView ? "operations" : "audit");
                }}
                type="button"
                aria-pressed={isAuditView}
              >
                {isAuditView ? "On" : "Off"}
              </button>
            </div>
            {isAuditView ? (
              <button
                className="button subtle evidence-toggle"
                type="button"
                onClick={() => setAllEvidencePanels(!allEvidenceExpanded)}
              >
                {allEvidenceExpanded ? "Collapse all evidence" : "Expand all evidence"}
              </button>
            ) : null}
          </div>
        </section>

        <section className="panel operating-model">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">Decisioning Fabric</p>
              <h2>Agent workflow</h2>
            </div>
            <p className="section-note">Machine flow plus binding human decision.</p>
          </div>

          <div className="fabric-rail">
            {fabricRows.map((agent) => (
              <article className="fabric-step" key={agent.name}>
                <div className="fabric-header">
                  <strong>{agent.name}</strong>
                  <StatusBadge label={agent.status} tone={agent.tone} />
                </div>
                <span className="fabric-role">{agent.role}</span>
                <div className="fabric-meta">
                  <span>{agent.latestEvent}</span>
                  <time>{agent.latestAt}</time>
                </div>
              </article>
            ))}
            <article className="fabric-step human-card">
              <div className="fabric-header">
                <strong>Human Loan Officer</strong>
                <StatusBadge
                  label={application?.final_decision ? "Complete" : "Pending"}
                  tone={application?.final_decision ? "ok" : "warning"}
                />
              </div>
              <span className="fabric-role">Final binding decision</span>
              <div className="fabric-meta">
                <span>{application?.final_decision ? titleize(application.final_decision) : "Awaiting officer review"}</span>
                <time>{application?.updated_at ? prettyDate(application.updated_at) : "No timestamp"}</time>
              </div>
            </article>
          </div>
        </section>

        <div className="workspace-grid">
          <aside className="workspace-column left-column">
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Scenario Runner</p>
                  <h2>Command center</h2>
                </div>
                <p className="section-note">Run controlled ledger commands.</p>
              </div>

              <div className="quick-actions">
                <button className="button primary" onClick={() => void handleRunDemo()} disabled={busy}>
                  {busy ? "Working..." : "Run governed lifecycle"}
                </button>
                <button
                  className="button subtle"
                  onClick={() =>
                    prepareTool("generate_decision", {
                      application_id: applicationId || "app-addis-001"
                    })
                  }
                >
                  Queue decision command
                </button>
                {hasIntegrityCommand ? (
                  <button
                    className="button subtle"
                    onClick={() =>
                      prepareTool("run_integrity_check", {
                      entity_type: "application",
                      entity_id: applicationId || "app-addis-001",
                      role: authUser.role
                    })
                    }
                  >
                    Queue integrity check
                  </button>
                ) : null}
              </div>

              <label>
                Command
                <select value={selectedTool} onChange={(event) => handleToolChange(event.target.value)}>
                  {toolDefinitions.map((tool) => (
                    <option value={tool.name} key={tool.name}>
                      {tool.name}
                    </option>
                  ))}
                </select>
              </label>
              <button className="button" onClick={() => void handleRunCommand()} disabled={busy || !toolDefinitions.length}>
                Execute command
              </button>
              <p className="field-note">{conciseDescription(selectedToolDefinition?.description)}</p>
              <details className="detail-inline">
                <summary>Command guidance</summary>
                <p className="field-note">
                  {selectedToolDefinition?.description ?? "No commands available for this role."}
                </p>
              </details>
              <details className="detail-block">
                <summary>Payload editor</summary>
                <label>
                  JSON payload
                  <textarea
                    className="code-area"
                    value={commandPayload}
                    onChange={(event) => setCommandPayload(event.target.value)}
                    rows={10}
                  />
                </label>
              </details>
              <details className="detail-block">
                <summary>Command receipt</summary>
                <pre className="result-box">
                  {commandResult || "Command result appears here after execution."}
                </pre>
              </details>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Role-Scoped Access</p>
                  <h2>{isOperationsView ? "Role summary" : "Permissions summary"}</h2>
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
              </div>
              <details className="detail-block">
                <summary>{isOperationsView ? "Role commands" : "Allowed commands"}</summary>
                <div className="tag-list">
                  {authUser.allowed_commands.map((command) => (
                    <span className="role-chip" key={command}>
                      {command}
                    </span>
                  ))}
                </div>
              </details>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Integrity Verification</p>
                  <h2>{isOperationsView ? "Verification status" : "Evidence posture"}</h2>
                </div>
                <div className="panel-actions">
                  <StatusBadge label={integritySummary.status} tone={integritySummary.tone} />
                  {isAuditView ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => toggleEvidencePanel("integrity")}
                    >
                      {evidencePanels.integrity ? "Hide evidence" : "Expand evidence"}
                    </button>
                  ) : null}
                </div>
              </div>

              <div className="definition-list">
                <div>
                  <span>Verification window</span>
                  <strong>{integritySummary.verifiedWindow}</strong>
                </div>
                <div>
                  <span>Latest verification</span>
                  <strong>{integritySummary.verifiedAt}</strong>
                </div>
                <div>
                  <span>Evidence hash</span>
                  <strong>{integritySummary.evidenceHash}</strong>
                </div>
              </div>
              {isAuditView && evidencePanels.integrity ? (
                <div className="evidence-inline">
                  <span className="meta-label">Tamper state</span>
                  <p className="field-note">{integritySummary.tamperState}</p>
                </div>
              ) : null}
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Temporal Replay</p>
                  <h2>{isOperationsView ? "Reconstruction quick view" : "Regulatory reconstruction"}</h2>
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
                    <span>Current</span>
                    <strong>
                      {titleize(
                        application?.compliance_status ??
                          readString(currentComplianceSnapshot.compliance_status) ??
                          "NOT_STARTED"
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Historical</span>
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
          </aside>

          <main className="workspace-column center-column">
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
                  {isAuditView ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => toggleEvidencePanel("snapshot")}
                    >
                      {evidencePanels.snapshot ? "Hide evidence" : "Expand evidence"}
                    </button>
                  ) : null}
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
                        <h3>{application.application_id}</h3>
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
                    {isAuditView && evidencePanels.snapshot ? (
                      <div className="evidence-block">
                        <span className="meta-label">Event evidence</span>
                        <div className="definition-list compact">
                          <div>
                            <span>Last event</span>
                            <strong>{titleize(application.last_event_type)}</strong>
                          </div>
                          <div>
                            <span>Global position</span>
                            <strong>#{application.last_global_position}</strong>
                          </div>
                          <div>
                            <span>Updated</span>
                            <strong>{prettyDate(application.updated_at)}</strong>
                          </div>
                        </div>
                      </div>
                    ) : null}
                  </div>

                  <div className="snapshot-side">
                    <div className="side-card">
                      <span className="meta-label">Compliance posture</span>
                      <strong>{titleize(readString(currentComplianceSnapshot.compliance_status) ?? "Not available")}</strong>
                      <p>Regulation set {readString(currentComplianceSnapshot.regulation_set_version) ?? "Not set"}</p>
                    </div>
                    <div className="side-card">
                      <span className="meta-label">Integrity verification</span>
                      <strong>{integritySummary.status}</strong>
                      <p>{integritySummary.verifiedAt}</p>
                    </div>
                    <div className="side-card">
                      <span className="meta-label">Last refresh</span>
                      <strong>{prettyDate(application.updated_at)}</strong>
                      <p>Projection status synced from immutable events.</p>
                    </div>
                  </div>
                </div>
              )}
            </section>

            <section className="panel lineage-panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Decision Lineage</p>
                  <h2>Event evidence timeline</h2>
                </div>
                <div className="panel-actions">
                  <p className="section-note">
                    {isOperationsView
                      ? `${visibleLineage.length} of ${lineageTotalCount} events`
                      : `${visibleLineage.length} events in view`}
                  </p>
                  {isAuditView ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => toggleEvidencePanel("lineage")}
                    >
                      {evidencePanels.lineage ? "Hide evidence" : "Expand evidence"}
                    </button>
                  ) : null}
                  {isOperationsView && lineageTotalCount > lineageDefaultCount ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setShowAllLineage((current) => !current)}
                    >
                      {showAllLineage ? "Show less" : "Show all"}
                    </button>
                  ) : null}
                </div>
              </div>

              {visibleLineage.length === 0 ? (
                <p className="empty-state">No lineage for the current application focus.</p>
              ) : (
                <div className="lineage-list">
                  {visibleLineage.map((item) => (
                    <article className="lineage-item" key={item.key}>
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
                          <span>
                            {applicationId
                              ? isOperationsView
                                ? shortId(applicationId, 5)
                                : applicationId
                              : "No application selected"}
                          </span>
                          {isAuditView ? <span>{item.checkpoint}</span> : null}
                        </div>
                        <p className={`lineage-summary ${isOperationsView ? "compact" : ""}`}>
                          {item.summary}
                        </p>
                        {isAuditView && evidencePanels.lineage ? (
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

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Recent Activity</p>
                  <h2>Latest ledger events</h2>
                </div>
                <div className="panel-actions">
                  <p className="section-note">
                    {isOperationsView
                      ? `${visibleActivity.length} of ${activityTotalCount} events`
                      : "Ordered by latest append."}
                  </p>
                  {isAuditView ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => toggleEvidencePanel("activity")}
                    >
                      {evidencePanels.activity ? "Hide evidence" : "Expand evidence"}
                    </button>
                  ) : null}
                  {isOperationsView && activityTotalCount > activityDefaultCount ? (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setShowAllActivity((current) => !current)}
                    >
                      {showAllActivity ? "Show less" : "Show all"}
                    </button>
                  ) : null}
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
                              {!rowApplicationId
                                ? "Unlinked event"
                                : shortId(rowApplicationId, isAuditView ? 8 : 5)}
                            </strong>
                            <span title={prettyDate(event.recorded_at)}>{prettyDateCompact(event.recorded_at)}</span>
                          </div>
                          <div>
                            <strong title={titleize(laneForEvent(event.event_type))}>
                              {titleize(laneForEvent(event.event_type))}
                            </strong>
                            <span>{isAuditView ? `Pos #${event.global_position}` : "Open"}</span>
                          </div>
                        </button>
                        {isAuditView && evidencePanels.activity ? (
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
          </main>

          <aside className="workspace-column right-column">
            <section className="panel">
                <div className="panel-heading">
                  <div>
                    <p className="section-kicker">Application Register</p>
                    <h2>Tracked pipeline</h2>
                  </div>
                <p className="section-note">{visibleApplicationRows.length} rows in view</p>
              </div>

              <div className="application-list">
                <div className="list-head">
                  <span>Application</span>
                  <span>Pipeline</span>
                  <span>Compliance</span>
                </div>
                {visibleApplicationRows.map((item) => (
                  <button
                    key={item.application_id}
                    className={`application-row ${
                      application?.application_id === item.application_id ? "active" : ""
                    }`}
                    onClick={() => void handleSelectApplication(item.application_id)}
                  >
                    <div>
                      <strong title={item.application_id}>
                        {shortId(item.application_id, isOperationsView ? 6 : 8)}
                      </strong>
                      <span>{isOperationsView ? "Commercial application" : "Operational record"}</span>
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
                      <span className="row-muted">
                        {application?.application_id === item.application_id ? "Focused" : "Select"}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Projection Status</p>
                  <h2>Lag and catch-up</h2>
                </div>
              </div>

              <div className="projection-list">
                {projectionStatusRows.length === 0 ? (
                  <p className="empty-state">Projection checkpoints have not reported yet.</p>
                ) : (
                  projectionStatusRows.map(({ name, lag, assessment }) => {
                    const ratio = lag.latest_position > 0 ? lag.checkpoint_position / lag.latest_position : 1;
                    return (
                      <article className="projection-row" key={name}>
                        <div className="projection-head">
                          <strong>{titleize(name)}</strong>
                          <StatusBadge label={assessment.label} tone={assessment.tone} />
                        </div>
                        <div className="projection-metrics">
                          <span>{lag.events_behind > 0 ? `${lag.events_behind} events behind` : "No event backlog"}</span>
                          <span>{formatLagMs(lag.lag_ms)} lag</span>
                          <span>Pos #{lag.checkpoint_position}</span>
                        </div>
                        <p className="projection-note">{assessment.helper}</p>
                        <div className="progress-track" aria-hidden="true">
                          <span className="progress-fill" style={{ width: `${Math.max(6, ratio * 100)}%` }} />
                        </div>
                      </article>
                    );
                  })
                )}
              </div>
            </section>

            <section className="panel">
              <div className="panel-heading">
                <div>
                  <p className="section-kicker">Compliance Posture</p>
                  <h2>{isOperationsView ? "Posture summary" : "Current regulatory state"}</h2>
                </div>
                {isAuditView ? (
                  <div className="panel-actions">
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => toggleEvidencePanel("compliance")}
                    >
                      {evidencePanels.compliance ? "Hide evidence" : "Expand evidence"}
                    </button>
                  </div>
                ) : null}
              </div>

              {compliance ? (
                <>
                  <div className={`definition-list ${isOperationsView ? "" : "compact"}`}>
                    <div>
                      <span>Status</span>
                      <strong>{titleize(readString(currentComplianceSnapshot.compliance_status) ?? "Unknown")}</strong>
                    </div>
                    <div>
                      <span>Regulation set</span>
                      <strong>{readString(currentComplianceSnapshot.regulation_set_version) ?? "Unavailable"}</strong>
                    </div>
                    {isAuditView ? (
                      <div>
                        <span>Mandatory checks</span>
                        <strong>{readStringList(currentComplianceSnapshot.mandatory_checks).length}</strong>
                      </div>
                    ) : null}
                    {isAuditView ? (
                      <div>
                        <span>Passed checks</span>
                        <strong>{readStringList(currentComplianceSnapshot.passed_checks).length}</strong>
                      </div>
                    ) : null}
                    <div>
                      <span>Failed checks</span>
                      <strong>{currentFailedChecks}</strong>
                    </div>
                    <div>
                      <span>Projection updated</span>
                      <strong>{prettyDate(readString(currentComplianceSnapshot.updated_at))}</strong>
                    </div>
                  </div>

                  {isAuditView && evidencePanels.compliance ? (
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

            {isAuditView ? (
              <>
                <section className="panel">
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

                <section className="panel">
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
          </aside>
        </div>

        <footer className="notice-bar panel">
          <span className="notice-label">System notice</span>
          <strong>{notice || "The Ledger is synchronized and ready for review."}</strong>
        </footer>
      </div>
    </div>
  );
}
