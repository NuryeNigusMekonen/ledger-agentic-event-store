export type ApiError = {
  error_type: string;
  message: string;
  suggested_action?: string;
  details?: unknown;
};

export type ApiEnvelope<T> = {
  ok: boolean;
  result?: T;
  error?: ApiError;
};

export type ProjectionLag = {
  checkpoint_position: number;
  latest_position: number;
  events_behind: number;
  lag_ms: number;
  checkpoint_age_ms: number;
  status: string;
  updated_at: string;
};

export type LedgerHealth = {
  projections: Record<string, ProjectionLag>;
};

export type DeliveryHealth = {
  status: string;
  transport: {
    mode: string;
    status: string;
    detail: string;
    broker: string | null;
  };
  outbox: {
    pending: number;
    retrying: number;
    dead_letter: number;
    published_total: number;
    due_now: number;
    pending_avg_attempts: number;
    oldest_pending_age_ms: number | null;
    last_published_at: string | null;
  };
  throughput: {
    published_last_5m: number;
    published_last_1h: number;
  };
  topics: Array<{
    topic: string;
    pending: number;
    dead_letter: number;
    published_last_1h: number;
  }>;
  updated_at: string;
};

export type AnalyticsSummary = {
  generated_at: string;
  window_days: 7 | 30 | 90;
  kpis: {
    submitted: number;
    approved: number;
    declined: number;
    finalized: number;
    approval_rate_pct: number | null;
    requested_volume_usd: number;
    approved_volume_usd: number;
    avg_turnaround_hours: number;
    median_turnaround_hours: number;
    turnaround_sample_size: number;
  };
  funnel: {
    submitted_apps: number;
    analyzed_apps: number;
    compliance_completed_apps: number;
    decisioned_apps: number;
    finalized_apps: number;
  };
  compliance: {
    checks_completed: number;
    checks_cleared: number;
    cleared_rate_pct: number | null;
    failed_rule_events: number;
    top_failed_rules: Array<{
      rule_id: string;
      rule_version: string;
      failures: number;
    }>;
  };
  human_review: {
    reviews_total: number;
    overrides: number;
    override_rate_pct: number | null;
  };
  agent_leaderboard: Array<{
    agent_id: string;
    decisions_generated: number;
    avg_confidence_score: number | null;
    approved_outcomes: number;
    declined_outcomes: number;
    approval_rate_pct: number | null;
  }>;
  approval_trend: Array<{
    day: string;
    submitted: number;
    approved: number;
    declined: number;
    approval_rate_pct: number | null;
  }>;
  approval_trend_recent_7d: Array<{
    day: string;
    submitted: number;
    approved: number;
    declined: number;
    approval_rate_pct: number | null;
  }>;
};

export type MetricsSummary = {
  generated_at: string;
  window_days: 7 | 30 | 90;
  submitted: number;
  finalized: number;
  approved: number;
  declined: number;
  approval_rate: number;
  avg_processing_time: number;
};

export type MetricsDailyPoint = {
  date: string;
  submitted: number;
  approved: number;
  declined: number;
  avg_processing_seconds: number;
};

export type MetricsAgentPoint = {
  agent_id: string;
  activity_score: number;
  decisions: number;
  approval_rate: number;
};

export type ApplicationSummary = {
  application_id: string;
  current_state: string;
  decision_recommendation: string | null;
  final_decision: string | null;
  requested_amount_usd: number | null;
  approved_amount_usd: number | null;
  assessed_max_limit_usd: number | null;
  compliance_status: string | null;
  last_event_type: string;
  last_global_position: number;
  updated_at: string;
};

export type ComplianceTimelineEvent = {
  global_position: number;
  recorded_at: string;
  event_type: string;
  compliance_status: string;
  rule_id?: string;
  rule_version?: string;
  failure_reason?: string;
};

export type ComplianceView = {
  application_id: string;
  as_of: string | null;
  snapshot: Record<string, unknown>;
  timeline: ComplianceTimelineEvent[];
};

export type AgentModelPerformance = {
  agent_id: string;
  model_version: string;
  sessions_started: number;
  analyses_completed: number;
  fraud_screenings_completed: number;
  decisions_recorded: number;
  avg_confidence_score: number;
  updated_at: string;
};

export type AgentPerformance = {
  agent_id: string;
  models: AgentModelPerformance[];
};

export type AppStateCount = {
  state: string;
  count: number;
};

export type RecentEvent = {
  event_id: string;
  stream_id: string;
  stream_position: number;
  global_position: number;
  event_type: string;
  event_version: number;
  recorded_at: string;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
};

export type AuditTrail = {
  stream_id: string;
  events: RecentEvent[];
};

export type AgentSessionReplay = {
  stream_id: string;
  events: RecentEvent[];
};

export type ResourceDefinition = {
  uri: string;
  description: string;
};

export type ToolDefinition = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};
