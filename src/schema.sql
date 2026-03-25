BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS events (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stream_id TEXT NOT NULL,
  stream_position BIGINT NOT NULL,
  global_position BIGINT GENERATED ALWAYS AS IDENTITY,
  event_type TEXT NOT NULL,
  event_version SMALLINT NOT NULL DEFAULT 1,
  payload JSONB NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_stream_position UNIQUE (stream_id, stream_position)
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id
  ON events (stream_id, stream_position);
CREATE INDEX IF NOT EXISTS idx_events_global_pos
  ON events (global_position);
CREATE INDEX IF NOT EXISTS idx_events_type
  ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_recorded
  ON events (recorded_at);
CREATE INDEX IF NOT EXISTS idx_events_metadata_gin
  ON events USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_events_payload_gin
  ON events USING GIN (payload);

CREATE TABLE IF NOT EXISTS event_streams (
  stream_id TEXT PRIMARY KEY,
  aggregate_type TEXT NOT NULL,
  current_version BIGINT NOT NULL DEFAULT 0 CHECK (current_version >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  archived_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_event_streams_aggregate_type
  ON event_streams (aggregate_type);
CREATE INDEX IF NOT EXISTS idx_event_streams_archived_at
  ON event_streams (archived_at);

CREATE TABLE IF NOT EXISTS projection_checkpoints (
  projection_name TEXT PRIMARY KEY,
  last_global_position BIGINT NOT NULL DEFAULT 0,
  last_event_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_projection_checkpoints_updated_at
  ON projection_checkpoints (updated_at);

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

CREATE TABLE IF NOT EXISTS outbox (
  outbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id UUID NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  payload JSONB NOT NULL,
  headers JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  published_at TIMESTAMPTZ,
  last_error TEXT,
  CONSTRAINT chk_outbox_status
    CHECK (status IN ('pending', 'published', 'dead_letter'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_event_topic
  ON outbox (event_id, topic);
CREATE INDEX IF NOT EXISTS idx_outbox_status_next_attempt
  ON outbox (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS outbox_sink_events (
  sink_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  outbox_id BIGINT NOT NULL UNIQUE REFERENCES outbox(outbox_id) ON DELETE CASCADE,
  event_id UUID NOT NULL,
  topic TEXT NOT NULL,
  payload JSONB NOT NULL,
  headers JSONB NOT NULL DEFAULT '{}'::jsonb,
  delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbox_sink_topic_delivered
  ON outbox_sink_events (topic, delivered_at DESC);

CREATE TABLE IF NOT EXISTS auth_users (
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login_at TIMESTAMPTZ,
  CONSTRAINT chk_auth_role
    CHECK (role IN ('analyst', 'compliance', 'ops', 'admin'))
);

CREATE TABLE IF NOT EXISTS auth_audit_log (
  audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username TEXT,
  role TEXT,
  action TEXT NOT NULL,
  success BOOLEAN NOT NULL DEFAULT FALSE,
  ip_address TEXT,
  user_agent TEXT,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auth_audit_created
  ON auth_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_action
  ON auth_audit_log (action, created_at DESC);

COMMIT;
