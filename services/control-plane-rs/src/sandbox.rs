use std::{env, pin::Pin, time::Duration};

use async_stream::try_stream;
use async_trait::async_trait;
use futures::Stream;
use serde_json::{Value, json};
use tokio::time::sleep;
use uuid::Uuid;

use crate::{
    db::RuntimeAssignment,
    error::Result,
    types::{Harness, SystemPromptSpec, TurnEnvelope},
};

pub type RawEventStream = Pin<Box<dyn Stream<Item = Result<Value>> + Send>>;

#[derive(Clone, Debug)]
pub struct SandboxSpec {
    pub thread_ref: String,
    pub harness: Harness,
    #[cfg_attr(not(feature = "kube-client"), allow(dead_code))]
    pub model: Option<String>,
    pub system_prompt: SystemPromptSpec,
    pub trace_id: Option<String>,
}

#[derive(Clone, Debug)]
pub struct SandboxLease {
    pub sandbox_id: String,
    pub state_volume_ref: Option<String>,
}

#[async_trait]
pub trait SandboxClient: Send + Sync {
    async fn ensure(&self, spec: SandboxSpec) -> Result<SandboxLease>;
    async fn start_turn(
        &self,
        assignment: RuntimeAssignment,
        envelope: TurnEnvelope,
    ) -> Result<RawEventStream>;
    async fn steer(&self, assignment: RuntimeAssignment, envelope: TurnEnvelope) -> Result<Value>;
    async fn interrupt(&self, assignment: RuntimeAssignment, execution_id: Uuid) -> Result<()>;
}

#[derive(Default)]
pub struct FakeSandboxClient;

#[async_trait]
impl SandboxClient for FakeSandboxClient {
    async fn ensure(&self, spec: SandboxSpec) -> Result<SandboxLease> {
        tracing::info!(
            thread_ref = spec.thread_ref,
            harness = spec.harness.as_str(),
            persona_id = spec.system_prompt.persona_id,
            has_prompt_override = spec.system_prompt.override_text.is_some(),
            trace_id = spec.trace_id,
            "fake_sandbox_ensure"
        );
        Ok(SandboxLease {
            sandbox_id: stable_sandbox_id(&spec.thread_ref),
            state_volume_ref: Some(format!("state-{}", stable_sandbox_id(&spec.thread_ref))),
        })
    }

    async fn start_turn(
        &self,
        assignment: RuntimeAssignment,
        envelope: TurnEnvelope,
    ) -> Result<RawEventStream> {
        let harness = assignment.harness.clone();
        let execution_id = envelope.execution_id;
        Ok(Box::pin(try_stream! {
            yield json!({
                "type": "system",
                "subtype": "fake_sandbox_started",
                "harness": harness,
                "execution_id": execution_id,
            });
            sleep(fake_turn_delay()).await;
            yield json!({
                "type": "result",
                "subtype": "success",
                "result": "fake sandbox completed turn",
                "execution_id": execution_id,
            });
        }))
    }

    async fn steer(&self, assignment: RuntimeAssignment, envelope: TurnEnvelope) -> Result<Value> {
        Ok(json!({
            "type": "system",
            "subtype": "fake_sandbox_steered",
            "sandbox_id": assignment.sandbox_id,
            "execution_id": envelope.execution_id,
        }))
    }

    async fn interrupt(&self, _assignment: RuntimeAssignment, _execution_id: Uuid) -> Result<()> {
        Ok(())
    }
}

fn stable_sandbox_id(thread_ref: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(thread_ref.as_bytes());
    format!("cplane-{}", hex_prefix(&digest, 20))
}

fn hex_prefix(bytes: &[u8], len: usize) -> String {
    bytes
        .iter()
        .flat_map(|byte| [byte >> 4, byte & 0x0f])
        .take(len)
        .map(|nibble| char::from_digit(nibble as u32, 16).unwrap())
        .collect()
}

fn fake_turn_delay() -> Duration {
    env::var("CONTROL_PLANE_FAKE_TURN_DELAY_MS")
        .ok()
        .and_then(|value| value.parse().ok())
        .map(Duration::from_millis)
        .unwrap_or_else(|| Duration::from_millis(25))
}

#[cfg(feature = "kube-client")]
pub mod kube_client {
    use std::{collections::HashMap, pin::Pin, sync::Arc, time::Instant};

    use k8s_openapi::api::core::v1::Pod;
    use kube::{
        Api, Client,
        api::{AttachParams, DeleteParams, PostParams},
        core::{ApiResource, DynamicObject, GroupVersionKind},
    };
    use tokio::{
        io::{AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader},
        sync::{Mutex, broadcast},
        time::{Duration, sleep},
    };

    use crate::{auth::mint_sandbox_token, error::ControlError};

    use super::*;

    const BASE_PROMPT: &str = include_str!("../../sandbox/SYSTEM_PROMPT.md");
    const CONTAINER_NAME: &str = "sandbox";
    const PROXY_LABEL: &str = "centaur.ai/iron-proxy";
    const READY_TIMEOUT: Duration = Duration::from_secs(180);

    type StdinWriter = Pin<Box<dyn AsyncWrite + Send>>;

    pub struct KubeSandboxClient {
        client: Client,
        namespace: String,
        image: String,
        proxy_image: String,
        api_url: String,
        secret_name: String,
        secret_prefix: String,
        firewall_ca_secret: String,
        firewall_ca_key_secret: String,
        runtimes: Arc<Mutex<HashMap<String, Arc<AttachedRuntime>>>>,
    }

