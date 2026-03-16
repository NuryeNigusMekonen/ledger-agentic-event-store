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

COMMIT;

