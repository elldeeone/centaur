mod auth;
mod control;
mod db;
mod error;
mod sandbox;
mod types;

use std::{env, net::SocketAddr, sync::Arc};

use axum::{
    Json, Router,
    extract::{Path, State},
    http::HeaderMap,
    response::Sse,
    routing::{get, post},
};
use serde_json::json;
use tower_http::trace::TraceLayer;
use tracing_subscriber::{EnvFilter, layer::SubscriberExt, util::SubscriberInitExt};

use crate::{
    control::{ControlPlane, keep_alive, sse_stream},
    db::Db,
    error::Result,
    sandbox::{FakeSandboxClient, SandboxClient},
    types::AgentTurnRequest,
};

#[derive(Clone)]
struct AppState {
    db: Db,
    control: ControlPlane,
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();
    let database_url =
        env::var("DATABASE_URL").expect("DATABASE_URL is required for centaur-control-plane");
    let db = Db::connect(&database_url).await?;
    let sandbox = sandbox_client().await?;
    let control = ControlPlane::new(
        db.clone(),
        sandbox,
        env_usize("CONTROL_PLANE_MAX_TURNS", 128),
    );
    let state = AppState { db, control };

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(healthz))
        .route("/agent-turns", post(agent_turn))
        .route("/agent-turns/{execution_id}/cancel", post(cancel_turn))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let addr: SocketAddr = env::var("CONTROL_PLANE_BIND")
        .unwrap_or_else(|_| "0.0.0.0:8080".into())
        .parse()
        .expect("CONTROL_PLANE_BIND must be host:port");
    tracing::info!(%addr, "control_plane_listening");
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|err| crate::error::ControlError::Internal(err.to_string()))?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|err| crate::error::ControlError::Internal(err.to_string()))
}

async fn agent_turn(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(req): Json<AgentTurnRequest>,
) -> Result<
    Sse<
        impl futures::Stream<
            Item = std::result::Result<axum::response::sse::Event, std::convert::Infallible>,
        >,
    >,
> {
    let caller = auth::authorize(&state.db.pool, &headers, "agent:execute").await?;
    tracing::info!(
        source = caller.source,
        key_id = caller.id,
        thread_ref = req.thread_ref,
        harness = req.runtime.harness.as_str(),
        request_id = req.request_id,
        "agent_turn_received"
    );
    let rx = state.control.submit_turn(req).await?;
    Ok(Sse::new(sse_stream(rx)).keep_alive(keep_alive()))
}

async fn cancel_turn(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(execution_id): Path<uuid::Uuid>,
) -> Result<Json<serde_json::Value>> {
    let caller = auth::authorize(&state.db.pool, &headers, "agent:execute").await?;
    tracing::info!(
        source = caller.source,
        key_id = caller.id,
        %execution_id,
        "agent_turn_cancel_received"
    );
    state.control.cancel(execution_id).await?;
    Ok(Json(json!({"ok": true, "execution_id": execution_id})))
}

async fn healthz() -> Json<serde_json::Value> {
    Json(json!({"ok": true, "service": "centaur-control-plane"}))
}

async fn sandbox_client() -> Result<Arc<dyn SandboxClient>> {
    match env::var("CONTROL_PLANE_SANDBOX_BACKEND")
        .unwrap_or_else(|_| "fake".into())
        .as_str()
    {
        "fake" => Ok(Arc::new(FakeSandboxClient)),
        #[cfg(feature = "kube-client")]
        "kube" => Ok(Arc::new(
            sandbox::kube_client::KubeSandboxClient::from_env().await?,
        )),
        other => Err(crate::error::ControlError::BadRequest(format!(
            "unknown CONTROL_PLANE_SANDBOX_BACKEND: {other}"
        ))),
    }
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("centaur_control_plane=info,tower_http=info"));
    tracing_subscriber::registry()
        .with(filter)
        .with(tracing_subscriber::fmt::layer().json())
        .init();
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        let mut signal = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
        signal.recv().await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

fn env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}
