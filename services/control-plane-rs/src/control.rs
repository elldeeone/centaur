use std::{collections::HashMap, sync::Arc};

use axum::response::sse::{Event, KeepAlive};
use futures::{Stream, StreamExt};
use serde_json::json;
use tokio::sync::{Mutex, Semaphore, broadcast};
use uuid::Uuid;

use crate::{
    db::{Db, RuntimeAssignment},
    error::{ControlError, Result},
    sandbox::{SandboxClient, SandboxSpec},
    types::{AgentTurnRequest, ControlEvent, TurnEnvelope, TurnPolicy},
};

#[derive(Clone)]
pub struct ControlPlane {
    db: Db,
    sandbox: Arc<dyn SandboxClient>,
    hub: Arc<EventHub>,
    assignment_locks: Arc<Mutex<HashMap<String, Arc<Mutex<()>>>>>,
    thread_locks: Arc<Mutex<HashMap<String, Arc<Mutex<()>>>>>,
    turn_slots: Arc<Semaphore>,
}

impl ControlPlane {
    pub fn new(db: Db, sandbox: Arc<dyn SandboxClient>, max_concurrent_turns: usize) -> Self {
        Self {
            db,
            sandbox,
            hub: Arc::new(EventHub::default()),
            assignment_locks: Arc::new(Mutex::new(HashMap::new())),
            thread_locks: Arc::new(Mutex::new(HashMap::new())),
            turn_slots: Arc::new(Semaphore::new(max_concurrent_turns.max(1))),
        }
    }

    pub async fn submit_turn(
        &self,
        req: AgentTurnRequest,
    ) -> Result<broadcast::Receiver<ControlEvent>> {
        req.validate()?;
        let execution = self.db.create_execution(&req).await?;
        let tx = self.hub.sender(execution.execution_id).await;
        let rx = tx.subscribe();

        if !execution.inserted {
            let _ = tx.send(ControlEvent::new(
                "execution.duplicate",
                execution.execution_id,
                execution.thread_ref,
                json!({
                    "status": execution.status,
                    "request_id": execution.request_id,
                    "duplicate": true
                }),
            ));
            return Ok(rx);
        }

        let assignment_lock = named_lock(&self.assignment_locks, &req.thread_ref).await;
        let _assignment_guard = assignment_lock.lock().await;
        let active = self
            .db
            .active_execution(&req.thread_ref, execution.execution_id)
            .await?;
        if active.is_some() && req.turn_policy == TurnPolicy::Steer {
            let assignment = match self.db.assignment(&req.thread_ref).await? {
                Some(assignment) => assignment,
                None => {
                    let err = ControlError::Conflict(
                        "thread assignment is not ready for steering".into(),
                    );
                    let _ = self
                        .db
                        .mark_terminal(execution.execution_id, "failed", Some(&err.to_string()))
                        .await;
                    return Err(err);
                }
            };
            if assignment.harness != req.runtime.harness.as_str() {
                let err = ControlError::Conflict(
                    "runtime.harness cannot change after thread assignment; start a new thread"
                        .into(),
                );
                let _ = self
                    .db
                    .mark_terminal(execution.execution_id, "failed", Some(&err.to_string()))
                    .await;
                return Err(err);
            }
            if req.system_prompt.has_override() {
                let err = ControlError::Conflict(
                    "system_prompt override is only accepted when creating a thread assignment"
                        .into(),
                );
                let _ = self
                    .db
                    .mark_terminal(execution.execution_id, "failed", Some(&err.to_string()))
                    .await;
                return Err(err);
            }
            self.db.touch_assignment(&req.thread_ref).await?;
            self.spawn_steer(req, execution.execution_id, assignment, tx);
        } else {
            let assignment = match self.ensure_assignment(&req).await {
                Ok(assignment) => assignment,
                Err(err) => {
                    let _ = self
                        .db
                        .mark_terminal(execution.execution_id, "failed", Some(&err.to_string()))
                        .await;
                    return Err(err);
                }
            };
            self.spawn_turn(req, execution.execution_id, assignment, tx)
                .await;
        }
        Ok(rx)
    }

    pub async fn cancel(&self, execution_id: Uuid) -> Result<()> {
        let target = self
            .db
            .mark_cancelling(execution_id)
            .await?
            .ok_or_else(|| ControlError::Conflict("execution is not cancellable".into()))?;
        let assignment = self
            .db
            .assignment(&target.thread_ref)
            .await?
            .ok_or_else(|| ControlError::Conflict("thread assignment is not active".into()))?;
        self.sandbox
            .interrupt(assignment, target.execution_id)
            .await?;
        self.db
            .mark_terminal(execution_id, "cancelled", Some("cancelled"))
            .await
    }

