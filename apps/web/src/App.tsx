import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import {
  bootstrapDemo,
  clearStoredToken,
  fetchDeliveryHealth,
  fetchAgentPerformance,
  fetchAgentSession,
  fetchAllApplications,
  fetchApplication,
  fetchApplicationAuditTrail,
  fetchApplicationEvents,
  fetchResources,
  fetchApplicationStates,
  fetchAuthAudit,
  fetchCompliance,
  fetchLedgerHealth,
  fetchMe,
  fetchMetricsAgents,
  fetchMetricsDaily,
  fetchMetricsSummary,
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
  DeliveryHealth,
  LedgerHealth,
  MetricsAgentPoint,
  MetricsDailyPoint,
  MetricsSummary,
  RecentEvent,
  ResourceDefinition,
  ToolDefinition
} from "./types";
import apexLogo from "../logo/logo.png";
import BarChartAgents from "./components/charts/BarChartAgents";
import LineChartDaily from "./components/charts/LineChartDaily";
import LineChartProcessing from "./components/charts/LineChartProcessing";
import PieChartApproval from "./components/charts/PieChartApproval";
import BarChartTopicMessages from "./components/charts/BarChartTopicMessages";
import {
  formatCount as formatBusinessCount,
  formatDuration as formatBusinessDuration,
  formatPercent as formatBusinessPercent,
  formatTimestamp as formatBusinessTimestamp
} from "./utils/formatters";

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
type TimelinePhase = "Intake" | "Analysis" | "Compliance" | "Decision" | "Final Outcome";
type EventTimeRange = "all" | "1h" | "24h" | "7d" | "30d";
type AnalyticsWindowDays = 7 | 30 | 90;

type EvidenceSection = {
  label: string;
  items: string[];
};

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
  evidenceSections: EvidenceSection[];
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

const ANALYTICS_WINDOWS: AnalyticsWindowDays[] = [7, 30, 90];

