use std::{collections::BTreeMap, env, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use centaur_api_server::{SandboxRuntime, build_router_with_runtime};
use centaur_iron_proxy::{SourceKind, SourcePolicy, load_fragment_files};
use centaur_sandbox_agent_k8s::{AgentSandboxBackend, AgentSandboxConfig, IronProxyPodConfig};
use centaur_sandbox_core::SandboxSpec;
use centaur_sandbox_local::LocalSandboxBackend;
use centaur_session_core::{HarnessType, ThreadKey};
use centaur_session_sqlx::PgSessionStore;
use thiserror::Error;
use tokio::net::TcpListener;
use tracing::info;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() -> Result<(), ServerError> {
    init_tracing();

    let database_url = env::var("DATABASE_URL").map_err(|_| ServerError::MissingDatabaseUrl)?;
    let bind_addr = env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_owned());
    let bind_addr: SocketAddr = bind_addr.parse()?;
    let run_migrations = env::var("RUN_MIGRATIONS")
        .map(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes"))
        .unwrap_or(false);

    let store = PgSessionStore::connect(&database_url).await?;
    if run_migrations {
        store.run_migrations().await?;
    }
    let sandbox_runtime = sandbox_runtime_from_env().await?;

    let listener = TcpListener::bind(bind_addr).await?;
    info!(%bind_addr, "starting centaur api-rs server");

    axum::serve(listener, build_router_with_runtime(store, sandbox_runtime))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).json().init();
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

async fn sandbox_runtime_from_env() -> Result<SandboxRuntime, ServerError> {
    let backend = env::var("SESSION_SANDBOX_BACKEND").unwrap_or_else(|_| "mock".to_owned());
    match backend.as_str() {
        "mock" => Ok(SandboxRuntime::Mock),
        "local" => {
            let workload =
                env::var("SESSION_SANDBOX_WORKLOAD").unwrap_or_else(|_| "mock".to_owned());
            let backend = Arc::new(LocalSandboxBackend::new());
            match workload.as_str() {
                "mock" => Ok(SandboxRuntime::backend(
                    backend,
                    local_mock_app_server_spec(),
                )),
                "codex-app-server" => {
                    let harness_server = env::var("SESSION_LOCAL_HARNESS_SERVER_BIN")
                        .unwrap_or_else(|_| {
                            "crates/harness-server/target/debug/harness-server".to_owned()
                        });
                    let env_template = codex_app_server_env_template();
                    Ok(SandboxRuntime::backend_with_spec_factory(
                        backend,
                        move |thread_key, harness_type, _execution_id| {
                            local_codex_app_server_spec(
                                &harness_server,
                                harness_server_kind(harness_type),
                                thread_key,
                                &env_template,
                            )
                        },
                    ))
                }
                other => Err(ServerError::InvalidSandboxWorkload(other.to_owned())),
            }
        }
        "agent-k8s" => {
            let namespace = env::var("SESSION_SANDBOX_K8S_NAMESPACE")
                .unwrap_or_else(|_| "centaur-sandbox-e2e".to_owned());
            let workload =
                env::var("SESSION_SANDBOX_WORKLOAD").unwrap_or_else(|_| "mock".to_owned());
            let image =
                env::var("SESSION_SANDBOX_IMAGE").unwrap_or_else(|_| match workload.as_str() {
                    "codex-app-server" => "centaur-agent:latest".to_owned(),
                    _ => "busybox:1.36".to_owned(),
                });
            let mut config = AgentSandboxConfig::new(namespace);
            config.image_pull_policy = env::var("SESSION_SANDBOX_IMAGE_PULL_POLICY").ok();
            config.ready_timeout = Duration::from_secs(
                env::var("SESSION_SANDBOX_READY_TIMEOUT_SECS")
                    .ok()
                    .and_then(|value| value.parse().ok())
                    .unwrap_or(90),
            );
            config.iron_proxy = iron_proxy_config_from_env()?;

            let client = if let Ok(context) = env::var("SESSION_SANDBOX_K8S_CONTEXT") {
                let kube_config = kube::Config::from_kubeconfig(&kube::config::KubeConfigOptions {
                    context: Some(context),
                    ..kube::config::KubeConfigOptions::default()
                })
                .await?;
                kube::Client::try_from(kube_config)?
            } else {
                kube::Client::try_default().await?
            };
            let backend = AgentSandboxBackend::new(client, config);

            match workload.as_str() {
                "mock" => Ok(SandboxRuntime::backend(
                    Arc::new(backend),
                    agent_k8s_mock_app_server_spec(&image),
                )),
                "codex-app-server" => {
                    let env_template = codex_app_server_env_template();
                    Ok(SandboxRuntime::backend_with_spec_factory(
                        Arc::new(backend),
                        move |thread_key, harness_type, _execution_id| {
                            codex_app_server_spec(&image, harness_type, thread_key, &env_template)
                        },
                    ))
                }
                other => Err(ServerError::InvalidSandboxWorkload(other.to_owned())),
            }
        }
        other => Err(ServerError::InvalidSandboxBackend(other.to_owned())),
    }
}

fn local_mock_app_server_spec() -> SandboxSpec {
    SandboxSpec::new("/bin/sh")
        .command(["/bin/sh", "-lc"])
        .args([mock_app_server_script()])
}

fn local_codex_app_server_spec(
    harness_server: &str,
    harness_kind: &str,
    thread_key: &ThreadKey,
    env_template: &[(String, String)],
) -> SandboxSpec {
    let script = format!(
        "exec {} {} 2>/tmp/centaur-harness-server-{}.$$.stderr",
        shell_quote(harness_server),
        shell_quote(harness_kind),
        shell_quote(harness_kind)
    );
    let mut spec = SandboxSpec::new("/bin/sh")
        .command(["/bin/sh", "-lc"])
        .args([script])
        .env("CENTAUR_THREAD_KEY", thread_key.as_str());
    for (name, value) in env_template {
        spec = spec.env(name.clone(), value.clone());
    }
    spec
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn agent_k8s_mock_app_server_spec(image: &str) -> SandboxSpec {
    SandboxSpec::new(image)
        .command(["/bin/sh", "-lc"])
        .args([mock_app_server_script()])
}

fn iron_proxy_config_from_env() -> Result<Option<IronProxyPodConfig>, ServerError> {
    let fragment_paths = iron_proxy_fragment_paths();
    if !env_bool("SESSION_SANDBOX_IRON_PROXY_ENABLED") && fragment_paths.is_empty() {
        return Ok(None);
    }
    let ca_cert_secret_name = env::var("SESSION_SANDBOX_IRON_PROXY_CA_CERT_SECRET_NAME")
        .or_else(|_| env::var("KUBERNETES_FIREWALL_CA_SECRET_NAME"))
        .map_err(|_| ServerError::MissingIronProxyCaSecret)?;
    let ca_key_secret_name = env::var("SESSION_SANDBOX_IRON_PROXY_CA_KEY_SECRET_NAME")
        .or_else(|_| env::var("KUBERNETES_FIREWALL_CA_KEY_SECRET_NAME"))
        .map_err(|_| ServerError::MissingIronProxyCaSecret)?;
    let image = env::var("SESSION_SANDBOX_IRON_PROXY_IMAGE")
        .or_else(|_| env::var("KUBERNETES_IRON_PROXY_IMAGE"))
        .unwrap_or_else(|_| "centaur-iron-proxy:latest".to_owned());
    let mut config = IronProxyPodConfig::new(image, ca_cert_secret_name, ca_key_secret_name)
        .with_fragments(load_fragment_files(&fragment_paths)?);
    config.image_pull_policy = env::var("SESSION_SANDBOX_IRON_PROXY_IMAGE_PULL_POLICY")
        .or_else(|_| env::var("KUBERNETES_IRON_PROXY_IMAGE_PULL_POLICY"))
        .ok();
    config.source_policy = SourcePolicy::from_env();
    if let Some(secret_name) = env::var("SESSION_SANDBOX_IRON_PROXY_ENV_SECRET")
        .or_else(|_| env::var("KUBERNETES_SECRET_ENV_NAME"))
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
    {
        config.env_from_secret_names.push(secret_name);
    }
    if matches!(config.source_policy.kind, SourceKind::OnePassword) {
        if let Some(secret_name) = env::var("KUBERNETES_BOOTSTRAP_SECRET_NAME")
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
        {
            config.env_from_secret_names.push(secret_name);
        }
    }
    if let Ok(app_name) = env::var("KUBERNETES_OP_CONNECT_APP_NAME") {
        config.op_connect_app_name = app_name;
    }
    config.op_connect_port = env::var("KUBERNETES_OP_CONNECT_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .or_else(|| {
            env::var("KUBERNETES_OP_CONNECT_HOST")
                .ok()
                .and_then(|value| parse_host_port(&value))
        })
        .unwrap_or(config.op_connect_port);
    if let Ok(selector) = env::var("KUBERNETES_API_POD_LABEL_SELECTOR") {
        let labels = parse_label_selector(&selector);
        if !labels.is_empty() {
            config.api_pod_labels = labels;
        }
    }
    config.harness_auth_modes = harness_auth_modes_from_env();
    push_optional_proxy_env(
        &mut config.extra_env,
        "OP_CONNECT_HOST",
        env::var("SESSION_SANDBOX_OP_CONNECT_HOST")
            .or_else(|_| env::var("KUBERNETES_OP_CONNECT_HOST"))
            .ok(),
    );
    push_optional_proxy_env(
        &mut config.extra_env,
        "IRON_BROKER_URL",
        env::var("SESSION_SANDBOX_IRON_BROKER_URL")
            .or_else(|_| env::var("KUBERNETES_TOKEN_BROKER_URL"))
            .ok(),
    );
    Ok(Some(config))
}

fn iron_proxy_fragment_paths() -> Vec<PathBuf> {
    env::var("SESSION_SANDBOX_IRON_PROXY_FRAGMENT_PATHS")
        .unwrap_or_default()
        .split([',', ':'])
        .map(str::trim)
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .collect()
}

fn harness_auth_modes_from_env() -> BTreeMap<String, String> {
    let mut modes = BTreeMap::new();
    if let Ok(mode) = env::var("CODEX_AUTH_MODE") {
        modes.insert("codex".to_owned(), mode);
    }
    if let Ok(mode) = env::var("CLAUDE_CODE_AUTH_MODE") {
        modes.insert("claude-code".to_owned(), mode);
    }
    modes
}

fn push_optional_proxy_env(envs: &mut BTreeMap<String, String>, name: &str, value: Option<String>) {
    if let Some(value) = value
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
    {
        envs.insert(name.to_owned(), value);
    }
}

fn parse_host_port(value: &str) -> Option<u16> {
    value.rsplit_once(':')?.1.parse().ok()
}

fn parse_label_selector(value: &str) -> BTreeMap<String, String> {
    value
        .split(',')
        .filter_map(|item| {
            let (key, value) = item.split_once('=')?;
            let key = key.trim();
            let value = value.trim();
            (!key.is_empty() && !value.is_empty()).then(|| (key.to_owned(), value.to_owned()))
        })
        .collect()
}

fn env_bool(name: &str) -> bool {
    env::var(name)
        .map(|value| matches!(value.trim(), "1" | "true" | "TRUE" | "yes" | "YES"))
        .unwrap_or(false)
}

fn codex_app_server_spec(
    image: &str,
    harness_type: &HarnessType,
    thread_key: &ThreadKey,
    env_template: &[(String, String)],
) -> SandboxSpec {
    let mut spec = SandboxSpec::new(image)
        .args(["harness-server", harness_server_kind(harness_type)])
        .env("CENTAUR_THREAD_KEY", thread_key.as_str())
        .env("CENTAUR_HARNESS_KIND", harness_server_kind(harness_type));
    for (name, value) in env_template {
        spec = spec.env(name.clone(), value.clone());
    }
    spec
}

fn harness_server_kind(harness_type: &HarnessType) -> &str {
    match harness_type.as_str() {
        "claude" | "claude-code" => "claude-code",
        "amp" => "amp",
        "codex" => "codex",
        other => other,
    }
}

fn codex_app_server_env_template() -> Vec<(String, String)> {
    let mut envs = Vec::new();
    push_env(
        &mut envs,
        "CENTAUR_API_URL",
        env::var("SESSION_SANDBOX_CENTAUR_API_URL")
            .or_else(|_| env::var("CENTAUR_API_URL"))
            .unwrap_or_else(|_| "http://api:8000".to_owned()),
    );
    if let Ok(api_key) =
        env::var("SESSION_SANDBOX_CENTAUR_API_KEY").or_else(|_| env::var("CENTAUR_API_KEY"))
    {
        push_env(&mut envs, "CENTAUR_API_KEY", api_key);
    }
    for name in ["CLAUDE_CODE_AUTH_MODE", "CODEX_AUTH_MODE"] {
        if let Ok(value) = env::var(name) {
            push_env(&mut envs, name, value);
        }
    }

    for name in passthrough_env_names() {
        if let Ok(value) = env::var(&name) {
            push_env(&mut envs, &name, value);
        }
    }

    envs
}

fn passthrough_env_names() -> impl Iterator<Item = String> {
    env::var("SESSION_SANDBOX_PASSTHROUGH_ENV")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>()
        .into_iter()
}

fn push_env(envs: &mut Vec<(String, String)>, name: &str, value: String) {
    if let Some((_, existing_value)) = envs
        .iter_mut()
        .find(|(existing_name, _)| existing_name == name)
    {
        *existing_value = value;
    } else {
        envs.push((name.to_owned(), value));
    }
}

fn mock_app_server_script() -> &'static str {
    r#"while IFS= read -r line; do
printf '%s\n' '{"type":"system","subtype":"wrapper_heartbeat","phase":"startup"}'
sleep 0.2
printf '%s\n' '{"type":"system","subtype":"wrapper_heartbeat","phase":"app_server_started"}'
sleep 0.2
printf '%s\n' '{"type":"thread.started","thread_id":"mock-codex-thread"}'
sleep 0.2
turn_index=1
while [ "$turn_index" -le 3 ]; do
  turn_id="mock-turn-$turn_index"
  printf '{"type":"turn.started","turn_id":"%s"}\n' "$turn_id"
  sleep 0.2
  printf '{"type":"item.agentMessage.delta","turnId":"%s","session_id":"mock-codex-thread","delta":"PONG %s"}\n' "$turn_id" "$turn_index"
  sleep 0.2
  printf '{"type":"turn.completed","turn":{"id":"%s"},"usage":{"input_tokens":0,"output_tokens":1}}\n' "$turn_id"
  sleep 0.2
  turn_index=$((turn_index + 1))
done
done"#
}

