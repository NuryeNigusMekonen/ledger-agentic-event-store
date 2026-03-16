BEGIN;

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
