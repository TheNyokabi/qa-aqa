"""Postgres DDL. Idempotent — runs on every artefact-service boot.

RLS is FORCED on tenant-scoped tables so even the table owner (the runtime
`app` user) is subject to the policy. Without FORCE, owners bypass RLS and the
substrate guarantees collapse silently.
"""

SCHEMA_SQL = r"""
-- ── artefacts ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS artefacts (
    id              text PRIMARY KEY,
    tenant_id       text NOT NULL DEFAULT 'default',
    type            text NOT NULL,
    version         int  NOT NULL DEFAULT 1,
    state           text NOT NULL DEFAULT 'draft',
    payload         jsonb NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    parent_id       text REFERENCES artefacts(id),
    workflow_id     text,
    actor           text NOT NULL
                    CHECK (actor ~ '^urn:qa-aqa:(agent|user|system|service):[A-Za-z0-9_\-]+(:v[0-9]+)?$'),
    actor_type      text GENERATED ALWAYS AS (split_part(actor, ':', 3)) STORED,
    attestation     jsonb NOT NULL DEFAULT '{}'::jsonb,
    compliance_level text NOT NULL DEFAULT 'none'
                    CHECK (compliance_level IN ('none','gxp','iso17025','sox')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS artefacts_tenant_type_state_idx ON artefacts(tenant_id, type, state);
CREATE INDEX IF NOT EXISTS artefacts_tenant_workflow_idx   ON artefacts(tenant_id, workflow_id);
CREATE INDEX IF NOT EXISTS artefacts_tenant_actor_type_idx ON artefacts(tenant_id, actor_type);

ALTER TABLE artefacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artefacts FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS artefacts_tenant_isolation ON artefacts;
CREATE POLICY artefacts_tenant_isolation ON artefacts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- ── idempotency_keys ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idempotency_keys (
    tenant_id   text NOT NULL,
    key         text NOT NULL,
    artefact_id text NOT NULL REFERENCES artefacts(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
);

ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS idempotency_keys_tenant_isolation ON idempotency_keys;
CREATE POLICY idempotency_keys_tenant_isolation ON idempotency_keys
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- ── artefact_history ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS artefact_history (
    id             bigserial PRIMARY KEY,
    tenant_id      text NOT NULL DEFAULT 'default',
    artefact_id    text NOT NULL REFERENCES artefacts(id) ON DELETE CASCADE,
    version        int  NOT NULL,
    state          text NOT NULL,
    payload        jsonb NOT NULL,
    metadata       jsonb NOT NULL,
    actor          text NOT NULL,
    policy_version text,
    attestation    jsonb NOT NULL DEFAULT '{}'::jsonb,
    changed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS artefact_history_artefact_idx ON artefact_history(tenant_id, artefact_id);

ALTER TABLE artefact_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE artefact_history FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS artefact_history_tenant_isolation ON artefact_history;
CREATE POLICY artefact_history_tenant_isolation ON artefact_history
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
"""