#[derive(Debug, Error)]
enum ServerError {
    #[error("DATABASE_URL is required")]
    MissingDatabaseUrl,
    #[error(
        "SESSION_SANDBOX_IRON_PROXY_CA_CERT_SECRET_NAME/KUBERNETES_FIREWALL_CA_SECRET_NAME and SESSION_SANDBOX_IRON_PROXY_CA_KEY_SECRET_NAME/KUBERNETES_FIREWALL_CA_KEY_SECRET_NAME are required when SESSION_SANDBOX_IRON_PROXY_ENABLED is set"
    )]
    MissingIronProxyCaSecret,
    #[error("unknown SESSION_SANDBOX_BACKEND {0:?}; expected mock, local, or agent-k8s")]
    InvalidSandboxBackend(String),
    #[error("unknown SESSION_SANDBOX_WORKLOAD {0:?}; expected mock or codex-app-server")]
    InvalidSandboxWorkload(String),
    #[error(transparent)]
    AddrParse(#[from] std::net::AddrParseError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Store(#[from] centaur_session_sqlx::SessionStoreError),
    #[error(transparent)]
    Sandbox(#[from] centaur_sandbox_core::SandboxError),
    #[error(transparent)]
    IronProxy(#[from] centaur_iron_proxy::IronProxyConfigError),
    #[error(transparent)]
    KubeConfig(#[from] kube::config::KubeconfigError),
    #[error(transparent)]
    KubeInferConfig(#[from] kube::config::InferConfigError),
    #[error(transparent)]
    Kube(#[from] kube::Error),
}
