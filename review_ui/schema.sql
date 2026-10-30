-- Durable review-gate state (RC1-576, ADR-0022). Applied by the database
-- OWNER via `python -m review_ui.db_setup migrate`; idempotent. The app
-- connects as a separate, least-privilege role (esp_app) whose grants live in
-- db_setup.py, never as the owner.

-- The gate's decision log. Append-only is enforced twice: esp_app is granted
-- INSERT and SELECT only, and these triggers refuse UPDATE, DELETE and
-- TRUNCATE for every role, including the owner (only a superuser disabling the
-- trigger, or dropping the table, gets past them - both visible in Neon's
-- operation history). The sha256 chain from RC1-554 stays: the database stops
-- the app from rewriting history, the chain proves nobody else did.
CREATE TABLE IF NOT EXISTS review_audit (
    seq          bigint PRIMARY KEY CHECK (seq > 0),
    at           text NOT NULL,          -- the exact string the digest covers
    event        text NOT NULL,
    actor        jsonb NOT NULL,
    detail       jsonb NOT NULL,
    prev_sha256  char(64) NOT NULL,
    sha256       char(64) NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION review_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'review_audit is append-only: % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

DROP TRIGGER IF EXISTS review_audit_no_rewrite ON review_audit;
CREATE TRIGGER review_audit_no_rewrite
    BEFORE UPDATE OR DELETE ON review_audit
    FOR EACH ROW EXECUTE FUNCTION review_audit_append_only();

DROP TRIGGER IF EXISTS review_audit_no_truncate ON review_audit;
CREATE TRIGGER review_audit_no_truncate
    BEFORE TRUNCATE ON review_audit
    FOR EACH STATEMENT EXECUTE FUNCTION review_audit_append_only();

-- SSO sessions: the refresh token that lets the gate re-ask the IdP
-- (ADR-0021), encrypted at rest (Fernet, key outside the database), so a
-- deploy or restart no longer signs SSO users out. Mutable by design.
CREATE TABLE IF NOT EXISTS review_sso_sessions (
    sid                text PRIMARY KEY,
    refresh_token_enc  text NOT NULL,
    checked_at         timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS review_sso_sessions_expires ON review_sso_sessions (expires_at);
