CREATE TABLE IF NOT EXISTS control_agent_runtime_assignments (
    thread_ref                  TEXT PRIMARY KEY,
    sandbox_id                  TEXT NOT NULL,
    harness                     TEXT NOT NULL,
    persona_id                  TEXT,
    prompt_ref                  TEXT NOT NULL,
    system_prompt_sha256        TEXT NOT NULL,
    harness_session_id          TEXT,
    state_volume_ref            TEXT,
    resume_json                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    state                       TEXT NOT NULL
                                CHECK (state IN ('active', 'released')),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_control_assignments_sandbox
    ON control_agent_runtime_assignments (sandbox_id)
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS control_agent_executions (
    execution_id        UUID PRIMARY KEY,
    request_id          TEXT NOT NULL UNIQUE,
    thread_ref          TEXT NOT NULL,
    sandbox_id          TEXT,
    harness             TEXT NOT NULL,
    actor_ref           JSONB NOT NULL DEFAULT '{}'::jsonb,
    turn_policy         TEXT NOT NULL
                        CHECK (turn_policy IN ('steer', 'enqueue')),
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'queued',
                            'running',
                            'steered',
                            'cancelling',
                            'completed',
                            'failed',
                            'cancelled'
                        )),
    trace_id            TEXT,
    traceparent         TEXT,
    terminal_error      TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    terminal_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_control_executions_thread_status
    ON control_agent_executions (thread_ref, status, created_at);

CREATE INDEX IF NOT EXISTS idx_control_executions_status_created
    ON control_agent_executions (status, created_at);
