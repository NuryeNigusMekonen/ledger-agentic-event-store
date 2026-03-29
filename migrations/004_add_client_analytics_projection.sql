BEGIN;

CREATE TABLE IF NOT EXISTS client_analytics_projection (
  application_id TEXT PRIMARY KEY,
  submitted_at TIMESTAMPTZ,
  finalized_at TIMESTAMPTZ,
  final_decision TEXT,
  requested_amount_usd DOUBLE PRECISION,
  approved_amount_usd DOUBLE PRECISION,
  decision_agent_id TEXT,
  decision_generated_at TIMESTAMPTZ,
  processing_time_hours DOUBLE PRECISION,
  last_global_position BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_client_analytics_submitted_at
  ON client_analytics_projection (submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_client_analytics_finalized_at
  ON client_analytics_projection (finalized_at DESC);
CREATE INDEX IF NOT EXISTS idx_client_analytics_agent
  ON client_analytics_projection (decision_agent_id, finalized_at DESC);

COMMIT;