    struct AttachedRuntime {
        stdin: Mutex<StdinWriter>,
        tx: broadcast::Sender<Value>,
    }

    impl KubeSandboxClient {
        pub async fn from_env() -> Result<Self> {
            Ok(Self {
                client: Client::try_default()
                    .await
                    .map_err(|err| ControlError::Sandbox(err.to_string()))?,
                namespace: std::env::var("KUBERNETES_NAMESPACE")
                    .unwrap_or_else(|_| "centaur".into()),
                image: std::env::var("KUBERNETES_SANDBOX_IMAGE")
                    .unwrap_or_else(|_| "centaur-agent:latest".into()),
                proxy_image: std::env::var("KUBERNETES_IRON_PROXY_IMAGE")
                    .unwrap_or_else(|_| "centaur-iron-proxy:latest".into()),
                api_url: std::env::var("AGENT_API_URL")
                    .unwrap_or_else(|_| "http://api:8000".into()),
                secret_name: std::env::var("KUBERNETES_SECRET_ENV_NAME")
                    .unwrap_or_else(|_| "centaur-infra-env".into()),
                secret_prefix: std::env::var("KUBERNETES_SECRET_ENV_PREFIX").unwrap_or_default(),
                firewall_ca_secret: std::env::var("KUBERNETES_FIREWALL_CA_SECRET_NAME")
                    .unwrap_or_else(|_| "centaur-firewall-ca".into()),
                firewall_ca_key_secret: std::env::var("KUBERNETES_FIREWALL_CA_KEY_SECRET_NAME")
                    .unwrap_or_else(|_| "centaur-firewall-ca-key".into()),
                runtimes: Arc::new(Mutex::new(HashMap::new())),
            })
        }

        fn sandbox_api(&self) -> Api<DynamicObject> {
            let gvk = GroupVersionKind::gvk("agents.x-k8s.io", "v1alpha1", "Sandbox");
            self.dynamic_api(&gvk)
        }

        fn dynamic_api(&self, gvk: &GroupVersionKind) -> Api<DynamicObject> {
            Api::namespaced_with(
                self.client.clone(),
                &self.namespace,
                &ApiResource::from_gvk(&gvk),
            )
        }

        fn pods(&self) -> Api<Pod> {
            Api::namespaced(self.client.clone(), &self.namespace)
        }

        async fn ensure_attached(
            &self,
            assignment: &RuntimeAssignment,
        ) -> Result<Arc<AttachedRuntime>> {
            let mut runtimes = self.runtimes.lock().await;
            if let Some(runtime) = runtimes.get(&assignment.sandbox_id).cloned() {
                return Ok(runtime);
            }

            let pods = self.pods();
            let mut attached = pods
                .attach(
                    &assignment.sandbox_id,
                    &AttachParams::default()
                        .container(CONTAINER_NAME)
                        .stdin(true)
                        .stdout(true)
                        .stderr(false)
                        .tty(false)
                        .max_stdin_buf_size(64 * 1024)
                        .max_stdout_buf_size(256 * 1024),
                )
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            let stdin = attached.stdin().ok_or_else(|| {
                ControlError::Sandbox("kubernetes attach did not provide stdin".into())
            })?;
            let stdout = attached.stdout().ok_or_else(|| {
                ControlError::Sandbox("kubernetes attach did not provide stdout".into())
            })?;
            let (tx, _) = broadcast::channel(512);
            let runtime = Arc::new(AttachedRuntime {
                stdin: Mutex::new(Box::pin(stdin)),
                tx: tx.clone(),
            });
            let sandbox_id = assignment.sandbox_id.clone();
            tokio::spawn(async move {
                let mut lines = BufReader::new(stdout).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    let trimmed = line.trim();
                    if trimmed.is_empty() {
                        continue;
                    }
                    let value = serde_json::from_str(trimmed).unwrap_or_else(
                        |_| json!({"type": "system", "subtype": "raw_stdout", "line": trimmed}),
                    );
                    let _ = tx.send(value);
                }
                let _ = attached.join().await;
                tracing::info!(sandbox_id, "kube_sandbox_attach_closed");
            });
            runtimes.insert(assignment.sandbox_id.clone(), runtime.clone());
            Ok(runtime)
        }

