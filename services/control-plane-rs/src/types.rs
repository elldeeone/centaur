use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::error::{ControlError, Result};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Harness {
    Codex,
    #[serde(rename = "claude-code")]
    ClaudeCode,
    Amp,
}

impl Harness {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::ClaudeCode => "claude-code",
            Self::Amp => "amp",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeSpec {
    pub harness: Harness,
    #[serde(default)]
    pub model: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct SystemPromptSpec {
    #[serde(default)]
    pub persona_id: Option<String>,
    #[serde(default)]
    pub override_text: Option<String>,
}

impl SystemPromptSpec {
    pub fn has_override(&self) -> bool {
        self.persona_id
            .as_deref()
            .is_some_and(|v| !v.trim().is_empty())
            || self
                .override_text
                .as_deref()
                .is_some_and(|v| !v.trim().is_empty())
    }

    pub fn prompt_ref(&self, harness: &Harness) -> String {
        if let Some(persona) = self.persona_id.as_deref().filter(|v| !v.trim().is_empty()) {
            return format!("persona:{}", persona.trim());
        }
        format!("harness:{}", harness.as_str())
    }

    pub fn sha256(&self, harness: &Harness) -> String {
        let fallback = self.prompt_ref(harness);
        let source = self
            .override_text
            .as_deref()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or(&fallback);
        let mut hasher = Sha256::new();
        hasher.update(source.as_bytes());
        format!("{:x}", hasher.finalize())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TurnPolicy {
    Steer,
    Enqueue,
}

impl Default for TurnPolicy {
    fn default() -> Self {
        Self::Steer
    }
}

impl TurnPolicy {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Steer => "steer",
            Self::Enqueue => "enqueue",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AgentTurnRequest {
    pub request_id: String,
    pub thread_ref: String,
    #[serde(default)]
    pub actor_ref: Value,
    pub runtime: RuntimeSpec,
    #[serde(default)]
    pub system_prompt: SystemPromptSpec,
    pub content: Vec<Value>,
    #[serde(default)]
    pub attachments: Vec<Value>,
    #[serde(default)]
    pub turn_policy: TurnPolicy,
    #[serde(default)]
    pub trace_id: Option<String>,
    #[serde(default)]
    pub traceparent: Option<String>,
    #[serde(default)]
    pub trace_context: Value,
}

impl AgentTurnRequest {
    pub fn validate(&self) -> Result<()> {
        if self.request_id.trim().is_empty() {
            return Err(ControlError::BadRequest("request_id is required".into()));
        }
        if self.thread_ref.trim().is_empty() {
            return Err(ControlError::BadRequest("thread_ref is required".into()));
        }
        if self.content.is_empty() {
            return Err(ControlError::BadRequest("content must not be empty".into()));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ControlEvent {
    #[serde(rename = "type")]
    pub event_type: &'static str,
    pub execution_id: Uuid,
    pub thread_ref: String,
    #[serde(skip_serializing_if = "Value::is_null")]
    pub data: Value,
}

impl ControlEvent {
    pub fn new(
        event_type: &'static str,
        execution_id: Uuid,
        thread_ref: impl Into<String>,
        data: Value,
    ) -> Self {
        Self {
            event_type,
            execution_id,
            thread_ref: thread_ref.into(),
            data,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct TurnEnvelope {
    #[serde(rename = "type")]
    pub envelope_type: &'static str,
    pub execution_id: Uuid,
    pub thread_ref: String,
    pub message: Value,
    pub attachments: Vec<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub traceparent: Option<String>,
    #[serde(default)]
    pub trace_context: Value,
}

impl TurnEnvelope {
    pub fn start(execution_id: Uuid, req: &AgentTurnRequest) -> Self {
        Self {
            envelope_type: "turn.start",
            execution_id,
            thread_ref: req.thread_ref.clone(),
            message: json!({"role": "user", "content": req.content}),
            attachments: req.attachments.clone(),
            trace_id: req.trace_id.clone(),
            traceparent: req.traceparent.clone(),
            trace_context: req.trace_context.clone(),
        }
    }

    pub fn steer(execution_id: Uuid, req: &AgentTurnRequest) -> Self {
        Self {
            envelope_type: "turn.steer",
            ..Self::start(execution_id, req)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_empty_content() {
        let req = AgentTurnRequest {
            request_id: "r1".into(),
            thread_ref: "t1".into(),
            actor_ref: Value::Null,
            runtime: RuntimeSpec {
                harness: Harness::Codex,
                model: None,
            },
            system_prompt: SystemPromptSpec::default(),
            content: vec![],
            attachments: vec![],
            turn_policy: TurnPolicy::Steer,
            trace_id: None,
            traceparent: None,
            trace_context: Value::Null,
        };
        assert!(req.validate().is_err());
    }

    #[test]
    fn prompt_ref_prefers_persona_and_hashes_override() {
        let prompt = SystemPromptSpec {
            persona_id: Some(" eng ".into()),
            override_text: Some("custom prompt".into()),
        };
        assert!(prompt.has_override());
        assert_eq!(prompt.prompt_ref(&Harness::Codex), "persona:eng");
        assert_eq!(prompt.sha256(&Harness::Codex), prompt.sha256(&Harness::Amp));

        let fallback = SystemPromptSpec::default();
        assert_ne!(
            fallback.sha256(&Harness::Codex),
            fallback.sha256(&Harness::Amp)
        );
    }
}
