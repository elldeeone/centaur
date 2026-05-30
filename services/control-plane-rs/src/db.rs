use serde_json::Value;
use sqlx::{PgPool, postgres::PgPoolOptions};
use uuid::Uuid;

use crate::{
    error::Result,
    types::{AgentTurnRequest, Harness, SystemPromptSpec},
};

#[derive(Clone)]
pub struct Db {
    pub pool: PgPool,
}

#[derive(Clone, Debug)]
pub struct RuntimeAssignment {
    pub thread_ref: String,
    pub sandbox_id: String,
    pub harness: String,
    pub persona_id: Option<String>,
    pub prompt_ref: String,
    pub system_prompt_sha256: String,
    pub harness_session_id: Option<String>,
    pub state_volume_ref: Option<String>,
    pub resume_json: Value,
}

#[derive(Clone, Debug)]
pub struct ExecutionRecord {
    pub execution_id: Uuid,
    pub request_id: String,
    pub thread_ref: String,
    pub status: String,
    pub inserted: bool,
}

#[derive(Clone, Debug)]
pub struct ExecutionTarget {
    pub execution_id: Uuid,
    pub thread_ref: String,
    pub sandbox_id: String,
}

impl Db {
    pub async fn connect(database_url: &str) -> Result<Self> {
        let pool = PgPoolOptions::new()
            .max_connections(env_u32("CONTROL_PLANE_DB_MAX_CONNECTIONS", 10))
            .connect(database_url)
            .await?;
        if env_bool("CONTROL_PLANE_RUN_MIGRATIONS", true) {
            sqlx::migrate!("./migrations").run(&pool).await?;
        }
        Ok(Self { pool })
    }