        async fn write_input(runtime: &AttachedRuntime, payload: Value) -> Result<()> {
            let mut line = serde_json::to_vec(&payload)
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            line.push(b'\n');
            let mut stdin = runtime.stdin.lock().await;
            stdin
                .write_all(&line)
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            stdin
                .flush()
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))
        }

        async fn delete_resource(&self, gvk: GroupVersionKind, name: &str) -> Result<()> {
            let api = self.dynamic_api(&gvk);
            match api.delete(name, &DeleteParams::default()).await {
                Ok(_) => Ok(()),
                Err(kube::Error::Api(err)) if err.code == 404 => Ok(()),
                Err(err) => Err(ControlError::Sandbox(err.to_string())),
            }
        }

        async fn recreate_resource(&self, gvk: GroupVersionKind, body: Value) -> Result<()> {
            let name = body
                .get("metadata")
                .and_then(|m| m.get("name"))
                .and_then(Value::as_str)
                .ok_or_else(|| ControlError::Sandbox("resource metadata.name missing".into()))?;
            self.delete_resource(gvk.clone(), name).await?;
            let obj = serde_json::from_value::<DynamicObject>(body)
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            self.dynamic_api(&gvk)
                .create(&PostParams::default(), &obj)
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            Ok(())
        }

        async fn ensure_proxy(&self, sandbox_id: &str, harness: &Harness) -> Result<String> {
            let proxy_name = proxy_name(sandbox_id);
            self.recreate_resource(
                GroupVersionKind::gvk("", "v1", "ConfigMap"),
                json!({
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": proxy_config_name(sandbox_id), "labels": proxy_labels(sandbox_id)},
                    "data": {"proxy.yaml": render_proxy_yaml(harness)?}
                }),
            )
            .await?;
            self.recreate_resource(
                GroupVersionKind::gvk("", "v1", "Service"),
                json!({
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": proxy_name, "labels": proxy_labels(sandbox_id)},
                    "spec": {
                        "selector": proxy_labels(sandbox_id),
                        "ports": [
                            {"name": "proxy", "port": proxy_port(), "targetPort": proxy_port(), "protocol": "TCP"},
                            {"name": "management", "port": proxy_management_port(), "targetPort": proxy_management_port(), "protocol": "TCP"},
                            {"name": "health", "port": proxy_health_port(), "targetPort": proxy_health_port(), "protocol": "TCP"}
                        ]
                    }
                }),
            )
            .await?;
            self.recreate_resource(
                GroupVersionKind::gvk("networking.k8s.io", "v1", "NetworkPolicy"),
                sandbox_network_policy(sandbox_id),
            )
            .await?;
            self.recreate_resource(
                GroupVersionKind::gvk("networking.k8s.io", "v1", "NetworkPolicy"),
                proxy_network_policy(sandbox_id),
            )
            .await?;
            self.recreate_resource(
                GroupVersionKind::gvk("", "v1", "Pod"),
                json!({
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": proxy_pod_name(sandbox_id), "labels": proxy_labels(sandbox_id)},
                    "spec": {
                        "automountServiceAccountToken": false,
                        "restartPolicy": "Never",
                        "containers": [{
                            "name": "iron-proxy",
                            "image": self.proxy_image,
                            "imagePullPolicy": proxy_pull_policy(),
                            "env": self.proxy_env(),
                            "envFrom": self.proxy_env_from(),
                            "ports": [
                                {"containerPort": proxy_port(), "name": "proxy"},
                                {"containerPort": proxy_management_port(), "name": "management"},
                                {"containerPort": proxy_health_port(), "name": "health"}
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": proxy_health_port()},
                                "periodSeconds": 5,
                                "failureThreshold": 30
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": proxy_health_port()}
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": false,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"}
                            },
                            "volumeMounts": [
                                {"name": "iron-proxy-config-rendered", "mountPath": "/etc/iron-proxy-rendered", "readOnly": true},
                                {"name": "iron-proxy-config", "mountPath": "/etc/iron-proxy"},
                                {"name": "iron-proxy-certs", "mountPath": "/certs"},
                                {"name": "iron-proxy-ca", "mountPath": "/etc/iron-proxy-ca", "readOnly": true}
                            ],
                            "command": ["/bin/sh", "-ec"],
                            "args": ["cp /etc/iron-proxy-rendered/proxy.yaml /etc/iron-proxy/proxy.yaml && exec /entrypoint.sh"]
                        }],
                        "volumes": [
                            {"name": "iron-proxy-config-rendered", "configMap": {"name": proxy_config_name(sandbox_id)}},
                            {"name": "iron-proxy-config", "emptyDir": {}},
                            {"name": "iron-proxy-certs", "emptyDir": {}},
                            {"name": "iron-proxy-ca", "secret": {"secretName": self.firewall_ca_key_secret}}
                        ]
                    }
                }),
            )
            .await?;
            self.wait_pod_ready(&proxy_pod_name(sandbox_id), false)
                .await?;
            Ok(proxy_name)
        }

        fn proxy_env(&self) -> Vec<Value> {
            let mut env = vec![json!({
                "name": "IRON_MANAGEMENT_API_KEY",
                "valueFrom": {"secretKeyRef": {"name": self.secret_name, "key": self.secret_key("IRON_MANAGEMENT_API_KEY")}}
            })];
            if let Ok(url) = std::env::var("KUBERNETES_TOKEN_BROKER_URL") {
                if !url.trim().is_empty() {
                    env.push(json!({"name": "IRON_BROKER_URL", "value": url}));
                    env.push(json!({
                        "name": "IRON_BROKER_TOKEN",
                        "valueFrom": {"secretKeyRef": {"name": self.secret_name, "key": self.secret_key("IRON_BROKER_TOKEN")}}
                    }));
                }
            }
            if secret_source() == "onepassword-connect" {
                if let Ok(host) = std::env::var("KUBERNETES_OP_CONNECT_HOST") {
                    if !host.trim().is_empty() {
                        env.push(json!({"name": "OP_CONNECT_HOST", "value": host}));
                    }
                }
                env.push(json!({
                    "name": "OP_CONNECT_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": self.secret_name, "key": self.secret_key("OP_CONNECT_TOKEN")}}
                }));
            }
            env
        }

        fn proxy_env_from(&self) -> Vec<Value> {
            let mut env_from = vec![json!({"secretRef": {"name": self.secret_name}})];
            if secret_source() == "onepassword" {
                if let Ok(name) = std::env::var("KUBERNETES_BOOTSTRAP_SECRET_NAME") {
                    if !name.trim().is_empty() {
                        env_from.push(json!({"secretRef": {"name": name}}));
                    }
                }
            }
            env_from
        }

        fn secret_key(&self, name: &str) -> String {
            format!("{}{}", self.secret_prefix, name)
        }

        async fn create_prompt_secret(&self, sandbox_id: &str, spec: &SandboxSpec) -> Result<()> {
            self.recreate_resource(
                GroupVersionKind::gvk("", "v1", "Secret"),
                json!({
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": prompt_secret_name(sandbox_id), "labels": {"centaur.ai/managed": "true"}},
                    "type": "Opaque",
                    "stringData": {"AGENTS_BASE.md": prompt_bundle(&spec.system_prompt)}
                }),
            )
            .await
        }

        async fn create_sandbox(
            &self,
            spec: &SandboxSpec,
            sandbox_id: &str,
            proxy_host: &str,
        ) -> Result<()> {
            let mut pod_spec = json!({
                "automountServiceAccountToken": false,
                "restartPolicy": "Never",
                "containers": [{
                    "name": CONTAINER_NAME,
                    "image": self.image,
                    "imagePullPolicy": sandbox_pull_policy(),
                    "args": [harness_cmd(&spec.harness)],
                    "stdin": true,
                    "tty": false,
                    "workingDir": "/home/agent",
                    "env": self.sandbox_env(spec, sandbox_id, proxy_host)?,
                    "resources": sandbox_resources(),
                    "securityContext": {
                        "allowPrivilegeEscalation": false,
                        "capabilities": {"drop": ["ALL"]},
                        "runAsGroup": 1001,
                        "runAsNonRoot": true,
                        "runAsUser": 1001,
                        "seccompProfile": {"type": "RuntimeDefault"}
                    },
                    "volumeMounts": [
                        {"name": "firewall-ca", "mountPath": "/firewall-certs", "readOnly": true},
                        {"name": "prompt-bundle", "mountPath": "/home/agent/AGENTS_BASE.md", "subPath": "AGENTS_BASE.md", "readOnly": true}
                    ]
                }],
                "volumes": [
                    {"name": "firewall-ca", "secret": {"secretName": self.firewall_ca_secret}},
                    {"name": "prompt-bundle", "secret": {"secretName": prompt_secret_name(sandbox_id)}}
                ]
            });
            if let Some(secrets) = image_pull_secrets() {
                pod_spec["imagePullSecrets"] = secrets;
            }
            if let Ok(runtime_class) = std::env::var("KUBERNETES_SANDBOX_RUNTIME_CLASS_NAME") {
                if !runtime_class.trim().is_empty() {
                    pod_spec["runtimeClassName"] = json!(runtime_class);
                }
            }
            if state_volume_enabled() {
                pod_spec["containers"][0]["volumeMounts"]
                    .as_array_mut()
                    .expect("volumeMounts array")
                    .push(json!({"name": "state", "mountPath": "/home/agent/state"}));
            }

            let mut sandbox_spec = json!({
                "replicas": 1,
                "service": false,
                "shutdownPolicy": "Retain",
                "podTemplate": {
                    "metadata": {
                        "labels": sandbox_labels(sandbox_id, &spec.harness),
                        "annotations": {
                            "centaur.ai/thread-key": spec.thread_ref,
                            "centaur.ai/harness": spec.harness.as_str(),
                            "centaur.ai/engine": spec.harness.as_str()
                        }
                    },
                    "spec": pod_spec
                }
            });
            if state_volume_enabled() {
                sandbox_spec["volumeClaimTemplates"] = json!([{
                    "metadata": {"name": "state"},
                    "spec": state_volume_claim_spec()
                }]);
            }
            let body = serde_json::from_value::<DynamicObject>(json!({
                "apiVersion": "agents.x-k8s.io/v1alpha1",
                "kind": "Sandbox",
                "metadata": {
                    "name": sandbox_id,
                    "labels": sandbox_labels(sandbox_id, &spec.harness),
                    "annotations": {
                        "centaur.ai/thread-key": spec.thread_ref,
                        "centaur.ai/harness": spec.harness.as_str(),
                        "centaur.ai/engine": spec.harness.as_str()
                    }
                },
                "spec": sandbox_spec
            }))
            .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            self.sandbox_api()
                .create(&PostParams::default(), &body)
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            Ok(())
        }

        fn sandbox_env(
            &self,
            spec: &SandboxSpec,
            sandbox_id: &str,
            proxy_host: &str,
        ) -> Result<Vec<Value>> {
            let token = mint_sandbox_token(&spec.thread_ref, sandbox_id)?;
            let mut no_proxy = vec![
                "localhost".to_string(),
                "127.0.0.1".to_string(),
                proxy_host.to_string(),
                "victoriametrics".to_string(),
                "victorialogs".to_string(),
            ];
            if let Some(host) = host_from_url(&self.api_url) {
                no_proxy.push(host);
            }
            let proxy_url = format!("http://{proxy_host}:{}", proxy_port());
            let mut env = vec![
                env_value("CENTAUR_API_URL", &self.api_url),
                env_value("CENTAUR_API_KEY", &token),
                env_value("CENTAUR_THREAD_KEY", &spec.thread_ref),
                env_value("CENTAUR_TRACE_ID", spec.trace_id.as_deref().unwrap_or("")),
                env_value("AMP_MODE", "deep"),
                env_value("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
                env_value("OPENAI_API_KEY", "OPENAI_API_KEY"),
                env_value("AMP_API_KEY", "AMP_API_KEY"),
                env_value("GITHUB_TOKEN", "GITHUB_TOKEN"),
                env_value("FIREWALL_HOST", proxy_host),
                env_value("HTTPS_PROXY", &proxy_url),
                env_value("HTTP_PROXY", &proxy_url),
                env_value("https_proxy", &proxy_url),
                env_value("http_proxy", &proxy_url),
                env_value("NO_PROXY", &no_proxy.join(",")),
                env_value("no_proxy", &no_proxy.join(",")),
                env_value("NODE_EXTRA_CA_CERTS", "/firewall-certs/ca-cert.pem"),
                env_value("REQUESTS_CA_BUNDLE", "/firewall-certs/ca-cert.pem"),
                env_value("SSL_CERT_FILE", "/firewall-certs/ca-cert.pem"),
                env_value("GIT_SSL_CAINFO", "/firewall-certs/ca-cert.pem"),
                env_value("CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY", "1"),
                env_value("CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", "1"),
                env_value("CLAUDE_CODE_PROXY_RESOLVES_HOSTS", "1"),
                env_value("CLAUDE_CODE_CERT_STORE", "bundled,system"),
                env_value("DISABLE_ERROR_REPORTING", "1"),
                env_value("DISABLE_FEEDBACK_COMMAND", "1"),
                env_value("DISABLE_GROWTHBOOK", "1"),
                env_value("DISABLE_UPDATES", "1"),
            ];
            if let Some(model) = spec.model.as_deref().filter(|v| !v.trim().is_empty()) {
                match spec.harness {
                    Harness::ClaudeCode => env.push(env_value("CLAUDE_MODEL", model)),
                    Harness::Codex => env.push(env_value("CODEX_MODEL", model)),
                    Harness::Amp => {}
                }
            }
            if let Some(persona) = spec
                .system_prompt
                .persona_id
                .as_deref()
                .filter(|v| !v.trim().is_empty())
            {
                env.push(env_value("AGENT_PERSONA", persona.trim()));
            }
            for (name, value) in sandbox_extra_env() {
                if !pinned_env(&name) {
                    env.push(env_value(&name, &value));
                }
            }
            Ok(env)
        }

        async fn wait_pod_ready(&self, pod_name: &str, require_ready_file: bool) -> Result<()> {
            let pods = self.pods();
            let start = Instant::now();
            while start.elapsed() < READY_TIMEOUT {
                match pods.get(pod_name).await {
                    Ok(pod) => {
                        if pod_phase(&pod)
                            .as_deref()
                            .is_some_and(|p| p == "failed" || p == "succeeded")
                        {
                            return Err(ControlError::Sandbox(format!(
                                "pod {pod_name} exited before ready"
                            )));
                        }
                        if is_pod_ready(&pod)
                            && (!require_ready_file || self.ready_file_exists(pod_name).await?)
                        {
                            return Ok(());
                        }
                    }
                    Err(kube::Error::Api(err)) if err.code == 404 => {}
                    Err(err) => return Err(ControlError::Sandbox(err.to_string())),
                }
                sleep(Duration::from_millis(500)).await;
            }
            Err(ControlError::Sandbox(format!(
                "pod readiness timed out: {pod_name}"
            )))
        }

        async fn ready_file_exists(&self, pod_name: &str) -> Result<bool> {
            let pods = self.pods();
            let mut attached = match pods
                .exec(
                    pod_name,
                    vec!["test", "-f", "/home/agent/.ready"],
                    &AttachParams::default()
                        .container(CONTAINER_NAME)
                        .stdin(false)
                        .stdout(false)
                        .stderr(false),
                )
                .await
            {
                Ok(attached) => attached,
                Err(_) => return Ok(false),
            };
            let Some(status_rx) = attached.take_status() else {
                attached.abort();
                return Ok(false);
            };
            let status = tokio::time::timeout(Duration::from_secs(3), status_rx)
                .await
                .ok()
                .flatten();
            attached.abort();
            Ok(status.as_ref().and_then(|status| status.status.as_deref()) == Some("Success"))
        }
    }

    #[async_trait]
    impl SandboxClient for KubeSandboxClient {
        async fn ensure(&self, spec: SandboxSpec) -> Result<SandboxLease> {
            let sandbox_id = stable_sandbox_id(&spec.thread_ref);
            let api = self.sandbox_api();
            if api
                .get_opt(&sandbox_id)
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?
                .is_some()
            {
                self.wait_pod_ready(&sandbox_id, false).await?;
                return Ok(SandboxLease {
                    sandbox_id: sandbox_id.clone(),
                    state_volume_ref: state_volume_enabled().then(|| format!("state-{sandbox_id}")),
                });
            }

            let proxy_host = self.ensure_proxy(&sandbox_id, &spec.harness).await?;
            self.create_prompt_secret(&sandbox_id, &spec).await?;
            self.create_sandbox(&spec, &sandbox_id, &proxy_host).await?;
            self.wait_pod_ready(&sandbox_id, false).await?;
            Ok(SandboxLease {
                sandbox_id: sandbox_id.clone(),
                state_volume_ref: state_volume_enabled().then(|| format!("state-{sandbox_id}")),
            })
        }

        async fn start_turn(
            &self,
            assignment: RuntimeAssignment,
            envelope: TurnEnvelope,
        ) -> Result<RawEventStream> {
            let runtime = self.ensure_attached(&assignment).await?;
            let mut rx = runtime.tx.subscribe();
            Self::write_input(&runtime, sandbox_input(&envelope, false)).await?;
            Ok(Box::pin(try_stream! {
                loop {
                    let raw = rx.recv().await.map_err(|err| ControlError::Sandbox(err.to_string()))?;
                    let terminal = is_terminal_event(&raw);
                    yield raw;
                    if terminal {
                        break;
                    }
                }
            }))
        }

        async fn steer(
            &self,
            assignment: RuntimeAssignment,
            envelope: TurnEnvelope,
        ) -> Result<Value> {
            let runtime = self.ensure_attached(&assignment).await?;
            Self::write_input(&runtime, sandbox_input(&envelope, true)).await?;
            Ok(json!({
                "type": "system",
                "subtype": "steer_sent",
                "sandbox_id": assignment.sandbox_id,
                "execution_id": envelope.execution_id,
            }))
        }

        async fn interrupt(
            &self,
            assignment: RuntimeAssignment,
            _execution_id: Uuid,
        ) -> Result<()> {
            let attached = self
                .pods()
                .exec(
                    &assignment.sandbox_id,
                    vec!["kill", "-USR1", "1"],
                    &AttachParams::default()
                        .container(CONTAINER_NAME)
                        .stdin(false)
                        .stdout(true)
                        .stderr(true),
                )
                .await
                .map_err(|err| ControlError::Sandbox(err.to_string()))?;
            let _ = attached.join().await;
            Ok(())
        }
    }

    fn sandbox_input(envelope: &TurnEnvelope, steer: bool) -> Value {
        json!({
            "type": "user",
            "thread_key": envelope.thread_ref,
            "message": envelope.message,
            "attachments": envelope.attachments,
            "trace_id": envelope.trace_id,
            "traceparent": envelope.traceparent,
            "trace_metadata": envelope.trace_context,
            "steer": steer,
        })
    }

    fn is_terminal_event(raw: &Value) -> bool {
        matches!(
            raw.get("type").and_then(Value::as_str),
            Some("result" | "error" | "turn.completed" | "turn.failed")
        )
    }

    fn is_pod_ready(pod: &Pod) -> bool {
        pod.status
            .as_ref()
            .and_then(|status| status.conditions.as_ref())
            .is_some_and(|conditions| {
                conditions
                    .iter()
                    .any(|condition| condition.type_ == "Ready" && condition.status == "True")
            })
    }

    fn pod_phase(pod: &Pod) -> Option<String> {
        pod.status
            .as_ref()?
            .phase
            .clone()
            .map(|v| v.to_ascii_lowercase())
    }

    fn env_value(name: &str, value: &str) -> Value {
        json!({"name": name, "value": value})
    }

    fn sandbox_labels(sandbox_id: &str, harness: &Harness) -> Value {
        json!({
            "app.kubernetes.io/managed-by": "centaur-control-plane-rs",
            "centaur.ai/managed": "true",
            "centaur.ai/sandbox-id": sandbox_id,
            "centaur.ai/harness": harness.as_str(),
            "centaur.ai/engine": harness.as_str(),
        })
    }

    fn proxy_labels(sandbox_id: &str) -> Value {
        json!({PROXY_LABEL: "true", "centaur.ai/sandbox-id": sandbox_id})
    }

    fn sandbox_selector(sandbox_id: &str) -> Value {
        json!({"centaur.ai/managed": "true", "centaur.ai/sandbox-id": sandbox_id})
    }

    fn proxy_name(sandbox_id: &str) -> String {
        format!("cplane-proxy-{}", &sandbox_id[sandbox_id.len() - 20..])
    }

    fn proxy_pod_name(sandbox_id: &str) -> String {
        format!("{}-pod", proxy_name(sandbox_id))
    }

    fn proxy_config_name(sandbox_id: &str) -> String {
        format!("{}-config", proxy_name(sandbox_id))
    }

    fn sandbox_policy_name(sandbox_id: &str) -> String {
        format!("{}-sandbox-net", proxy_name(sandbox_id))
    }

    fn proxy_policy_name(sandbox_id: &str) -> String {
        format!("{}-proxy-net", proxy_name(sandbox_id))
    }

    fn prompt_secret_name(sandbox_id: &str) -> String {
        format!("{}-prompt", sandbox_id)
    }

    fn harness_cmd(harness: &Harness) -> &'static str {
        match harness {
            Harness::Codex => "codex-app-wrapper",
            Harness::ClaudeCode => "claude-app-wrapper",
            Harness::Amp => "amp-wrapper",
        }
    }

    fn proxy_port() -> u16 {
        env_u16("KUBERNETES_IRON_PROXY_PORT", 8080)
    }

    fn proxy_management_port() -> u16 {
        env_u16("KUBERNETES_IRON_PROXY_MANAGEMENT_PORT", 9092)
    }

    fn proxy_health_port() -> u16 {
        env_u16("KUBERNETES_IRON_PROXY_HEALTH_PORT", 9090)
    }

    fn env_u16(name: &str, default: u16) -> u16 {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    fn sandbox_pull_policy() -> String {
        std::env::var("KUBERNETES_AGENT_IMAGE_PULL_POLICY")
            .unwrap_or_else(|_| "IfNotPresent".into())
    }

    fn proxy_pull_policy() -> String {
        std::env::var("KUBERNETES_IRON_PROXY_IMAGE_PULL_POLICY")
            .unwrap_or_else(|_| "IfNotPresent".into())
    }

    fn state_volume_enabled() -> bool {
        std::env::var("KUBERNETES_SANDBOX_STATE_VOLUME_ENABLED")
            .ok()
            .is_some_and(|v| {
                matches!(
                    v.trim().to_ascii_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            })
    }

    fn state_volume_claim_spec() -> Value {
        let mut spec = json!({
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": std::env::var("KUBERNETES_SANDBOX_STATE_VOLUME_SIZE").unwrap_or_else(|_| "10Gi".into())}}
        });
        if let Ok(class) = std::env::var("KUBERNETES_SANDBOX_STATE_VOLUME_STORAGE_CLASS") {
            if !class.trim().is_empty() {
                spec["storageClassName"] = json!(class);
            }
        }
        spec
    }

    fn image_pull_secrets() -> Option<Value> {
        let raw = std::env::var("KUBERNETES_SANDBOX_IMAGE_PULL_SECRETS").ok()?;
        let items = raw
            .split(',')
            .map(str::trim)
            .filter(|item| !item.is_empty())
            .map(|name| json!({"name": name}))
            .collect::<Vec<_>>();
        (!items.is_empty()).then(|| json!(items))
    }

    fn sandbox_resources() -> Value {
        let mut limits = serde_json::Map::new();
        if let Some(value) =
            env_optional("KUBERNETES_SANDBOX_CPU_LIMIT").or_else(|| Some("2".into()))
        {
            if !value.is_empty() {
                limits.insert("cpu".into(), json!(value));
            }
        }
        if let Some(value) =
            env_optional("KUBERNETES_SANDBOX_MEMORY_LIMIT").or_else(|| Some("4Gi".into()))
        {
            if !value.is_empty() {
                limits.insert("memory".into(), json!(value));
            }
        }
        let mut requests = serde_json::Map::new();
        for (env_name, key) in [
            ("KUBERNETES_SANDBOX_CPU_REQUEST", "cpu"),
            ("KUBERNETES_SANDBOX_MEMORY_REQUEST", "memory"),
        ] {
            if let Some(value) = env_optional(env_name) {
                requests.insert(key.into(), json!(value));
            }
        }
        let mut resources = serde_json::Map::new();
        if !limits.is_empty() {
            resources.insert("limits".into(), Value::Object(limits));
        }
        if !requests.is_empty() {
            resources.insert("requests".into(), Value::Object(requests));
        }
        Value::Object(resources)
    }

    fn env_optional(name: &str) -> Option<String> {
        std::env::var(name)
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
    }

    fn sandbox_extra_env() -> Vec<(String, String)> {
        let Ok(raw) = std::env::var("KUBERNETES_SANDBOX_EXTRA_ENV") else {
            return vec![];
        };
        serde_json::from_str::<Vec<Value>>(&raw)
            .unwrap_or_default()
            .into_iter()
            .filter_map(|item| {
                let name = item.get("name")?.as_str()?.trim().to_string();
                let value = item
                    .get("value")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                (!name.is_empty() && !name.contains('=')).then_some((name, value))
            })
            .collect()
    }

    fn pinned_env(name: &str) -> bool {
        matches!(
            name,
            "HTTP_PROXY"
                | "HTTPS_PROXY"
                | "http_proxy"
                | "https_proxy"
                | "FIREWALL_HOST"
                | "NODE_EXTRA_CA_CERTS"
                | "REQUESTS_CA_BUNDLE"
                | "SSL_CERT_FILE"
                | "GIT_SSL_CAINFO"
        )
    }

    fn host_from_url(url: &str) -> Option<String> {
        let after_scheme = url.split_once("://").map(|(_, rest)| rest).unwrap_or(url);
        after_scheme
            .split('/')
            .next()?
            .split(':')
            .next()
            .map(str::to_string)
    }

    fn prompt_bundle(prompt: &SystemPromptSpec) -> String {
        let mut sections = vec![
            format!(
                "[Active deployment]\n|Persona: {}\n|Overlay loaded: unknown\n",
                prompt
                    .persona_id
                    .as_deref()
                    .filter(|v| !v.trim().is_empty())
                    .unwrap_or("none - base centaur identity")
            ),
            BASE_PROMPT.to_string(),
        ];
        if let Some(override_text) = prompt
            .override_text
            .as_deref()
            .filter(|v| !v.trim().is_empty())
        {
            sections.push(format!("[System prompt override]\n{override_text}"));
        }
        sections.join("\n---\n\n")
    }

    fn render_proxy_yaml(harness: &Harness) -> Result<String> {
        let mut secrets = vec![
            http_secret("AMP_API_KEY", "ampcode.com", "Authorization"),
            http_secret("GITHUB_TOKEN", "github.com", "Authorization"),
            http_secret("GITHUB_TOKEN", "api.github.com", "Authorization"),
        ];
        match harness {
            Harness::Codex => secrets.push(http_secret(
                "OPENAI_API_KEY",
                "api.openai.com",
                "Authorization",
            )),
            Harness::ClaudeCode => secrets.push(http_secret(
                "ANTHROPIC_API_KEY",
                "api.anthropic.com",
                "X-Api-Key",
            )),
            Harness::Amp => {}
        }
        let config = json!({
            "dns": {"listen": ":53", "proxy_ip": "127.0.0.1"},
            "proxy": {"tunnel_listen": format!(":{}", proxy_port())},
            "management": {"listen": format!(":{}", proxy_management_port()), "api_key_env": "IRON_MANAGEMENT_API_KEY"},
            "tls": {"mode": "mitm", "ca_cert": "/etc/iron-proxy/ca.crt", "ca_key": "/etc/iron-proxy/ca.key"},
            "transforms": [
                {"name": "allowlist", "config": {"domains": ["*"]}},
                {"name": "header_allowlist", "config": {"headers": header_allowlist()}},
                {"name": "secrets", "config": {"secrets": secrets}}
            ],
            "log": {"level": "info"}
        });
        serde_yaml::to_string(&config).map_err(|err| ControlError::Sandbox(err.to_string()))
    }

    fn http_secret(name: &str, host: &str, header: &str) -> Value {
        json!({
            "source": secret_source_block(name),
            "replace": {"proxy_value": name, "match_headers": [header]},
            "rules": [{"host": host}]
        })
    }

    fn secret_source_block(name: &str) -> Value {
        match secret_source().as_str() {
            "onepassword" => {
                json!({"type": "1password", "secret_ref": format!("op://{}/{name}/credential", op_vault()), "ttl": secret_ttl()})
            }
            "onepassword-connect" => {
                json!({"type": "1password_connect", "secret_ref": format!("op://{}/{name}/credential", op_vault()), "ttl": secret_ttl()})
            }
            _ => json!({"type": "env", "var": name}),
        }
    }

    fn secret_source() -> String {
        std::env::var("FIREWALL_MANAGER_SECRET_SOURCE")
            .or_else(|_| std::env::var("KUBERNETES_FIREWALL_MANAGER_SECRET_SOURCE"))
            .unwrap_or_else(|_| "env".into())
            .trim()
            .to_ascii_lowercase()
    }

    fn op_vault() -> String {
        std::env::var("OP_VAULT").unwrap_or_else(|_| "ai-agents".into())
    }

    fn secret_ttl() -> String {
        std::env::var("FIREWALL_MANAGER_SECRET_TTL").unwrap_or_else(|_| "10m".into())
    }

    fn header_allowlist() -> Vec<&'static str> {
        vec![
            "host",
            "content-type",
            "content-length",
            "accept",
            "accept-encoding",
            "accept-language",
            "authorization",
            "anthropic-version",
            "anthropic-beta",
            "openai-organization",
            "openai-project",
            "chatgpt-account-id",
            "x-request-id",
            "x-stainless-arch",
            "x-stainless-os",
            "x-stainless-lang",
            "x-stainless-runtime",
            "x-stainless-runtime-version",
            "x-stainless-package-version",
            "x-stainless-retry-count",
            "connection",
            "transfer-encoding",
            "te",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-protocol",
            "sec-websocket-extensions",
            "cache-control",
            "pragma",
            "cookie",
            "api-key",
            "apikey",
            "x-api-key",
            "/^x-codex-.*$/",
            "/^x-openai-.*$/",
            "/^x-[a-z0-9-]*(api-key|apikey|secret|token|auth|key)$/",
        ]
    }

    fn sandbox_network_policy(sandbox_id: &str) -> Value {
        json!({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": sandbox_policy_name(sandbox_id), "labels": {"centaur.ai/sandbox-id": sandbox_id}},
            "spec": {
                "podSelector": {"matchLabels": sandbox_selector(sandbox_id)},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"podSelector": {"matchLabels": api_match_labels()}}],
                        "ports": [{"protocol": "TCP", "port": 8000}]
                    },
                    {
                        "to": [{"podSelector": {"matchLabels": proxy_labels(sandbox_id)}}],
                        "ports": [{"protocol": "TCP", "port": proxy_port()}]
                    }
                ]
            }
        })
    }

    fn proxy_network_policy(sandbox_id: &str) -> Value {
        json!({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": proxy_policy_name(sandbox_id), "labels": {"centaur.ai/sandbox-id": sandbox_id}},
            "spec": {
                "podSelector": {"matchLabels": proxy_labels(sandbox_id)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": sandbox_selector(sandbox_id)}}],
                        "ports": [{"protocol": "TCP", "port": proxy_port()}]
                    }
                ],
                "egress": [
                    {"ports": [{"protocol": "TCP", "port": 443}]},
                    {"ports": [{"protocol": "TCP", "port": 80}]},
                    {"ports": [{"protocol": "TCP", "port": 5432}]},
                    {
                        "to": [{"podSelector": {"matchLabels": api_match_labels()}}],
                        "ports": [{"protocol": "TCP", "port": 8000}]
                    }
                ]
            }
        })
    }

    fn api_match_labels() -> Value {
        let raw = std::env::var("KUBERNETES_API_POD_LABEL_SELECTOR").unwrap_or_default();
        let mut labels = serde_json::Map::new();
        for part in raw.split(',') {
            let Some((key, value)) = part.split_once('=') else {
                continue;
            };
            let key = key.trim();
            let value = value.trim();
            if !key.is_empty() && !value.is_empty() {
                labels.insert(key.into(), json!(value));
            }
        }
        if labels.is_empty() {
            labels.insert("app.kubernetes.io/component".into(), json!("api"));
        }
        Value::Object(labels)
    }
}

#[cfg(test)]
mod tests {
    use super::stable_sandbox_id;

    #[test]
    fn stable_sandbox_ids_are_deterministic_and_namespaced() {
        let one = stable_sandbox_id("thread-a");
        assert_eq!(one, stable_sandbox_id("thread-a"));
        assert_ne!(one, stable_sandbox_id("thread-b"));
        assert!(one.starts_with("cplane-"));
    }
}
