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
  status: string;
  updated_at: string;
};

export type LedgerHealth = {
  projections: Record<string, ProjectionLag>;
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

export type ToolDefinition = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
};
