BEGIN;

CREATE TABLE IF NOT EXISTS application_summary_projection (
  application_id TEXT PRIMARY KEY,
  current_state TEXT NOT NULL,
  decision_recommendation TEXT,
  final_decision TEXT,
  requested_amount_usd DOUBLE PRECISION,
  approved_amount_usd DOUBLE PRECISION,
  assessed_max_limit_usd DOUBLE PRECISION,
  compliance_status TEXT,
  last_event_type TEXT NOT NULL,
  last_global_position BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_application_summary_state
  ON application_summary_projection (current_state);

CREATE TABLE IF NOT EXISTS compliance_audit_state_projection (
  application_id TEXT PRIMARY KEY,
  regulation_set_version TEXT,
  mandatory_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  passed_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
  failed_checks JSONB NOT NULL DEFAULT '{}'::jsonb,
  compliance_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
  last_global_position BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS compliance_audit_view_projection (
  application_id TEXT NOT NULL,
  global_position BIGINT NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  event_type TEXT NOT NULL,
  compliance_status TEXT NOT NULL,
  regulation_set_version TEXT,
  rule_id TEXT,
  rule_version TEXT,
  failure_reason TEXT,
  payload JSONB NOT NULL,
  metadata JSONB NOT NULL,
  PRIMARY KEY (application_id, global_position)
);

CREATE INDEX IF NOT EXISTS idx_compliance_view_recorded
  ON compliance_audit_view_projection (application_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS agent_performance_projection (
  agent_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  sessions_started INTEGER NOT NULL DEFAULT 0,
  analyses_completed INTEGER NOT NULL DEFAULT 0,
  fraud_screenings_completed INTEGER NOT NULL DEFAULT 0,
  decisions_recorded INTEGER NOT NULL DEFAULT 0,
  total_confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  confidence_samples INTEGER NOT NULL DEFAULT 0,
  avg_confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0,
  last_global_position BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (agent_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_agent_performance_updated
  ON agent_performance_projection (updated_at DESC);

COMMIT;