    async fn ensure_assignment(&self, req: &AgentTurnRequest) -> Result<RuntimeAssignment> {
        if let Some(active) = self.db.assignment(&req.thread_ref).await? {
            if active.harness != req.runtime.harness.as_str() {
                return Err(ControlError::Conflict(
                    "runtime.harness cannot change after thread assignment; start a new thread"
                        .into(),
                ));
            }
            if req.system_prompt.has_override() {
                return Err(ControlError::Conflict(
                    "system_prompt override is only accepted when creating a thread assignment"
                        .into(),
                ));
            }
            self.db.touch_assignment(&req.thread_ref).await?;
            return Ok(active);
        }

        let lease = self
            .sandbox
            .ensure(SandboxSpec {
                thread_ref: req.thread_ref.clone(),
                harness: req.runtime.harness.clone(),
                model: req.runtime.model.clone(),
                system_prompt: req.system_prompt.clone(),
                trace_id: req.trace_id.clone(),
            })
            .await?;
        self.db
            .create_assignment(
                &req.thread_ref,
                &lease.sandbox_id,
                &req.runtime.harness,
                &req.system_prompt,
                lease.state_volume_ref.as_deref(),
            )
            .await
    }

    async fn spawn_turn(
        &self,
        req: AgentTurnRequest,
        execution_id: Uuid,
        assignment: RuntimeAssignment,
        tx: broadcast::Sender<ControlEvent>,
    ) {
        let db = self.db.clone();
        let sandbox = Arc::clone(&self.sandbox);
        let locks = Arc::clone(&self.thread_locks);
        let slots = Arc::clone(&self.turn_slots);
        let thread_ref = req.thread_ref.clone();
        tokio::spawn(async move {
            let result = async {
                let _slot = slots
                    .acquire_owned()
                    .await
                    .map_err(|_| ControlError::Capacity("turn semaphore closed".into()))?;
                let thread_lock = named_lock(&locks, &thread_ref).await;
                let _thread_guard = thread_lock.lock().await;
                emit(
                    &tx,
                    "execution.started",
                    execution_id,
                    &thread_ref,
                    json!({}),
                );
                emit(
                    &tx,
                    "sandbox.ready",
                    execution_id,
                    &thread_ref,
                    json!({
                        "sandbox_id": assignment.sandbox_id,
                        "assignment_thread_ref": assignment.thread_ref,
                        "harness": assignment.harness,
                        "persona_id": assignment.persona_id,
                        "prompt_ref": assignment.prompt_ref,
                        "system_prompt_sha256": assignment.system_prompt_sha256,
                        "harness_session_id": assignment.harness_session_id,
                        "state_volume_ref": assignment.state_volume_ref,
                        "resume": assignment.resume_json,
                    }),
                );
                db.mark_running(execution_id, &assignment.sandbox_id)
                    .await?;
                let mut stream = sandbox
                    .start_turn(assignment.clone(), TurnEnvelope::start(execution_id, &req))
                    .await?;
                let mut terminal_status = "completed";
                let mut terminal_error = None;
                while let Some(item) = stream.next().await {
                    let raw = item?;
                    if let Some((status, error)) = terminal_projection(&raw) {
                        terminal_status = status;
                        terminal_error = error;
                    }
                    emit(&tx, "harness.raw", execution_id, &thread_ref, raw);
                }
                db.mark_terminal(execution_id, terminal_status, terminal_error.as_deref())
                    .await?;
                emit(
                    &tx,
                    "execution.terminal",
                    execution_id,
                    &thread_ref,
                    json!({"status": terminal_status}),
                );
                Ok::<(), ControlError>(())
            }
            .await;

            if let Err(err) = result {
                let _ = db
                    .mark_terminal(execution_id, "failed", Some(&err.to_string()))
                    .await;
                emit(
                    &tx,
                    "execution.error",
                    execution_id,
                    &thread_ref,
                    json!({"code": err.code(), "message": err.to_string()}),
                );
            }
        });
    }

    fn spawn_steer(
        &self,
        req: AgentTurnRequest,
        execution_id: Uuid,
        assignment: RuntimeAssignment,
        tx: broadcast::Sender<ControlEvent>,
    ) {
        let db = self.db.clone();
        let sandbox = Arc::clone(&self.sandbox);
        let thread_ref = req.thread_ref.clone();
        tokio::spawn(async move {
            emit(
                &tx,
                "execution.started",
                execution_id,
                &thread_ref,
                json!({"mode": "steer"}),
            );
            match sandbox
                .steer(assignment.clone(), TurnEnvelope::steer(execution_id, &req))
                .await
            {
                Ok(raw) => {
                    let _ = db.mark_steered(execution_id, &assignment.sandbox_id).await;
                    emit(&tx, "harness.raw", execution_id, &thread_ref, raw);
                    emit(
                        &tx,
                        "execution.terminal",
                        execution_id,
                        &thread_ref,
                        json!({"status": "steered"}),
                    );
                }
                Err(err) => {
                    let _ = db
                        .mark_terminal(execution_id, "failed", Some(&err.to_string()))
                        .await;
                    emit(
                        &tx,
                        "execution.error",
                        execution_id,
                        &thread_ref,
                        json!({"code": err.code(), "message": err.to_string()}),
                    );
                }
            }
        });
    }
}