    pub async fn assignment(&self, thread_ref: &str) -> Result<Option<RuntimeAssignment>> {
        let row = sqlx::query_as::<_, RuntimeAssignmentRow>(
            "SELECT thread_ref, sandbox_id, harness, persona_id, prompt_ref, system_prompt_sha256,
                    harness_session_id, state_volume_ref, resume_json
             FROM control_agent_runtime_assignments
             WHERE thread_ref = $1 AND state = 'active'",
        )
        .bind(thread_ref)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.map(Into::into))
    }

    pub async fn create_assignment(
        &self,
        thread_ref: &str,
        sandbox_id: &str,
        harness: &Harness,
        prompt: &SystemPromptSpec,
        state_volume_ref: Option<&str>,
    ) -> Result<RuntimeAssignment> {
        let prompt_ref = prompt.prompt_ref(harness);
        let prompt_sha = prompt.sha256(harness);
        let row = sqlx::query_as::<_, RuntimeAssignmentRow>(
            "INSERT INTO control_agent_runtime_assignments (
                thread_ref, sandbox_id, harness, persona_id, prompt_ref,
                system_prompt_sha256, state_volume_ref, state
             )
             VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
             RETURNING thread_ref, sandbox_id, harness, persona_id, prompt_ref,
                    system_prompt_sha256, harness_session_id, state_volume_ref, resume_json",
        )
        .bind(thread_ref)
        .bind(sandbox_id)
        .bind(harness.as_str())
        .bind(prompt.persona_id.as_deref())
        .bind(prompt_ref)
        .bind(prompt_sha)
        .bind(state_volume_ref)
        .fetch_one(&self.pool)
        .await?;
        Ok(row.into())
    }

    pub async fn touch_assignment(&self, thread_ref: &str) -> Result<()> {
        sqlx::query(
            "UPDATE control_agent_runtime_assignments
             SET last_used_at = NOW(), updated_at = NOW()
             WHERE thread_ref = $1 AND state = 'active'",
        )
        .bind(thread_ref)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn create_execution(&self, req: &AgentTurnRequest) -> Result<ExecutionRecord> {
        let execution_id = Uuid::new_v4();
        let inserted = sqlx::query_as::<_, ExecutionRecordRow>(
            "INSERT INTO control_agent_executions (
                execution_id, request_id, thread_ref, harness, actor_ref,
                turn_policy, status, trace_id, traceparent
             )
             VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7, $8)
             ON CONFLICT (request_id) DO NOTHING
             RETURNING execution_id, request_id, thread_ref, status",
        )
        .bind(execution_id)
        .bind(&req.request_id)
        .bind(&req.thread_ref)
        .bind(req.runtime.harness.as_str())
        .bind(&req.actor_ref)
        .bind(req.turn_policy.as_str())
        .bind(&req.trace_id)
        .bind(&req.traceparent)
        .fetch_optional(&self.pool)
        .await?;
        if let Some(row) = inserted {
            return Ok(ExecutionRecord::from_row(row, true));
        }

        let row = sqlx::query_as::<_, ExecutionRecordRow>(
            "SELECT execution_id, request_id, thread_ref, status
             FROM control_agent_executions
             WHERE request_id = $1",
        )
        .bind(&req.request_id)
        .fetch_one(&self.pool)
        .await?;
        Ok(ExecutionRecord::from_row(row, false))
    }

    pub async fn mark_running(&self, execution_id: Uuid, sandbox_id: &str) -> Result<()> {
        sqlx::query(
            "UPDATE control_agent_executions
             SET status = 'running', sandbox_id = $2, started_at = COALESCE(started_at, NOW()),
                 updated_at = NOW()
             WHERE execution_id = $1",
        )
        .bind(execution_id)
        .bind(sandbox_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn mark_steered(&self, execution_id: Uuid, sandbox_id: &str) -> Result<()> {
        sqlx::query(
            "UPDATE control_agent_executions
             SET status = 'steered', sandbox_id = $2, started_at = COALESCE(started_at, NOW()),
                 terminal_at = NOW(), updated_at = NOW()
             WHERE execution_id = $1",
        )
        .bind(execution_id)
        .bind(sandbox_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn mark_terminal(
        &self,
        execution_id: Uuid,
        status: &str,
        error: Option<&str>,
    ) -> Result<()> {
        sqlx::query(
            "UPDATE control_agent_executions
             SET status = $2, terminal_error = $3, terminal_at = NOW(), updated_at = NOW()
             WHERE execution_id = $1 AND status <> 'cancelled'",
        )
        .bind(execution_id)
        .bind(status)
        .bind(error)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn mark_cancelling(&self, execution_id: Uuid) -> Result<Option<ExecutionTarget>> {
        let row = sqlx::query_as::<_, ExecutionTargetRow>(
            "UPDATE control_agent_executions
             SET status = 'cancelling', updated_at = NOW()
             WHERE execution_id = $1 AND status IN ('queued', 'running', 'steered')
             RETURNING execution_id, thread_ref, COALESCE(sandbox_id, '') AS sandbox_id",
        )
        .bind(execution_id)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.and_then(Into::into))
    }

    pub async fn active_execution(
        &self,
        thread_ref: &str,
        exclude_execution_id: Uuid,
    ) -> Result<Option<Uuid>> {
        let row = sqlx::query_scalar::<_, Uuid>(
            "SELECT execution_id FROM control_agent_executions
             WHERE thread_ref = $1
               AND execution_id <> $2
               AND status IN ('queued', 'running', 'cancelling')
             ORDER BY started_at DESC NULLS LAST, created_at DESC
             LIMIT 1",
        )
        .bind(thread_ref)
        .bind(exclude_execution_id)
        .fetch_optional(&self.pool)
        .await?;
        Ok(row)
    }
}

#[derive(sqlx::FromRow)]
struct RuntimeAssignmentRow {
    thread_ref: String,
    sandbox_id: String,
    harness: String,
    persona_id: Option<String>,
    prompt_ref: String,
    system_prompt_sha256: String,
    harness_session_id: Option<String>,
    state_volume_ref: Option<String>,
    resume_json: Value,
}

impl From<RuntimeAssignmentRow> for RuntimeAssignment {
    fn from(row: RuntimeAssignmentRow) -> Self {
        Self {
            thread_ref: row.thread_ref,
            sandbox_id: row.sandbox_id,
            harness: row.harness,
            persona_id: row.persona_id,
            prompt_ref: row.prompt_ref,
            system_prompt_sha256: row.system_prompt_sha256,
            harness_session_id: row.harness_session_id,
            state_volume_ref: row.state_volume_ref,
            resume_json: row.resume_json,
        }
    }
}

#[derive(sqlx::FromRow)]
struct ExecutionRecordRow {
    execution_id: Uuid,
    request_id: String,
    thread_ref: String,
    status: String,
}

#[derive(sqlx::FromRow)]
struct ExecutionTargetRow {
    execution_id: Uuid,
    thread_ref: String,
    sandbox_id: String,
}

impl From<ExecutionTargetRow> for Option<ExecutionTarget> {
    fn from(row: ExecutionTargetRow) -> Self {
        if row.sandbox_id.is_empty() {
            return None;
        }
        Some(ExecutionTarget {
            execution_id: row.execution_id,
            thread_ref: row.thread_ref,
            sandbox_id: row.sandbox_id,
        })
    }
}

impl ExecutionRecord {
    fn from_row(row: ExecutionRecordRow, inserted: bool) -> Self {
        Self {
            execution_id: row.execution_id,
            request_id: row.request_id,
            thread_ref: row.thread_ref,
            status: row.status,
            inserted,
        }
    }
}

fn env_u32(name: &str, default: u32) -> u32 {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn env_bool(name: &str, default: bool) -> bool {
    std::env::var(name)
        .ok()
        .map(|value| {
            !matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "0" | "false" | "no" | "off"
            )
        })
        .unwrap_or(default)
}
