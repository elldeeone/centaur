use axum::http::HeaderMap;
use base64::{Engine, engine::general_purpose::URL_SAFE};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use std::{
    env,
    time::{SystemTime, UNIX_EPOCH},
};

use crate::error::{ControlError, Result};

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Debug)]
pub struct ApiKeyInfo {
    pub id: String,
    pub scopes: Vec<String>,
    pub source: &'static str,
}

#[derive(Debug, Deserialize)]
struct SandboxClaims {
    #[allow(dead_code)]
    thread_key: Option<String>,
    container_id: String,
    expires_at: u64,
}

pub async fn authorize(pool: &PgPool, headers: &HeaderMap, required: &str) -> Result<ApiKeyInfo> {
    let token = bearer_token(headers).ok_or(ControlError::Unauthorized)?;
    let info = if token.starts_with("sbx1.") {
        verify_sandbox_token(token)?
    } else {
        lookup_db_key(pool, token).await?
    };
    if !scope_allows(&info.scopes, required) {
        return Err(ControlError::Forbidden(format!(
            "API key scope does not permit '{required}'"
        )));
    }
    Ok(info)
}

#[cfg_attr(not(feature = "kube-client"), allow(dead_code))]
pub fn mint_sandbox_token(thread_key: &str, container_id: &str) -> Result<String> {
    let ttl = env::var("SANDBOX_TOKEN_TTL_SECONDS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(7200);
    let expires_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ControlError::Unauthorized)?
        .as_secs()
        + ttl;
    let payload = serde_json::json!({
        "thread_key": thread_key,
        "container_id": container_id,
        "expires_at": expires_at,
    });
    let payload_b64 = URL_SAFE.encode(
        serde_json::to_vec(&payload).map_err(|err| ControlError::Internal(err.to_string()))?,
    );
    let key = env::var("SANDBOX_SIGNING_KEY")
        .or_else(|_| env::var("API_SECRET_KEY"))
        .map_err(|_| ControlError::Unauthorized)?;
    let mut mac =
        HmacSha256::new_from_slice(key.as_bytes()).map_err(|_| ControlError::Unauthorized)?;
    mac.update(payload_b64.as_bytes());
    let sig_b64 = URL_SAFE.encode(mac.finalize().into_bytes());
    Ok(format!("sbx1.{payload_b64}.{sig_b64}"))
}

fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    if let Some(value) = headers.get("x-api-key").and_then(|v| v.to_str().ok()) {
        return Some(value.trim()).filter(|v| !v.is_empty());
    }
    let auth = headers.get("authorization")?.to_str().ok()?.trim();
    auth.to_ascii_lowercase()
        .strip_prefix("bearer ")
        .map(|_| auth[7..].trim())
        .filter(|v| !v.is_empty())
}

async fn lookup_db_key(pool: &PgPool, token: &str) -> Result<ApiKeyInfo> {
    let key_hash = format!("{:x}", Sha256::digest(token.as_bytes()));
    let row = sqlx::query(
        "SELECT id::text AS id, scopes FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL",
    )
    .bind(key_hash)
    .fetch_optional(pool)
    .await?;
    let row = row.ok_or(ControlError::Unauthorized)?;
    Ok(ApiKeyInfo {
        id: row.try_get::<Option<String>, _>("id")?.unwrap_or_default(),
        scopes: row
            .try_get::<Option<Vec<String>>, _>("scopes")?
            .unwrap_or_default(),
        source: "db",
    })
}

fn verify_sandbox_token(token: &str) -> Result<ApiKeyInfo> {
    let mut parts = token.split('.');
    if parts.next() != Some("sbx1") {
        return Err(ControlError::Unauthorized);
    }
    let payload_b64 = parts.next().ok_or(ControlError::Unauthorized)?;
    let sig_b64 = parts.next().ok_or(ControlError::Unauthorized)?;
    if parts.next().is_some() {
        return Err(ControlError::Unauthorized);
    }

    let key = env::var("SANDBOX_SIGNING_KEY")
        .or_else(|_| env::var("API_SECRET_KEY"))
        .map_err(|_| ControlError::Unauthorized)?;
    let mut mac =
        HmacSha256::new_from_slice(key.as_bytes()).map_err(|_| ControlError::Unauthorized)?;
    mac.update(payload_b64.as_bytes());
    let provided = URL_SAFE
        .decode(sig_b64)
        .map_err(|_| ControlError::Unauthorized)?;
    mac.verify_slice(&provided)
        .map_err(|_| ControlError::Unauthorized)?;

    let payload = URL_SAFE
        .decode(payload_b64)
        .map_err(|_| ControlError::Unauthorized)?;
    let claims: SandboxClaims =
        serde_json::from_slice(&payload).map_err(|_| ControlError::Unauthorized)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ControlError::Unauthorized)?
        .as_secs();
    if now > claims.expires_at {
        return Err(ControlError::Unauthorized);
    }
    Ok(ApiKeyInfo {
        id: claims.container_id,
        scopes: vec!["agent".into(), "tools:*".into()],
        source: "sandbox",
    })
}

fn scope_allows(scopes: &[String], required: &str) -> bool {
    if scopes.iter().any(|scope| scope == "*") {
        return true;
    }
    let (category, action) = required
        .split_once(':')
        .filter(|(category, _)| *category != "tools" && *category != "workflows")
        .unwrap_or((required, ""));
    scopes
        .iter()
        .any(|scope| scope == category || (!action.is_empty() && scope == required))
}

#[cfg(test)]
mod tests {
    use super::scope_allows;

    #[test]
    fn bare_agent_scope_allows_execute() {
        assert!(scope_allows(&["agent".into()], "agent:execute"));
    }

    #[test]
    fn unrelated_scope_does_not_allow_agent_execute() {
        assert!(!scope_allows(&["tools:*".into()], "agent:execute"));
    }
}