function prettyDate(value: string | null | undefined): string {
  return formatBusinessTimestamp(value);
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

function formatMoneyEvidence(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return formatMoney(value);
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

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function formatEvidenceNumber(value: number | null | undefined, digits = 2): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return value.toLocaleString(undefined, {
    maximumFractionDigits: digits
  });
}

function appendEvidenceItem(items: string[], label: string, value: string | null | undefined): void {
  if (!value) {
    return;
  }
  items.push(`${label}: ${value}`);
}

function evidenceFactsSection(payload: Record<string, unknown>): EvidenceSection | null {
  const facts = readRecord(payload.facts);
  const items: string[] = [];
  appendEvidenceItem(items, "Revenue", formatEvidenceNumber(readNumber(facts.total_revenue)));
  appendEvidenceItem(items, "Net income", formatEvidenceNumber(readNumber(facts.net_income)));
  appendEvidenceItem(items, "EBITDA", formatEvidenceNumber(readNumber(facts.ebitda)));
  appendEvidenceItem(items, "Assets", formatEvidenceNumber(readNumber(facts.total_assets)));
  appendEvidenceItem(items, "Liabilities", formatEvidenceNumber(readNumber(facts.total_liabilities)));
  return items.length > 0 ? { label: "Extracted facts", items } : null;
}

function evidenceQualitySection(payload: Record<string, unknown>): EvidenceSection | null {
  const items: string[] = [];
  const confidence = readNumber(payload.overall_confidence);
  appendEvidenceItem(
    items,
    "Overall confidence",
    confidence === null ? null : formatPercent(confidence * 100, 1)
  );
  appendEvidenceItem(
    items,
    "Coherence",
    readBoolean(payload.is_coherent) === null
      ? null
      : readBoolean(payload.is_coherent)
        ? "Coherent"
        : "Needs review"
  );
  appendEvidenceItem(
    items,
    "Re-extraction",
    readBoolean(payload.reextraction_recommended) === null
      ? null
      : readBoolean(payload.reextraction_recommended)
        ? "Recommended"
        : "Not required"
  );
  const missingFields = readStringList(payload.critical_missing_fields);
  if (missingFields.length > 0) {
    items.push(`Missing fields: ${missingFields.map((field) => titleize(field)).join(", ")}`);
  }
  const anomalies = readStringList(payload.anomalies);
  if (anomalies.length > 0) {
    items.push(`Anomalies: ${anomalies.map((entry) => titleize(entry)).join(", ")}`);
  }
  return items.length > 0 ? { label: "Quality checks", items } : null;
}

function buildEvidenceSections(
  eventType: string,
  payload: Record<string, unknown>,
  metadata: Record<string, unknown>
): EvidenceSection[] {
  const sections: EvidenceSection[] = [];
  const documentSourceItems: string[] = [];

  if (eventType === "DocumentUploadRequested" || eventType === "DocumentUploaded") {
    appendEvidenceItem(documentSourceItems, "Document path", readString(payload.document_path));
  }
  if (eventType === "DocumentAdded" || eventType === "DocumentFormatValidated") {
    appendEvidenceItem(documentSourceItems, "Document path", readString(payload.document_path));
    appendEvidenceItem(documentSourceItems, "Document type", readString(payload.document_type));
    appendEvidenceItem(documentSourceItems, "Format", readString(payload.format));
    appendEvidenceItem(
      documentSourceItems,
      "Supported",
      readBoolean(payload.is_supported) === null
        ? null
        : readBoolean(payload.is_supported)
          ? "Yes"
          : "No"
    );
  }
  if (eventType === "ExtractionStarted") {
    appendEvidenceItem(documentSourceItems, "Document path", readString(payload.document_path));
    appendEvidenceItem(documentSourceItems, "Pipeline", readString(payload.pipeline));
  }
  if (documentSourceItems.length > 0) {
    sections.push({ label: "Document source", items: documentSourceItems });
  }

  if (eventType === "ExtractionCompleted") {
    const factsSection = evidenceFactsSection(payload);
    if (factsSection) {
      sections.push(factsSection);
    }
    const extractionContext = readRecord(payload.extraction_context);
    const contextItems: string[] = [];
    appendEvidenceItem(contextItems, "Strategy", readString(extractionContext.strategy_used));
    appendEvidenceItem(
      contextItems,
      "Extraction confidence",
      readNumber(extractionContext.extraction_confidence) === null
        ? null
        : formatPercent((readNumber(extractionContext.extraction_confidence) ?? 0) * 100, 1)
    );
    appendEvidenceItem(
      contextItems,
      "Pages",
      readNumber(extractionContext.page_count) === null
        ? null
        : formatCount(readNumber(extractionContext.page_count))
    );
    appendEvidenceItem(contextItems, "Domain hint", titleize(readString(extractionContext.domain_hint)));
    appendEvidenceItem(contextItems, "Origin", titleize(readString(extractionContext.origin_type)));
    appendEvidenceItem(contextItems, "Layout", titleize(readString(extractionContext.layout_complexity)));
    if (contextItems.length > 0) {
      sections.push({ label: "Extraction context", items: contextItems });
    }
    const confidenceMap = readRecord(payload.field_confidence);
    const confidenceItems: string[] = [];
    for (const [field, value] of Object.entries(confidenceMap)) {
      const numericValue = readNumber(value);
      if (numericValue === null) {
        continue;
      }
      confidenceItems.push(`${titleize(field)}: ${formatPercent(numericValue * 100, 0)}`);
    }
    if (confidenceItems.length > 0) {
      sections.push({ label: "Field coverage", items: confidenceItems });
    }
    const extractionNotes = readStringList(payload.extraction_notes);
    if (extractionNotes.length > 0) {
      sections.push({
        label: "Extraction notes",
        items: extractionNotes.map((entry) => titleize(entry))
      });
    }
    const factProvenance = readRecord(payload.fact_provenance);
    const provenanceItems = Object.entries(factProvenance)
      .map(([metric, raw]) => {
        const provenance = readRecord(raw);
        const parts = [
          titleize(metric),
          readString(provenance.raw_value) ? `raw ${String(provenance.raw_value)}` : null,
          readNumber(provenance.page_number) !== null
            ? `page ${formatCount(readNumber(provenance.page_number))}`
            : null,
          readString(provenance.source_chunk_id)
            ? `chunk ${shortId(readString(provenance.source_chunk_id), 10)}`
            : null
        ].filter((value): value is string => Boolean(value));
        return parts.join(" • ");
      })
      .filter(Boolean);
    if (provenanceItems.length > 0) {
      sections.push({ label: "Fact provenance", items: provenanceItems });
    }
    const excerptItems = Object.entries(factProvenance)
      .map(([metric, raw]) => {
        const provenance = readRecord(raw);
        const excerpt = readString(provenance.source_excerpt);
        if (!excerpt) {
          return null;
        }
        return `${titleize(metric)} excerpt: ${excerpt}`;
      })
      .filter((value): value is string => Boolean(value));
    if (excerptItems.length > 0) {
      sections.push({ label: "Source excerpts", items: excerptItems });
    }
  }

  if (eventType === "QualityAssessmentCompleted") {
    const qualitySection = evidenceQualitySection(payload);
    if (qualitySection) {
      sections.push(qualitySection);
    }
    const notes = readString(payload.auditor_notes);
    if (notes) {
      sections.push({ label: "Assessment notes", items: [`Auditor notes: ${notes}`] });
    }
  }

  if (eventType === "PackageCreated" || eventType === "PackageReadyForAnalysis") {
    const packageItems: string[] = [];
    appendEvidenceItem(packageItems, "Package ID", readString(payload.package_id));
    appendEvidenceItem(packageItems, "Ready at", readString(payload.ready_at));
    appendEvidenceItem(packageItems, "Created at", readString(payload.created_at));
    if (packageItems.length > 0) {
      sections.push({ label: "Package status", items: packageItems });
    }
  }

  if (eventType === "CreditAnalysisRequested" || eventType === "FraudScreeningRequested") {
    const requestItems: string[] = [];
    appendEvidenceItem(requestItems, "Assigned agent", readString(payload.assigned_agent_id));
    appendEvidenceItem(requestItems, "Priority", readString(payload.priority));
    appendEvidenceItem(requestItems, "Source", readString(payload.source));
    appendEvidenceItem(requestItems, "Document package stream", readString(payload.docpkg_stream_id));
    if (requestItems.length > 0) {
      sections.push({ label: "Request context", items: requestItems });
    }
  }

  if (eventType === "CreditAnalysisCompleted") {
    const analysisItems: string[] = [];
    appendEvidenceItem(analysisItems, "Risk tier", readString(payload.risk_tier));
    appendEvidenceItem(
      analysisItems,
      "Confidence",
      readNumber(payload.confidence_score) === null
        ? null
        : formatPercent((readNumber(payload.confidence_score) ?? 0) * 100, 1)
    );
    appendEvidenceItem(
      analysisItems,
      "Recommended limit",
      formatMoneyEvidence(readNumber(payload.recommended_limit_usd))
    );
    appendEvidenceItem(analysisItems, "Model", readString(payload.model_version));
    appendEvidenceItem(
      analysisItems,
      "Duration",
      readNumber(payload.analysis_duration_ms) === null
        ? null
        : `${formatCount(readNumber(payload.analysis_duration_ms))} ms`
    );
    appendEvidenceItem(analysisItems, "Input hash", shortId(readString(payload.input_data_hash), 8));
    if (analysisItems.length > 0) {
      sections.push({ label: "Risk evidence", items: analysisItems });
    }
  }

  if (eventType === "FraudScreeningCompleted") {
    const fraudItems: string[] = [];
    appendEvidenceItem(
      fraudItems,
      "Fraud score",
      readNumber(payload.fraud_score) === null ? null : formatEvidenceNumber(readNumber(payload.fraud_score), 2)
    );
    appendEvidenceItem(fraudItems, "Screening model", readString(payload.screening_model_version));
    appendEvidenceItem(fraudItems, "Input hash", shortId(readString(payload.input_data_hash), 8));
    const anomalyFlags = readStringList(payload.anomaly_flags);
    fraudItems.push(
      anomalyFlags.length > 0
        ? `Anomaly flags: ${anomalyFlags.map((entry) => titleize(entry)).join(", ")}`
        : "Anomaly flags: None"
    );
    sections.push({ label: "Fraud evidence", items: fraudItems });
  }

  if (eventType === "ComplianceCheckRequested") {
    const complianceItems: string[] = [];
    appendEvidenceItem(complianceItems, "Regulation set", readString(payload.regulation_set_version));
    const checksRequired = readStringList(payload.checks_required);
    if (checksRequired.length > 0) {
      complianceItems.push(`Checks required: ${checksRequired.map((entry) => titleize(entry)).join(", ")}`);
    }
    if (complianceItems.length > 0) {
      sections.push({ label: "Compliance scope", items: complianceItems });
    }
  }

  if (eventType === "ComplianceRulePassed" || eventType === "ComplianceRuleFailed") {
    const ruleItems: string[] = [];
    appendEvidenceItem(ruleItems, "Rule ID", readString(payload.rule_id));
    appendEvidenceItem(ruleItems, "Rule version", readString(payload.rule_version));
    appendEvidenceItem(ruleItems, "Failure reason", readString(payload.failure_reason));
    appendEvidenceItem(ruleItems, "Evidence hash", shortId(readString(payload.evidence_hash), 8));
    appendEvidenceItem(
      ruleItems,
      "Remediation required",
      readBoolean(payload.remediation_required) === null
        ? null
        : readBoolean(payload.remediation_required)
          ? "Yes"
          : "No"
    );
    if (ruleItems.length > 0) {
      sections.push({ label: "Rule evidence", items: ruleItems });
    }
  }

  if (eventType === "ComplianceCheckCompleted") {
    const verdictItems: string[] = [];
    appendEvidenceItem(verdictItems, "Overall verdict", titleize(readString(payload.overall_verdict)));
    appendEvidenceItem(
      verdictItems,
      "Completed checks",
      readNumber(payload.completed_checks) === null ? null : formatCount(readNumber(payload.completed_checks))
    );
    appendEvidenceItem(
      verdictItems,
      "Total checks",
      readNumber(payload.total_checks) === null ? null : formatCount(readNumber(payload.total_checks))
    );
    const failedRules = readStringList(payload.failed_rule_ids);
    verdictItems.push(
      failedRules.length > 0
        ? `Failed rules: ${failedRules.map((entry) => titleize(entry)).join(", ")}`
        : "Failed rules: None"
    );
    sections.push({ label: "Compliance verdict", items: verdictItems });
  }

  if (eventType === "DecisionRequested") {
    const requestItems: string[] = [];
    const requiredInputs = readStringList(payload.required_inputs);
    if (requiredInputs.length > 0) {
      requestItems.push(`Required inputs: ${requiredInputs.map((entry) => titleize(entry)).join(", ")}`);
    }
    appendEvidenceItem(requestItems, "Requested by", readString(payload.requested_by));
    if (requestItems.length > 0) {
      sections.push({ label: "Decision request", items: requestItems });
    }
  }

  if (eventType === "DecisionGenerated") {
    const recommendationItems: string[] = [];
    appendEvidenceItem(recommendationItems, "Recommendation", titleize(readString(payload.recommendation)));
    appendEvidenceItem(
      recommendationItems,
      "Confidence",
      readNumber(payload.confidence_score) === null
        ? null
        : formatPercent((readNumber(payload.confidence_score) ?? 0) * 100, 1)
    );
    appendEvidenceItem(
      recommendationItems,
      "Assessed max limit",
      formatMoneyEvidence(readNumber(payload.assessed_max_limit_usd))
    );
    appendEvidenceItem(recommendationItems, "Compliance status", titleize(readString(payload.compliance_status)));
    if (recommendationItems.length > 0) {
      sections.push({ label: "Recommendation evidence", items: recommendationItems });
    }

    const basisItems: string[] = [];
    appendEvidenceItem(basisItems, "Decision basis", readString(payload.decision_basis_summary));
    const sessions = readStringList(payload.contributing_agent_sessions);
    if (sessions.length > 0) {
      basisItems.push(`Contributing sessions: ${sessions.map((session) => shortId(session, 10)).join(", ")}`);
    }
    const modelVersions = readRecord(payload.model_versions);
    const modelEntries = Object.entries(modelVersions)
      .filter(([, version]) => readString(version))
      .map(([actor, version]) => `${actor}: ${String(version)}`);
    if (modelEntries.length > 0) {
      basisItems.push(`Model versions: ${modelEntries.join(", ")}`);
    }
    if (basisItems.length > 0) {
      sections.push({ label: "Decision basis", items: basisItems });
    }
  }

  if (eventType === "HumanReviewCompleted") {
    const reviewItems: string[] = [];
    appendEvidenceItem(reviewItems, "Final decision", titleize(readString(payload.final_decision)));
    appendEvidenceItem(
      reviewItems,
      "Override",
      readBoolean(payload.override) === null ? null : readBoolean(payload.override) ? "Yes" : "No"
    );
    appendEvidenceItem(reviewItems, "Override reason", readString(payload.override_reason));
    if (reviewItems.length > 0) {
      sections.push({ label: "Human review", items: reviewItems });
    }
  }

  if (eventType === "ApplicationApproved") {
    const approvalItems: string[] = [];
    appendEvidenceItem(
      approvalItems,
      "Approved amount",
      formatMoneyEvidence(readNumber(payload.approved_amount_usd))
    );
    appendEvidenceItem(
      approvalItems,
      "Interest rate",
      readNumber(payload.interest_rate) === null ? null : `${formatEvidenceNumber(readNumber(payload.interest_rate), 2)}%`
    );
    appendEvidenceItem(approvalItems, "Effective date", readString(payload.effective_date));
    const conditions = readStringList(payload.conditions);
    if (conditions.length > 0) {
      approvalItems.push(`Conditions: ${conditions.join(", ")}`);
    }
    if (approvalItems.length > 0) {
      sections.push({ label: "Final approval", items: approvalItems });
    }
  }

  if (eventType === "ApplicationDeclined") {
    const declineItems: string[] = [];
    const reasons = readStringList(payload.decline_reasons);
    if (reasons.length > 0) {
      declineItems.push(`Decline reasons: ${reasons.join(", ")}`);
    }
    appendEvidenceItem(
      declineItems,
      "Adverse notice required",
      readBoolean(payload.adverse_action_notice_required) === null
        ? null
        : readBoolean(payload.adverse_action_notice_required)
          ? "Yes"
          : "No"
    );
    if (declineItems.length > 0) {
      sections.push({ label: "Final decline", items: declineItems });
    }
  }

  if (eventType === "AuditIntegrityCheckRun") {
    const integrityItems: string[] = [];
    appendEvidenceItem(
      integrityItems,
      "Events verified",
      readNumber(payload.events_verified_count) === null ? null : formatCount(readNumber(payload.events_verified_count))
    );
    appendEvidenceItem(integrityItems, "Integrity hash", shortId(readString(payload.integrity_hash), 8));
    appendEvidenceItem(integrityItems, "Previous hash", shortId(readString(payload.previous_hash), 8));
    appendEvidenceItem(
      integrityItems,
      "Chain valid",
      readBoolean(payload.chain_valid) === null ? null : readBoolean(payload.chain_valid) ? "Yes" : "No"
    );
    appendEvidenceItem(
      integrityItems,
      "Tamper detected",
      readBoolean(payload.tamper_detected) === null ? null : readBoolean(payload.tamper_detected) ? "Yes" : "No"
    );
    if (integrityItems.length > 0) {
      sections.push({ label: "Integrity attestation", items: integrityItems });
    }
  }

  const lineageItems: string[] = [];
  appendEvidenceItem(lineageItems, "Correlation ID", shortId(readString(metadata.correlation_id), 8));
  appendEvidenceItem(lineageItems, "Causation ID", shortId(readString(metadata.causation_id), 8));
  appendEvidenceItem(lineageItems, "Actor", readString(metadata.actor_id));
  if (lineageItems.length > 0) {
    sections.push({ label: "Event lineage", items: lineageItems });
  }

  return sections;
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

function formatCount(value: number | null | undefined): string {
  return formatBusinessCount(value);
}

function formatPercent(value: number | null | undefined, digits = 1): string {
  return formatBusinessPercent(value, digits);
}

function formatDuration(seconds: number | null | undefined): string {
  return formatBusinessDuration(seconds);
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

function commandPayloadHint(toolName: string): string {
  const hints: Record<string, string> = {
    submit_application:
      "Change application_id and submitted_at each run. If document processing is enabled, set document_path or document_paths to real files.",
    start_agent_session:
      "Change agent_id + session_id (use a new pair per run). Keep model_version aligned with later analysis commands.",
    record_credit_analysis:
      "Change application_id, agent_id, session_id, model_version, and input_data_hash. Session must already be started.",
    record_fraud_screening:
      "Change application_id, agent_id, session_id, screening_model_version, and input_data_hash. Use the same model/session as session start.",
    record_compliance_check:
      "Change application_id and rule_id. First compliance call for an app must include checks_required containing that rule.",
    generate_decision:
      "Change application_id and contributing_agent_sessions (format: agent-<agent_id>-<session_id>) to match your completed analyses.",
    record_human_review:
      "Change application_id. For APPROVE include approved_amount_usd; if override=true you must provide override_reason.",
    run_integrity_check:
      "Change entity_id to your application ID. role must be compliance/admin, and checks are limited to once per minute per app."
  };
  return hints[toolName] ?? "Update identifiers (application/session/agent IDs) to match your current run context.";
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

function commandSeed(
  toolName: string,
  applicationId: string,
  role: string | undefined,
  currentAgentId: string
): Record<string, unknown> {
  const targetApplicationId = applicationId.trim() || "app-addis-001";
  const targetAgentId = currentAgentId.trim() || "credit-agent-ethi-01";
  const appToken =
    targetApplicationId
      .replace(/[^a-zA-Z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .toLowerCase() || "app-addis-001";
  const sessionId = `sess-${appToken}-01`;
  const sessionStreamId = `agent-${targetAgentId}-${sessionId}`;
  const modelVersion = "credit-v2";
  const normalizedRole = (role ?? "").toLowerCase();
  const integrityRole =
    normalizedRole === "admin" || normalizedRole === "compliance"
      ? normalizedRole
      : "compliance";

  if (toolName === "submit_application") {
    return {
      application_id: targetApplicationId,
      applicant_id: "et-borrower-001",
      requested_amount_usd: 1250000,
      loan_purpose: "working_capital",
      submission_channel: "addis-branch",
      submitted_at: new Date().toISOString(),
      process_documents_after_submit: false
    };
  }

  if (toolName === "start_agent_session") {
    return {
      agent_id: targetAgentId,
      session_id: sessionId,
      context_source: `loan_application:${targetApplicationId}`,
      event_replay_from_position: 0,
      context_token_count: 256,
      model_version: modelVersion
    };
  }

  if (toolName === "record_credit_analysis") {
    return {
      application_id: targetApplicationId,
      agent_id: targetAgentId,
      session_id: sessionId,
      model_version: modelVersion,
      confidence_score: 0.87,
      risk_tier: "MEDIUM",
      recommended_limit_usd: 1100000,
      analysis_duration_ms: 142,
      input_data_hash: `hash-credit-${appToken}`
    };
  }

  if (toolName === "record_fraud_screening") {
    return {
      application_id: targetApplicationId,
      agent_id: targetAgentId,
      session_id: sessionId,
      fraud_score: 0.08,
      anomaly_flags: [],
      screening_model_version: modelVersion,
      input_data_hash: `hash-fraud-${appToken}`
    };
  }

  if (toolName === "record_compliance_check") {
    return {
      application_id: targetApplicationId,
      regulation_set_version: "2026.03",
      rule_id: "rule-a",
      rule_version: "v1",
      passed: true,
      checks_required: ["rule-a"]
    };
  }

  if (toolName === "generate_decision") {
    return {
      application_id: targetApplicationId,
      orchestrator_agent_id: "orchestrator-1",
      recommendation: "APPROVE",
      confidence_score: 0.91,
      decision_basis_summary: "credit/fraud/compliance acceptable",
      contributing_agent_sessions: [sessionStreamId],
      model_versions: {
        [targetAgentId]: modelVersion,
        "orchestrator-1": "orch-v1"
      }
    };
  }

  if (toolName === "record_human_review") {
    return {
      application_id: targetApplicationId,
      reviewer_id: "loan-officer-addis-01",
      override: false,
      final_decision: "APPROVE",
      approved_amount_usd: 1000000,
      interest_rate: 7.2,
      conditions: ["signed guarantee"],
      effective_date: "2026-03-29"
    };
  }

  if (toolName === "run_integrity_check") {
    return {
      entity_type: "application",
      entity_id: targetApplicationId,
      role: integrityRole
    };
  }

  const seeded = DEFAULT_COMMAND_PAYLOAD[toolName];
  if (seeded) {
    return { ...seeded };
  }
  return { application_id: targetApplicationId };
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
    causationId: readString(metadata.causation_id),
    evidenceSections: buildEvidenceSections(event.event_type, payload, metadata)
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
    causationId: null,
    evidenceSections: buildEvidenceSections(entry.event_type, payload, {})
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
        {props.stages.map((stage, index) => {
          const hoverDetails = [
            `Agent: ${stage.name}`,
            `Role: ${stage.role}`,
            `Status: ${stage.status}`,
            `Latest event: ${stage.latestEvent}`,
            `Last update: ${stage.latestAt}`
          ].join("\n");

          return (
            <div className="pipeline-segment" key={stage.name}>
              <article
                className={`pipeline-step tone-${stage.tone} ${stage.name === "HumanReview" ? "pipeline-step-human" : ""}`}
                title={hoverDetails}
                aria-label={hoverDetails}
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
          );
        })}
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
      checkpoint_age_ms: number;
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
                    <span>Checkpoint age</span>
                    <strong>{formatLagMs(lag.checkpoint_age_ms)}</strong>
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

function DeliveryHealthPanel(props: { delivery: DeliveryHealth | null }): ReactNode {
  if (!props.delivery) {
    return (
      <section className="panel operations-health-panel system-health">
        <div className="panel-heading">
          <div>
            <p className="section-kicker">System Health</p>
            <h2>Kafka outbox and transport</h2>
          </div>
        </div>
        <p className="empty-state">No delivery health snapshot available yet.</p>
      </section>
    );
  }

  const { delivery } = props;
  const tone = toTone(delivery.status);
  const brokerHost = delivery.transport.broker ?? "Not configured";
  const sortedTopics = [...delivery.topics].sort(
    (left, right) => right.published_last_1h - left.published_last_1h
  );
  const hasTopicTraffic = sortedTopics.some(
    (topic) => topic.published_last_1h > 0 || topic.pending > 0 || topic.dead_letter > 0
  );

  return (
    <section className="panel operations-health-panel system-health">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">System Health</p>
          <h2>Kafka outbox and transport</h2>
        </div>
        <StatusBadge label={titleize(delivery.status)} tone={tone} />
      </div>

      <div className="operations-health-grid">
        <article className="operations-health-card">
          <span>Status</span>
          <strong>
            <span className={`health-indicator health-${tone}`} aria-hidden="true" />
            {titleize(delivery.status)}
          </strong>
          <small>Outbox relay health</small>
        </article>
        <article className="operations-health-card">
          <span>Pending</span>
          <strong>{formatCount(delivery.outbox.pending)}</strong>
          <small>
            Oldest age{" "}
            {delivery.outbox.oldest_pending_age_ms === null
              ? "n/a"
              : formatLagMs(delivery.outbox.oldest_pending_age_ms)}
          </small>
        </article>
        <article className="operations-health-card">
          <span>Retrying</span>
          <strong>{formatCount(delivery.outbox.retrying)}</strong>
          <small>Messages currently being retried</small>
        </article>
        <article className="operations-health-card">
          <span>Dead-letter</span>
          <strong>{formatCount(delivery.outbox.dead_letter)}</strong>
          <small>Messages requiring manual review</small>
        </article>
        <article className="operations-health-card">
          <span>Total published</span>
          <strong>{formatCount(delivery.outbox.published_total)}</strong>
          <small>{`${formatCount(delivery.throughput.published_last_1h)} in last 1h`}</small>
        </article>
      </div>

      <div className="operations-broker-panel">
        <div className="panel-subhead">
          <h3>Broker connection</h3>
          <span className="section-note">Last published: {prettyDate(delivery.outbox.last_published_at)}</span>
        </div>
        <div className="definition-list compact operations-broker-grid">
          <div>
            <span>Broker type</span>
            <strong>{titleize(delivery.transport.mode)}</strong>
          </div>
          <div>
            <span>Host</span>
            <strong>{brokerHost}</strong>
          </div>
          <div>
            <span>Transport status</span>
            <strong>{titleize(delivery.transport.status)}</strong>
          </div>
        </div>
        <p className="projection-note">{delivery.transport.detail}</p>
      </div>

      {sortedTopics.length > 0 ? (
        <div className="operations-topic-block">
          <div className="panel-subhead">
            <h3>Topic delivery</h3>
            <span className="section-note">{prettyDate(delivery.updated_at)}</span>
          </div>

          <div className="operations-topic-table-wrap">
            <table className="operations-topic-table">
              <thead>
                <tr>
                  <th scope="col">Topic</th>
                  <th scope="col">Published</th>
                  <th scope="col">Pending</th>
                  <th scope="col">DLQ</th>
                  <th scope="col">Last published</th>
                </tr>
              </thead>
              <tbody>
                {sortedTopics.map((topic) => (
                  <tr key={topic.topic}>
                    <td>
                      <strong>{topic.topic}</strong>
                    </td>
                    <td>{formatCount(topic.published_last_1h)}</td>
                    <td>{formatCount(topic.pending)}</td>
                    <td>{formatCount(topic.dead_letter)}</td>
                    <td>{prettyDate(delivery.outbox.last_published_at ?? delivery.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {hasTopicTraffic ? (
            <div className="operations-topic-chart">
              <h3>Messages per topic</h3>
              <p>Published in the last hour</p>
              <div className="operations-topic-chart-canvas">
                <BarChartTopicMessages topics={sortedTopics} />
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="empty-state">No topic activity has been recorded yet.</p>
      )}
    </section>
  );
}

function AnalyticsChartCard(props: {
  title: string;
  subtitle: string;
  hasData: boolean;
  children: ReactNode;
  compact?: boolean;
}): ReactNode {
  return (
    <article className="analytics-chart-card chart-card">
      <header>
        <h3>{props.title}</h3>
        <p>{props.subtitle}</p>
      </header>
      <div className={`analytics-chart-canvas ${props.compact ? "analytics-chart-canvas-pie" : ""}`}>
        {props.hasData ? props.children : <p className="analytics-chart-empty">No data available for this period</p>}
      </div>
    </article>
  );
}

function ClientAnalyticsPanel(props: {
  summary: MetricsSummary | null;
  daily: MetricsDailyPoint[];
  agents: MetricsAgentPoint[];
  selectedWindow: AnalyticsWindowDays;
  onWindowChange: (value: AnalyticsWindowDays) => void;
  className?: string;
}): ReactNode {
  const panelClassName = props.className
    ? `panel dashboard-analytics ${props.className}`
    : "panel dashboard-analytics";

  if (!props.summary) {
    return (
      <section className={panelClassName}>
        <div className="panel-heading">
          <div>
            <p className="section-kicker">Client Analytics</p>
            <h2>Business performance dashboard</h2>
          </div>
        </div>
        <p className="empty-state">Projection metrics are loading or no projected data is available yet.</p>
      </section>
    );
  }

  const { summary, daily, agents } = props;
  const approvalTone: Tone =
    summary.approval_rate >= 70
      ? "ok"
      : summary.approval_rate >= 50
        ? "warning"
        : "critical";
  const windowLabel = `${summary.window_days}d`;
  const hasDailyData = daily.some(
    (point) => point.submitted > 0 || point.approved > 0 || point.declined > 0
  );
  const hasOutcomeData = summary.approved + summary.declined > 0;
  const hasAgentData = agents.some(
    (agent) => Number(agent.activity_score ?? 0) > 0 || Number(agent.decisions ?? 0) > 0
  );
  const hasProcessingData = daily.some((point) => point.avg_processing_seconds > 0);

  return (
    <section className={panelClassName}>
      <div className="panel-heading analytics-heading">
        <div>
          <p className="section-kicker">Client Analytics</p>
          <h2>Business performance dashboard</h2>
        </div>
        <div className="analytics-heading-controls">
          <div className="analytics-window-switch" aria-label="Analytics window">
            {ANALYTICS_WINDOWS.map((windowDays) => (
              <button
                key={windowDays}
                type="button"
                className={`button subtle analytics-window-button ${props.selectedWindow === windowDays ? "active" : ""}`}
                onClick={() => props.onWindowChange(windowDays)}
              >
                {windowDays}d
              </button>
            ))}
          </div>
          <StatusBadge
            label={`${windowLabel} approval ${formatPercent(summary.approval_rate, 1)}`}
            tone={approvalTone}
          />
        </div>
      </div>

      <div className="analytics-kpi-grid kpi-row">
        <article className="analytics-kpi-card">
          <span>Total submitted</span>
          <strong>{formatCount(summary.submitted)}</strong>
          <small className="kpi-helper">{`Across the last ${summary.window_days} days`}</small>
        </article>
        <article className="analytics-kpi-card">
          <span>Approval rate</span>
          <strong>{formatPercent(summary.approval_rate, 1)}</strong>
          <small className="kpi-helper">Approved applications out of finalized decisions</small>
        </article>
        <article className="analytics-kpi-card">
          <span>Avg processing time</span>
          <strong>{formatDuration(summary.avg_processing_time)}</strong>
          <small className="kpi-helper">Auto-formatted in sec/min for readability</small>
        </article>
        <article className="analytics-kpi-card">
          <span>Finalized</span>
          <strong>{formatCount(summary.finalized)}</strong>
          <small className="kpi-helper">Applications with a final decision</small>
        </article>
      </div>

      <div className="analytics-chart-grid analytics-chart-grid-mid">
        <AnalyticsChartCard
          title="Daily applications and approvals"
          subtitle={`Last ${summary.window_days} days`}
          hasData={hasDailyData}
        >
          <>
            <LineChartDaily points={daily} />
          </>
        </AnalyticsChartCard>

        <AnalyticsChartCard
          title="Decision outcome split"
          subtitle={`Approved vs declined • total ${formatCount(summary.approved + summary.declined)}`}
          hasData={hasOutcomeData}
          compact
        >
          <>
            <PieChartApproval approved={summary.approved} declined={summary.declined} />
          </>
        </AnalyticsChartCard>
      </div>

      <div className="analytics-chart-grid analytics-chart-grid-bottom">
        <AnalyticsChartCard
          title="Agent activity"
          subtitle="Activity score by agent (sessions, analyses, decisions, reviews)"
          hasData={hasAgentData}
        >
          <>
            <BarChartAgents agents={agents} />
          </>
        </AnalyticsChartCard>

        <AnalyticsChartCard
          title="Average processing time"
          subtitle="Daily average time"
          hasData={hasProcessingData}
        >
          <>
            <LineChartProcessing points={daily} />
          </>
        </AnalyticsChartCard>
      </div>

      <p className="section-note">Last updated: {formatBusinessTimestamp(summary.generated_at)}</p>
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
  className?: string;
}): ReactNode {
  const isSidebar = props.className?.includes("sidebar-mcp") ?? false;
  const resourceOutput =
    props.resourceResult || (props.resourceBusy ? "Loading resource output..." : "Resource output appears here after loading a ledger URI.");

  return (
    <section className={`panel mcp-panel ${props.className ?? ""}`.trim()}>
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
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  props.onInspectResource();
                }
              }}
              placeholder="ledger://applications/app-.../compliance"
            />
          </label>

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

          {isSidebar ? (
            <div className="mcp-results mcp-results-inline">
              <div className="panel-subhead">
                <strong>Resource output</strong>
                <span>{props.resourceHistory[0]?.uri ?? "Awaiting query"}</span>
              </div>
              <pre className="result-box">{resourceOutput}</pre>
            </div>
          ) : null}

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
          {isSidebar ? null : <pre className="result-box">{resourceOutput}</pre>}
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
          <p className="field-note">{commandPayloadHint(props.selectedTool)}</p>
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
                          {props.showMetadata && item.evidenceSections.length > 0 ? (
                            item.evidenceSections.map((section) => (
                              <div className="evidence-inline" key={`${item.key}-${section.label}`}>
                                <span className="meta-label">{section.label}</span>
                                <div className="lineage-evidence">
                                  {section.items.map((evidenceItem) => (
                                    <span key={evidenceItem}>{evidenceItem}</span>
                                  ))}
                                </div>
                              </div>
                            ))
                          ) : null}
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

function assessProjection(lag: {
  events_behind: number;
  lag_ms: number;
  status: string;
}): ProjectionAssessment {
  const hasBacklog = lag.events_behind > 0;

  if (!hasBacklog) {
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
    JSON.stringify(commandSeed("submit_application", "", undefined, "credit-agent-ethi-01"), null, 2)
  );
  const [commandResult, setCommandResult] = useState<string>("");

  const [applicationId, setApplicationId] = useState<string>("");
  const [agentId, setAgentId] = useState<string>("credit-agent-ethi-01");
  const [temporalAsOf, setTemporalAsOf] = useState<string>("");

  const [states, setStates] = useState<AppStateCount[]>([]);
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [trackedApplicationsTotal, setTrackedApplicationsTotal] = useState<number>(0);
  const [application, setApplication] = useState<ApplicationSummary | null>(null);
  const [compliance, setCompliance] = useState<ComplianceView | null>(null);
  const [temporalCompliance, setTemporalCompliance] = useState<ComplianceView | null>(null);
  const [agentPerformance, setAgentPerformance] = useState<AgentPerformance | null>(null);
  const [health, setHealth] = useState<LedgerHealth | null>(null);
  const [deliveryHealth, setDeliveryHealth] = useState<DeliveryHealth | null>(null);
  const [metricsSummary, setMetricsSummary] = useState<MetricsSummary | null>(null);
  const [metricsDaily, setMetricsDaily] = useState<MetricsDailyPoint[]>([]);
  const [metricsAgents, setMetricsAgents] = useState<MetricsAgentPoint[]>([]);
  const [analyticsWindow, setAnalyticsWindow] = useState<AnalyticsWindowDays>(30);
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

    rows.forEach((item) => {
      deduped.set(item.key, item);
    });
    complianceRows.forEach((item) => {
      if (!deduped.has(item.key)) {
        deduped.set(item.key, item);
      }
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
  const hasIntegrityCommand = authUser?.allowed_commands.includes("run_integrity_check") ?? false;
  const primaryPipelineState = states[0];
  const applicationRegisterLimit = 10;
  const visibleApplicationRows = applications.slice(0, applicationRegisterLimit);

  const toggleEvidencePanel = useCallback((panel: EvidencePanelKey) => {
    setEvidencePanels((current) => ({ ...current, [panel]: !current[panel] }));
  }, []);

  const collapseEvidencePanels = useCallback(() => {
    setEvidencePanels({
      integrity: false,
      snapshot: false,
      lineage: false,
      activity: false,
      compliance: false
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
    setMetricsSummary(null);
    setMetricsDaily([]);
    setMetricsAgents([]);
    setApplicationEvents([]);
    setTrackedApplicationsTotal(0);
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
    const [
      nextStates,
      nextAppsPage,
      nextHealth,
      nextDeliveryHealth,
      nextMetricsSummary,
      nextMetricsDaily,
      nextMetricsAgents,
      nextEvents,
      nextTools,
      nextResources,
    ] = await Promise.all([
      fetchApplicationStates(),
      fetchAllApplications(200),
      fetchLedgerHealth(),
      fetchDeliveryHealth(),
      fetchMetricsSummary(analyticsWindow),
      fetchMetricsDaily(analyticsWindow),
      fetchMetricsAgents(analyticsWindow),
      fetchRecentEvents(60),
      fetchTools(),
      fetchResources()
    ]);

    setStates(nextStates);
    setApplications(nextAppsPage.items);
    setTrackedApplicationsTotal(nextAppsPage.total);
    setHealth(nextHealth);
    setDeliveryHealth(nextDeliveryHealth);
    setMetricsSummary(nextMetricsSummary);
    setMetricsDaily(nextMetricsDaily);
    setMetricsAgents(nextMetricsAgents);
    setRecentEvents(nextEvents);
    setToolDefinitions(nextTools);
    setResourceDefinitions(nextResources);

    if (nextTools.length > 0 && !nextTools.some((item) => item.name === selectedTool)) {
      const nextTool = nextTools[0].name;
      setSelectedTool(nextTool);
      setCommandPayload(JSON.stringify(commandSeed(nextTool, applicationId, authUser?.role, agentId), null, 2));
    }

    if (canViewAudit) {
      const audit = await fetchAuthAudit(40);
      setAuditRows(audit);
    } else {
      setAuditRows([]);
    }

    setLastRefresh(new Date().toISOString());
  }, [agentId, analyticsWindow, applicationId, authUser?.role, canViewAudit, selectedTool]);

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
    collapseEvidencePanels();
  }, [authUser, collapseEvidencePanels]);

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
    collapseEvidencePanels();
  }, [applicationId, collapseEvidencePanels]);

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
        JSON.stringify(commandSeed(selectedTool, applicationId, session.user.role, agentId), null, 2)
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
    setCommandPayload(JSON.stringify(commandSeed(nextTool, applicationId, authUser?.role, agentId), null, 2));
    setCommandResult("");
  }

  function prepareTool(toolName: string, overrides?: Record<string, unknown>) {
    const nextPayload = { ...commandSeed(toolName, applicationId, authUser?.role, agentId), ...overrides };
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
        <header className="identity-bar identity-bar-constrained panel">
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
            value={`${trackedApplicationsTotal}`}
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
            <MCPResourcePanel
              resourceUri={resourceUri}
              onResourceUriChange={setResourceUri}
              onInspectResource={(target) => void handleInspectResource(target)}
              resourceBusy={resourceBusy}
              resourceQuickPicks={resourceQuickPicks}
              resourceHistory={resourceHistory}
              resourceDefinitions={resourceDefinitions}
              resourceResult={resourceResult}
              className="sidebar-mcp"
            />
          </aside>

          <main className="workspace-column center-column">
            <section className="workspace-story-section">
              <DeliveryHealthPanel delivery={deliveryHealth} />
            </section>

            <section className="workspace-story-section">
              <div className="workspace-story-head">
                <div>
                  <p className="section-kicker">Audit</p>
                  <h3>Decision evidence and traceability</h3>
                </div>
                <div className="workspace-story-actions">
                  <p className="section-note">
                    End-to-end event evidence, agent activity, and decision lineage for client review.
                  </p>
                  <button
                    type="button"
                    className="button subtle button-compact"
                    onClick={() => toggleEvidencePanel("lineage")}
                  >
                    {evidencePanels.lineage ? "Collapse audit evidence" : "Expand audit evidence"}
                  </button>
                </div>
              </div>

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
              />
              <EventCoveragePanel
                applicationId={applicationId.trim()}
                observedEventTypes={lineageEventTypes}
              />
            </section>

            <section className="workspace-story-section">
              <div className="workspace-story-head">
                <div>
                  <p className="section-kicker">System</p>
                  <h3>Projection and resource readiness</h3>
                </div>
                <p className="section-note">
                  Platform health, projection readiness, and MCP resource access in one place.
                </p>
              </div>

              <ProjectionHealthPanel rows={projectionStatusRows} />
            </section>
          </main>

          <aside className="workspace-column right-column">
            <ClientAnalyticsPanel
              summary={metricsSummary}
              daily={metricsDaily}
              agents={metricsAgents}
              selectedWindow={analyticsWindow}
              onWindowChange={setAnalyticsWindow}
              className="sidebar-analytics"
            />

            <IntegrityStatusCard
              title="Ledger integrity"
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
