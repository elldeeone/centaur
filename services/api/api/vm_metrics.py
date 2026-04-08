"""Push-based metrics module for VictoriaMetrics.

Lightweight in-memory metric types (Counter, Gauge, Histogram) with a
background asyncio task that periodically pushes to VictoriaMetrics.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import httpx
import structlog
from asyncpg import Pool

log = structlog.get_logger().bind(service="api", component="vm_metrics")

_VM_URL = os.environ.get("VICTORIAMETRICS_URL", "http://victoriametrics:8428")
_PUSH_INTERVAL_S = 15

# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------

_ALL_METRICS: list[_MetricBase] = []


def _escape_label_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _MetricBase:
    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names: list[str] = list(label_names or [])
        self._lock = threading.Lock()
        _ALL_METRICS.append(self)

    def _label_key(self, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(labels.items()))

    def _format_labels(self, labels: dict[str, str]) -> str:
        if not labels:
            return ""
        parts = ",".join(
            f'{k}="{_escape_label_value(v)}"' for k, v in sorted(labels.items())
        )
        return "{" + parts + "}"


class _ChildProxy:
    """Returned by .labels() — holds a reference to the parent and label values."""

    def __init__(self, parent: Any, label_values: dict[str, str]) -> None:
        self._parent = parent
        self._labels = label_values
        self._key = parent._label_key(label_values)


class Counter(_MetricBase):
    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None) -> None:
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def labels(self, **kwargs: str) -> _CounterChild:
        return _CounterChild(self, kwargs)

    def inc(self, amount: float = 1) -> None:
        if self.label_names:
            raise ValueError("Must call .labels() first on a labeled metric")
        key: tuple[tuple[str, str], ...] = ()
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def _inc_keyed(self, key: tuple[tuple[str, str], ...], amount: float) -> None:
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            snapshot = list(self._values.items())
        for key, val in snapshot:
            labels = dict(key)
            lines.append(f"{self.name}{self._format_labels(labels)} {val}")
        return "\n".join(lines)


class _CounterChild:
    def __init__(self, parent: Counter, label_values: dict[str, str]) -> None:
        self._parent = parent
        self._key = parent._label_key(label_values)

    def inc(self, amount: float = 1) -> None:
        self._parent._inc_keyed(self._key, amount)


class Gauge(_MetricBase):
    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None) -> None:
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def labels(self, **kwargs: str) -> _GaugeChild:
        return _GaugeChild(self, kwargs)

    def set(self, value: float) -> None:
        key: tuple[tuple[str, str], ...] = ()
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1) -> None:
        key: tuple[tuple[str, str], ...] = ()
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def dec(self, amount: float = 1) -> None:
        key: tuple[tuple[str, str], ...] = ()
        with self._lock:
            self._values[key] = self._values.get(key, 0) - amount

    def _set_keyed(self, key: tuple[tuple[str, str], ...], value: float) -> None:
        with self._lock:
            self._values[key] = value

    def _inc_keyed(self, key: tuple[tuple[str, str], ...], amount: float) -> None:
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def _dec_keyed(self, key: tuple[tuple[str, str], ...], amount: float) -> None:
        with self._lock:
            self._values[key] = self._values.get(key, 0) - amount

    def clear_children(self) -> None:
        """Remove all labelled child values (used before re-populating from DB)."""
        with self._lock:
            self._values.clear()

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            snapshot = list(self._values.items())
        for key, val in snapshot:
            labels = dict(key)
            lines.append(f"{self.name}{self._format_labels(labels)} {val}")
        return "\n".join(lines)


class _GaugeChild:
    def __init__(self, parent: Gauge, label_values: dict[str, str]) -> None:
        self._parent = parent
        self._key = parent._label_key(label_values)

    def set(self, value: float) -> None:
        self._parent._set_keyed(self._key, value)

    def inc(self, amount: float = 1) -> None:
        self._parent._inc_keyed(self._key, amount)

    def dec(self, amount: float = 1) -> None:
        self._parent._dec_keyed(self._key, amount)


class Histogram(_MetricBase):
    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: list[str] | None = None,
        buckets: list[float] | None = None,
    ) -> None:
        super().__init__(name, help_text, label_names)
        self.buckets = sorted(buckets or _DEFAULT_BUCKETS)
        # keyed data: key -> {"buckets": [...counts...], "sum": float, "count": int}
        self._data: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}

    def _empty_data(self) -> dict[str, Any]:
        return {"buckets": [0] * len(self.buckets), "sum": 0.0, "count": 0}

    def labels(self, **kwargs: str) -> _HistogramChild:
        return _HistogramChild(self, kwargs)

    def observe(self, value: float) -> None:
        if self.label_names:
            raise ValueError("Must call .labels() first on a labeled metric")
        self._observe_keyed((), value)

    def _observe_keyed(self, key: tuple[tuple[str, str], ...], value: float) -> None:
        with self._lock:
            if key not in self._data:
                self._data[key] = self._empty_data()
            d = self._data[key]
            d["sum"] += value
            d["count"] += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    d["buckets"][i] += 1

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with self._lock:
            snapshot = list(self._data.items())
        for key, d in snapshot:
            labels = dict(key)
            lbl_str = self._format_labels(labels)
            cum = 0
            for i, bound in enumerate(self.buckets):
                cum += d["buckets"][i]
                le_labels = dict(labels)
                le_labels["le"] = str(bound)
                lines.append(f"{self.name}_bucket{self._format_labels(le_labels)} {cum}")
            inf_labels = dict(labels)
            inf_labels["le"] = "+Inf"
            lines.append(f"{self.name}_bucket{self._format_labels(inf_labels)} {d['count']}")
            lines.append(f"{self.name}_sum{lbl_str} {d['sum']}")
            lines.append(f"{self.name}_count{lbl_str} {d['count']}")
        return "\n".join(lines)


class _HistogramChild:
    def __init__(self, parent: Histogram, label_values: dict[str, str]) -> None:
        self._parent = parent
        self._key = parent._label_key(label_values)

    def observe(self, value: float) -> None:
        self._parent._observe_keyed(self._key, value)


_DEFAULT_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]

# ---------------------------------------------------------------------------
# Metric instances
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests served by the API.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of in-flight HTTP requests.",
)

AGENT_SESSIONS_ACTIVE = Gauge(
    "agent_sessions_active",
    "Number of running sandbox sessions.",
)
AGENT_EXECUTIONS_TOTAL = Counter(
    "agent_executions_total",
    "Total completed agent executions.",
    ["harness", "status"],
)
AGENT_EXECUTION_DURATION_SECONDS = Histogram(
    "agent_execution_duration_seconds",
    "Agent execution duration in seconds.",
    ["harness", "status"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
)

TOOL_CALLS_TOTAL = Counter(
    "agent_tool_calls_total",
    "Total tool calls by tool name and outcome.",
    ["tool_name", "tool_method", "success"],
)
TOOL_CALL_DURATION_SECONDS = Histogram(
    "agent_tool_call_duration_seconds",
    "Tool call latency in seconds.",
    ["tool_name", "tool_method"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
)

EXECUTIONS_ENQUEUED_TOTAL = Counter(
    "agent_executions_enqueued_total",
    "Total executions enqueued.",
    ["harness"],
)
EXECUTIONS_CLAIMED_TOTAL = Counter(
    "agent_executions_claimed_total",
    "Total executions claimed by a worker.",
    ["harness"],
)
EXECUTION_QUEUE_DELAY_SECONDS = Histogram(
    "agent_execution_queue_delay_seconds",
    "Time from enqueue to claim in seconds.",
    ["harness"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60],
)
EXECUTION_WATCHDOG_TIMEOUTS_TOTAL = Counter(
    "agent_execution_watchdog_timeouts_total",
    "Execution watchdog timeouts.",
    ["harness", "reason"],
)
EXECUTION_REQUESTS_GAUGE = Gauge(
    "agent_execution_requests",
    "Current execution requests by status.",
    ["status"],
)

FINAL_DELIVERY_OUTBOX_GAUGE = Gauge(
    "agent_final_delivery_outbox",
    "Final delivery outbox items by state.",
    ["state"],
)

WARM_POOL_CONTAINERS = Gauge(
    "agent_warm_pool_containers",
    "Warm pool container counts.",
    ["state"],
)
WARM_POOL_CLAIMS_TOTAL = Counter(
    "agent_warm_pool_claims_total",
    "Warm pool claim outcomes.",
    ["outcome"],
)
EXECUTION_TERMINAL_TOTAL = Counter(
    "agent_execution_terminal_total",
    "Terminal execution outcomes by harness and reason.",
    ["harness", "status", "terminal_reason"],
)
MESSAGE_EVENTS_TOTAL = Counter(
    "agent_message_events_total",
    "Stored message events by role and whether attachments were present.",
    ["role", "has_attachments"],
)
MESSAGE_TEXT_CHARS = Histogram(
    "agent_message_text_chars",
    "Character count for stored message text.",
    ["role"],
    buckets=[10, 50, 100, 500, 1000, 5000, 10000, 50000],
)
MESSAGE_ATTACHMENTS_TOTAL = Counter(
    "agent_message_attachments_total",
    "Attachment references stored on messages.",
    ["role"],
)
USAGE_TOKENS_TOTAL = Counter(
    "agent_usage_tokens_total",
    "Observed model token usage by harness, model, and token category.",
    ["harness", "model", "token_type"],
)
USAGE_COST_USD_TOTAL = Counter(
    "agent_usage_cost_usd_total",
    "Observed model cost in USD by harness and model.",
    ["harness", "model"],
)
TTFT_SECONDS = Histogram(
    "agent_ttft_seconds",
    "Time to first token in seconds.",
    ["harness"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)
ONESHOT_TOTAL = Counter(
    "agent_oneshot_total",
    "Execution outcomes for first execution on a thread (1-shot tracking).",
    ["harness", "success"],
)
TOOL_ERROR_CATEGORIES_TOTAL = Counter(
    "agent_tool_error_categories_total",
    "Tool errors by tool name and error category.",
    ["tool_name", "category"],
)
EXECUTION_BY_USER_TOTAL = Counter(
    "agent_execution_by_user_total",
    "Executions by user, harness, and terminal status.",
    ["user_id", "harness", "status"],
)
WORKFLOW_RUNS_TOTAL = Counter(
    "workflow_runs_total",
    "Total workflow runs by name and terminal status.",
    ["workflow_name", "status"],
)
WORKFLOW_RUN_DURATION_SECONDS = Histogram(
    "workflow_run_duration_seconds",
    "Workflow run duration in seconds (queued to completed).",
    ["workflow_name", "status"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
)
WORKFLOW_RUNS_ENQUEUED_TOTAL = Counter(
    "workflow_runs_enqueued_total",
    "Total workflow runs enqueued.",
    ["workflow_name"],
)
WORKFLOW_RUNS_CLAIMED_TOTAL = Counter(
    "workflow_runs_claimed_total",
    "Total workflow runs claimed by a worker.",
    ["workflow_name"],
)
WORKFLOW_RUN_QUEUE_DELAY_SECONDS = Histogram(
    "workflow_run_queue_delay_seconds",
    "Time from enqueue to claim in seconds.",
    ["workflow_name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60],
)
WORKFLOW_EVENTS_TOTAL = Counter(
    "workflow_events_total",
    "Total workflow events sent.",
    ["event_type"],
)
WORKFLOW_RUNS_GAUGE = Gauge(
    "workflow_runs",
    "Current workflow runs by status.",
    ["status"],
)

# ---------------------------------------------------------------------------
# Helper functions (same public interface as the old metrics.py)
# ---------------------------------------------------------------------------


def observe_http_request(method: str, path: str, status: int, duration_s: float) -> None:
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=str(status)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration_s)


def record_agent_execution(harness: str, status: str, duration_s: float) -> None:
    AGENT_EXECUTIONS_TOTAL.labels(harness=harness, status=status).inc()
    AGENT_EXECUTION_DURATION_SECONDS.labels(harness=harness, status=status).observe(duration_s)


def record_execution_terminal(harness: str, status: str, terminal_reason: str) -> None:
    EXECUTION_TERMINAL_TOTAL.labels(
        harness=harness,
        status=status,
        terminal_reason=terminal_reason,
    ).inc()


def record_tool_call(tool_name: str, tool_method: str, success: bool, duration_s: float) -> None:
    TOOL_CALLS_TOTAL.labels(
        tool_name=tool_name, tool_method=tool_method, success=str(success).lower()
    ).inc()
    TOOL_CALL_DURATION_SECONDS.labels(tool_name=tool_name, tool_method=tool_method).observe(
        duration_s
    )


def record_execution_enqueued(harness: str) -> None:
    EXECUTIONS_ENQUEUED_TOTAL.labels(harness=harness).inc()


def record_execution_claimed(harness: str, queue_delay_s: float) -> None:
    EXECUTIONS_CLAIMED_TOTAL.labels(harness=harness).inc()
    EXECUTION_QUEUE_DELAY_SECONDS.labels(harness=harness).observe(queue_delay_s)


def record_execution_watchdog_timeout(harness: str, reason: str) -> None:
    EXECUTION_WATCHDOG_TIMEOUTS_TOTAL.labels(harness=harness, reason=reason).inc()


def record_warm_pool_claim(outcome: str) -> None:
    WARM_POOL_CLAIMS_TOTAL.labels(outcome=outcome).inc()


def record_message_observation(role: str, text_chars: int, attachment_count: int) -> None:
    has_attachments = "true" if attachment_count > 0 else "false"
    MESSAGE_EVENTS_TOTAL.labels(role=role, has_attachments=has_attachments).inc()
    MESSAGE_TEXT_CHARS.labels(role=role).observe(max(text_chars, 0))
    if attachment_count > 0:
        MESSAGE_ATTACHMENTS_TOTAL.labels(role=role).inc(attachment_count)


def record_ttft(harness: str, ttft_s: float) -> None:
    TTFT_SECONDS.labels(harness=harness).observe(ttft_s)


def record_oneshot(harness: str, success: bool) -> None:
    ONESHOT_TOTAL.labels(harness=harness, success=str(success).lower()).inc()


def record_tool_error_category(tool_name: str, category: str) -> None:
    TOOL_ERROR_CATEGORIES_TOTAL.labels(tool_name=tool_name, category=category).inc()


def record_execution_by_user(user_id: str, harness: str, status: str) -> None:
    EXECUTION_BY_USER_TOTAL.labels(user_id=user_id, harness=harness, status=status).inc()


def record_workflow_run_terminal(workflow_name: str, status: str, duration_s: float) -> None:
    WORKFLOW_RUNS_TOTAL.labels(workflow_name=workflow_name, status=status).inc()
    WORKFLOW_RUN_DURATION_SECONDS.labels(workflow_name=workflow_name, status=status).observe(
        duration_s
    )


def record_workflow_run_enqueued(workflow_name: str) -> None:
    WORKFLOW_RUNS_ENQUEUED_TOTAL.labels(workflow_name=workflow_name).inc()


def record_workflow_run_claimed(workflow_name: str, queue_delay_s: float) -> None:
    WORKFLOW_RUNS_CLAIMED_TOTAL.labels(workflow_name=workflow_name).inc()
    WORKFLOW_RUN_QUEUE_DELAY_SECONDS.labels(workflow_name=workflow_name).observe(queue_delay_s)


def record_workflow_event_sent(event_type: str) -> None:
    WORKFLOW_EVENTS_TOTAL.labels(event_type=event_type).inc()


def record_usage_observation(
    harness: str,
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    resolved_model = model or "unknown"
    token_values = {
        "input_tokens": max(input_tokens, 0),
        "output_tokens": max(output_tokens, 0),
        "cache_creation_input_tokens": max(cache_creation_input_tokens, 0),
        "cache_read_input_tokens": max(cache_read_input_tokens, 0),
    }
    for token_type, value in token_values.items():
        if value > 0:
            USAGE_TOKENS_TOTAL.labels(
                harness=harness,
                model=resolved_model,
                token_type=token_type,
            ).inc(value)
    if cost_usd > 0:
        USAGE_COST_USD_TOTAL.labels(harness=harness, model=resolved_model).inc(cost_usd)


# ---------------------------------------------------------------------------
# DB-driven gauge refresh
# ---------------------------------------------------------------------------


async def refresh_runtime_metrics(pool: Pool) -> None:
    active_sessions = await pool.fetchval(
        "SELECT COUNT(*) FROM sandbox_sessions WHERE state = 'running'"
    )
    AGENT_SESSIONS_ACTIVE.set(int(active_sessions or 0))

    EXECUTION_REQUESTS_GAUGE.clear_children()
    rows = await pool.fetch(
        "SELECT status, COUNT(*) AS cnt FROM agent_execution_requests "
        "WHERE status IN ('queued', 'running', 'retry_wait', 'cancel_requested') "
        "GROUP BY status"
    )
    for row in rows:
        EXECUTION_REQUESTS_GAUGE.labels(status=row["status"]).set(row["cnt"])

    FINAL_DELIVERY_OUTBOX_GAUGE.clear_children()
    rows = await pool.fetch(
        "SELECT state, COUNT(*) AS cnt FROM agent_final_delivery_outbox "
        "WHERE state NOT IN ('delivered') "
        "GROUP BY state"
    )
    for row in rows:
        FINAL_DELIVERY_OUTBOX_GAUGE.labels(state=row["state"]).set(row["cnt"])

    WORKFLOW_RUNS_GAUGE.clear_children()
    rows = await pool.fetch(
        "SELECT status, COUNT(*) AS cnt FROM workflow_runs "
        "WHERE status IN ('queued', 'running', 'sleeping', 'waiting') "
        "GROUP BY status"
    )
    for row in rows:
        WORKFLOW_RUNS_GAUGE.labels(status=row["status"]).set(row["cnt"])


# ---------------------------------------------------------------------------
# Render (text exposition format for VictoriaMetrics import)
# ---------------------------------------------------------------------------


async def render_metrics(pool: Pool) -> bytes:
    await refresh_runtime_metrics(pool)
    parts = [m.render() for m in _ALL_METRICS if m.render()]
    return ("\n".join(parts) + "\n").encode("utf-8")


def _render_metrics_sync() -> str:
    parts = [m.render() for m in _ALL_METRICS if m.render()]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Push loop
# ---------------------------------------------------------------------------

_push_task: asyncio.Task[None] | None = None
_push_pool: Pool | None = None


async def _push_loop() -> None:
    url = f"{_VM_URL}/api/v1/import/prometheus"
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await asyncio.sleep(_PUSH_INTERVAL_S)
            try:
                if _push_pool is not None:
                    await refresh_runtime_metrics(_push_pool)
                payload = _render_metrics_sync()
                if payload.strip():
                    resp = await client.post(
                        url,
                        content=payload,
                        headers={"Content-Type": "text/plain"},
                    )
                    if resp.status_code >= 400:
                        log.warning(
                            "vm_push_failed",
                            status=resp.status_code,
                            body=resp.text[:200],
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("vm_push_error", exc_info=True)


def start_push_loop(pool: Pool) -> None:
    global _push_task, _push_pool
    _push_pool = pool
    _push_task = asyncio.create_task(_push_loop())
    log.info("vm_push_loop_started", url=_VM_URL, interval_s=_PUSH_INTERVAL_S)


async def stop_push_loop() -> None:
    global _push_task, _push_pool
    if _push_task is not None:
        _push_task.cancel()
        try:
            await _push_task
        except asyncio.CancelledError:
            pass
        _push_task = None
    _push_pool = None
    log.info("vm_push_loop_stopped")
