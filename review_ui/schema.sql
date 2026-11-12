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

-- RC1-575 (ADR-0023): whose session is it, so an IdP push can end every
-- session of a deprovisioned user at once. Nullable: sessions signed in
-- before this migration simply age out (8h).
ALTER TABLE review_sso_sessions ADD COLUMN IF NOT EXISTS subject text;
ALTER TABLE review_sso_sessions ADD COLUMN IF NOT EXISTS email text;
CREATE INDEX IF NOT EXISTS review_sso_sessions_subject ON review_sso_sessions (subject);
CREATE INDEX IF NOT EXISTS review_sso_sessions_email ON review_sso_sessions (lower(email));

-- Users the IdP has provisioned over SCIM (RC1-575). The IdP is the source
-- of truth; this is its push target, holding what it sent so the connector's
-- reads and updates round-trip. Mutable by design; the decisions it drives
-- (deactivations) land in review_audit.
CREATE TABLE IF NOT EXISTS review_scim_users (
    id           text PRIMARY KEY,
    user_name    text NOT NULL,
    active       boolean NOT NULL,
    attributes   jsonb NOT NULL,
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS review_scim_users_user_name
    ON review_scim_users (lower(user_name));

-- RC1-575 follow-up: a revoked session stays as a marker until it expires,
-- so concurrent requests that discover the revocation write exactly one
-- access_revoked record between them (the one that flips
-- revocation_audited).
ALTER TABLE review_sso_sessions ADD COLUMN IF NOT EXISTS revoked_at timestamptz;
ALTER TABLE review_sso_sessions
    ADD COLUMN IF NOT EXISTS revocation_audited boolean NOT NULL DEFAULT false;

-- RC1-640 (ADR-0027): which release's schema this database holds, so an
-- installer can decide whether an application rollback is safe without
-- trusting its own records. One row, written by `db_setup migrate` from
-- SCHEMA_VERSION / SCHEMA_COMPATIBLE_WITH in db_setup.py. compatible_with is
-- the oldest schema version whose application code can run against this
-- schema; a rollback to a release older than that must restore data instead.
CREATE TABLE IF NOT EXISTS review_schema_version (
    singleton        boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version          integer NOT NULL CHECK (version > 0),
    compatible_with  integer NOT NULL CHECK (compatible_with > 0 AND compatible_with <= version),
    applied_at       timestamptz NOT NULL DEFAULT now()
);