async fn named_lock(locks: &Mutex<HashMap<String, Arc<Mutex<()>>>>, key: &str) -> Arc<Mutex<()>> {
    let mut guard = locks.lock().await;
    guard
        .entry(key.to_owned())
        .or_insert_with(|| Arc::new(Mutex::new(())))
        .clone()
}

#[derive(Default)]
struct EventHub {
    senders: Mutex<HashMap<Uuid, broadcast::Sender<ControlEvent>>>,
}

impl EventHub {
    async fn sender(&self, execution_id: Uuid) -> broadcast::Sender<ControlEvent> {
        let mut guard = self.senders.lock().await;
        guard
            .entry(execution_id)
            .or_insert_with(|| broadcast::channel(256).0)
            .clone()
    }
}

fn emit(
    tx: &broadcast::Sender<ControlEvent>,
    event_type: &'static str,
    execution_id: Uuid,
    thread_ref: &str,
    data: serde_json::Value,
) {
    let _ = tx.send(ControlEvent::new(
        event_type,
        execution_id,
        thread_ref,
        data,
    ));
}

fn terminal_projection(raw: &serde_json::Value) -> Option<(&'static str, Option<String>)> {
    match raw.get("type").and_then(serde_json::Value::as_str)? {
        "error" | "turn.failed" => Some(("failed", raw_error(raw))),
        "result"
            if string_field(raw, "terminal_reason").is_some_and(is_interrupted)
                || string_field(raw, "stop_reason").is_some_and(is_interrupted)
                || string_field(raw, "result").is_some_and(is_interrupted) =>
        {
            Some(("cancelled", raw_error(raw)))
        }
        "result" if raw.get("is_error").and_then(serde_json::Value::as_bool) == Some(true) => {
            Some(("failed", raw_error(raw)))
        }
        "result" => Some(("completed", None)),
        "turn.completed"
            if raw
                .pointer("/turn/error")
                .is_some_and(|value| !value.is_null()) =>
        {
            Some(("failed", raw_error(raw)))
        }
        "turn.completed" => Some(("completed", None)),
        _ => None,
    }
}

fn string_field<'a>(raw: &'a serde_json::Value, field: &str) -> Option<&'a str> {
    raw.get(field).and_then(serde_json::Value::as_str)
}

fn is_interrupted(value: &str) -> bool {
    matches!(value, "interrupted" | "cancelled" | "canceled")
}

fn raw_error(raw: &serde_json::Value) -> Option<String> {
    raw.get("message")
        .or_else(|| raw.get("error"))
        .or_else(|| raw.pointer("/turn/error"))
        .map(ToString::to_string)
}

pub fn sse_stream(
    mut rx: broadcast::Receiver<ControlEvent>,
) -> impl Stream<Item = std::result::Result<Event, std::convert::Infallible>> {
    async_stream::stream! {
        loop {
            match rx.recv().await {
                Ok(event) => {
                    let sse = Event::default()
                        .event(event.event_type)
                        .json_data(&event)
                        .unwrap_or_else(|_| Event::default().event("execution.error").data("serialization error"));
                    yield Ok(sse);
                    if matches!(event.event_type, "execution.terminal" | "execution.error" | "execution.duplicate") {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    }
}

pub fn keep_alive() -> KeepAlive {
    KeepAlive::new()
        .interval(std::time::Duration::from_secs(15))
        .text("keepalive")
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::terminal_projection;

    #[test]
    fn interrupted_result_projects_cancelled() {
        let (status, error) = terminal_projection(&json!({
            "type": "result",
            "terminal_reason": "interrupted",
            "result": "interrupted"
        }))
        .expect("terminal result");
        assert_eq!(status, "cancelled");
        assert!(error.is_none());
    }

    #[test]
    fn failed_turn_projects_failed() {
        let (status, error) = terminal_projection(&json!({
            "type": "turn.completed",
            "turn": {"error": {"message": "boom"}}
        }))
        .expect("terminal turn");
        assert_eq!(status, "failed");
        assert_eq!(error.as_deref(), Some("{\"message\":\"boom\"}"));
    }
}
