use axum::{Json, http::StatusCode, response::IntoResponse};
use serde_json::json;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ControlError {
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("unauthorized")]
    Unauthorized,
    #[error("forbidden: {0}")]
    Forbidden(String),
    #[error("conflict: {0}")]
    Conflict(String),
    #[error("capacity exhausted: {0}")]
    Capacity(String),
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    #[error("migration error: {0}")]
    Migration(#[from] sqlx::migrate::MigrateError),
    #[cfg_attr(not(feature = "kube-client"), allow(dead_code))]
    #[error("sandbox error: {0}")]
    Sandbox(String),
    #[error("internal error: {0}")]
    Internal(String),
}

impl ControlError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::BadRequest(_) => "BAD_REQUEST",
            Self::Unauthorized => "UNAUTHORIZED",
            Self::Forbidden(_) => "FORBIDDEN",
            Self::Conflict(_) => "CONFLICT",
            Self::Capacity(_) => "CAPACITY_EXHAUSTED",
            Self::Db(_) | Self::Migration(_) => "DB_ERROR",
            Self::Sandbox(_) => "SANDBOX_ERROR",
            Self::Internal(_) => "INTERNAL_ERROR",
        }
    }

    pub fn status(&self) -> StatusCode {
        match self {
            Self::BadRequest(_) => StatusCode::UNPROCESSABLE_ENTITY,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::Conflict(_) => StatusCode::CONFLICT,
            Self::Capacity(_) => StatusCode::SERVICE_UNAVAILABLE,
            Self::Db(_) | Self::Migration(_) | Self::Sandbox(_) | Self::Internal(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        }
    }
}

impl IntoResponse for ControlError {
    fn into_response(self) -> axum::response::Response {
        let status = self.status();
        let body = Json(json!({
            "code": self.code(),
            "message": self.to_string(),
        }));
        (status, body).into_response()
    }
}

pub type Result<T> = std::result::Result<T, ControlError>;
