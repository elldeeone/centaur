"""Workflow: autonomous nightly self-improvement loop.

Internal modes:
- parent: the scheduled nightly review and fix-selection pass
- fix_child: one focused child run for one selected fix
"""

from __future__ import annotations

import datetime as dt
import json
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any

from api.runtime_control import ControlPlaneError, decode_jsonb
from api.workflow_engine import Delivery, WorkflowContext
from workflows.json_payloads import extract_json_payload, has_required_keys, missing_required_keys

WORKFLOW_NAME = "self_improve_daily"
SCHEDULE = {
    "cron": "0 22 * * *",
    "timezone": "America/Los_Angeles",
    "slack_channel": "ai-v2",
    "catchup_policy": "skip",
}

PRIOR_CONTEXT_WINDOW = 3
FOLLOWUP_LIMIT = 8
FOLLOWUP_CUTOFF_HOURS = 4
NOTIFIER_STATS_WINDOW_HOURS = 24
CHILD_TIMEOUT_HOURS = 2
DEDUP_WINDOW_HOURS = 72
MAX_DELIVERY_TEXT_CHARS = 2000
PR_METADATA_START = "<!-- self_improve_metadata_v1:start -->"
PR_METADATA_END = "<!-- self_improve_metadata_v1:end -->"

TRIAGE_PREFERRED_KEYS = ("selected_task_ids", "task_assessments")
TRIAGE_REQUIRED_KEYS = ("selected_task_ids",)
RECONCILE_PREFERRED_KEYS = ("reconciled_fixes",)
RECONCILE_REQUIRED_KEYS = ("reconciled_fixes",)


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(value, 1)


REVIEW_WINDOW_HOURS_DEFAULT = _env_positive_int("SELF_IMPROVE_REVIEW_WINDOW_HOURS", 24)
MAX_SELECTED_FIXES_DEFAULT = _env_positive_int("SELF_IMPROVE_MAX_SELECTED_FIXES", 1)
CANDIDATE_LIMIT_DEFAULT = _env_positive_int("SELF_IMPROVE_CANDIDATE_LIMIT", 5)
CANDIDATE_FETCH_FACTOR = _env_positive_int("SELF_IMPROVE_CANDIDATE_FETCH_FACTOR", 4)
REVIEW_PREFERRED_KEYS = (
    "tasks_reviewed",
    "below_bar_count",
    "below_bar_rate",
    "task_reviews",
    "top_failure_modes",
    "selected_fixes",
)
REVIEW_REQUIRED_KEYS = ("task_reviews", "selected_fixes")
SYNTHESIS_PREFERRED_KEYS = (
    "sessions_analyzed",
    "opportunities_found",
    "opportunities",
    "selected_builds",
)
SYNTHESIS_REQUIRED_KEYS = ("opportunities", "selected_builds")
OPEN_PR_PREFERRED_KEYS = (
    "branch",
    "commit",
    "pr_number",
    "pr_url",
    "pr_title",
    "verified_handoff",
)
OPEN_PR_REQUIRED_KEYS = ("branch", "pr_number", "pr_url")
RESEARCH_PREFERRED_KEYS = (
    "root_cause",
    "fix_type",
    "affected_files",
    "acceptance_criteria",
    "verification_plan",
    "risks",
    "confidence",
    "new_capability_justification",
)
RESEARCH_REQUIRED_KEYS = (
    "root_cause",
    "fix_type",
    "affected_files",
    "acceptance_criteria",
    "verification_plan",
)
PLAN_PREFERRED_KEYS = ("files", "plan", "validation", "pr_title", "expected_impact")
PLAN_REQUIRED_KEYS = ("files", "plan", "validation", "pr_title")
IMPLEMENT_PREFERRED_KEYS = ("changed_files", "summary")
IMPLEMENT_REQUIRED_KEYS = ("changed_files", "summary")
VALIDATE_PREFERRED_KEYS = ("checks", "summary", "regression_check")
VALIDATE_REQUIRED_KEYS = ("checks", "summary")


@dataclass
class Input:
    mode: str = "parent"
    review_window_hours: int = REVIEW_WINDOW_HOURS_DEFAULT
    max_selected_fixes: int = MAX_SELECTED_FIXES_DEFAULT
    candidate_limit: int = CANDIDATE_LIMIT_DEFAULT
    fix_packet: dict[str, Any] = field(default_factory=dict)


def _message_text(parts: list[dict[str, Any]]) -> str:
    texts = [
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text).strip()


def _message_part_types(parts: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(part.get("type") or "").strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("type") or "").strip()
        }
    )


def _extract_required_json_payload(
    text: str,
    *,
    stage: str,
    preferred_keys: tuple[str, ...] = (),
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload = extract_json_payload(text, preferred_keys=preferred_keys)
    if required_keys and not has_required_keys(payload, required_keys):
        missing = ", ".join(missing_required_keys(payload, required_keys))
        payload_keys = ", ".join(sorted(payload.keys()))
        snippet = str(payload.get("raw_snippet") or "")[:160]
        raise RuntimeError(
            f"{stage} response missing required keys [{missing}] "
            f"(payload keys: [{payload_keys}]; snippet: {snippet})"
        )
    return payload


def _parse_thread_key(thread_key: str) -> tuple[str, str]:
    parts = thread_key.strip().split(":")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], parts[2]
    raise ValueError(f"invalid thread key: {thread_key}")


def _slack_ts_to_datetime(value: str) -> dt.datetime | None:
    seconds, _, micros = value.partition(".")
    if not seconds:
        return None
    try:
        base = int(seconds)
        frac = float(f"0.{micros}") if micros else 0.0
    except ValueError:
        return None
    return dt.datetime.fromtimestamp(base + frac, tz=dt.timezone.utc)


def _normalize_message(row: dict[str, Any]) -> dict[str, Any]:
    parts = decode_jsonb(row.get("parts"), [])
    metadata = decode_jsonb(row.get("metadata"), {})
    part_list = parts if isinstance(parts, list) else []
    return {
        "id": str(row.get("id") or ""),
        "role": str(row.get("role") or "user"),
        "parts": part_list,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": row.get("created_at"),
        "text": _message_text(part_list),
        "part_types": _message_part_types(part_list),
    }


def _serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        created_at = message.get("created_at")
        payload.append(
            {
                "message_id": message.get("metadata", {}).get("message_id"),
                "role": message.get("role"),
                "created_at": created_at.isoformat()
                if isinstance(created_at, dt.datetime)
                else None,
                "text": message.get("text", ""),
                "part_types": list(message.get("part_types") or []),
            }
        )
    return payload


def _summarize_followups(messages: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(message.get("text") or "").strip() for message in messages]
    return {
        "followup_count": len(messages),
        "has_followup": bool(messages),
        "example_texts": [text for text in texts if text][:5],
    }


def _sum_int_values(mapping: Any) -> int:
    if not isinstance(mapping, dict):
        return 0
    total = 0
    for value in mapping.values():
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def _sum_mapping_values(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        mapping = item.get(key)
        if not isinstance(mapping, dict):
            continue
        for map_key, value in mapping.items():
            try:
                totals[str(map_key)] = totals.get(str(map_key), 0) + int(value or 0)
            except (TypeError, ValueError):
                continue
    return totals


def _compact_task_summary(task: dict[str, Any]) -> dict[str, Any]:
    followup_summary = task.get("user_followup_summary") or {}
    return {
        "task_id": task.get("task_id"),
        "thread_key": task.get("thread_key"),
        "ask_text": str(task.get("ask_text") or "")[:500],
        "status": task.get("status"),
        "terminal_reason": task.get("terminal_reason"),
        "duration_s": task.get("duration_s"),
        "tool_errors": _sum_int_values(task.get("tool_errors_by_name")),
        "tool_retry_count": task.get("tool_retry_count", 0),
        "subagent_failures": task.get("subagent_failures", 0),
        "command_error_events": task.get("command_error_events", 0),
        "followup_count": followup_summary.get("followup_count", 0),
        "followup_texts": followup_summary.get("example_texts", []),
        "delivery_snippet": str(task.get("final_delivery_text") or "")[:300],
    }


async def _run_triage_pass(
    ctx: WorkflowContext,
    *,
    tasks: list[dict[str, Any]],
    limit: int,
) -> list[str]:
    if not tasks:
        return []
    if len(tasks) <= limit:
        return [str(task.get("task_id") or "") for task in tasks]

    summaries = [_compact_task_summary(task) for task in tasks]
    prompt = textwrap.dedent(
        f"""
        You are triaging Slack-thread user tasks for a nightly self-improvement review.

        Below are {len(summaries)} reconstructed tasks from the past day. Select the
        {limit} most valuable tasks to send to the full quality-review pass.

        Selection criteria (use your judgment, not rigid rules):
        - Prioritize tasks where the user likely had a bad experience: failed
          executions, negative or corrective follow-ups, timeouts, silence.
        - Include a mix of failure modes so the review covers diverse issues.
        - Include at least 1-2 tasks that completed successfully so the reviewer
          can calibrate what "good" looks like in this batch.
        - Interpret follow-up messages semantically. "thanks" after a good answer
          is positive. "can you try again" after a failure is negative. Do not
          rely on keyword matching — read the actual conversation snippets.
        - Consider execution telemetry (errors, retries, duration) as supporting
          evidence, not as the sole selection criterion.

        Return JSON only with exactly these top-level keys:
        - `selected_task_ids`: array of {limit} task_id strings, ordered by
          review priority (most important first).
        - `task_assessments`: array of objects, one per input task, each with
          `task_id`, `review_priority` ("high"/"medium"/"low"),
          `followup_quality` ("positive"/"neutral"/"negative"/"none"),
          and a short `rationale`.

        Task summaries:
        ```json
        {json.dumps(summaries, indent=2)}
        ```
        """
    ).strip()

    triage_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:triage",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "triage",
        },
    )

    async def _parse_triage() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(triage_turn.get("result_text") or ""),
            stage="triage",
            preferred_keys=TRIAGE_PREFERRED_KEYS,
            required_keys=TRIAGE_REQUIRED_KEYS,
        )

    triage_result = await ctx.step("triage_tasks", _parse_triage, step_kind="review")
    selected_ids = list(triage_result.get("selected_task_ids") or [])
    ctx.log(
        "self_improve_triage_completed",
        candidate_count=len(tasks),
        selected_count=len(selected_ids),
    )
    return [str(tid) for tid in selected_ids[:limit]]


def _looks_insufficient(task: dict[str, Any]) -> bool:
    return not str(task.get("ask_text") or "").strip()


def _normalize_source_threads(items: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_key = str(item.get("thread_key") or "").strip()
        channel = str(item.get("channel") or "").strip()
        thread_ts = str(item.get("thread_ts") or "").strip()
        if not channel and not thread_ts and thread_key:
            try:
                channel, thread_ts = _parse_thread_key(thread_key)
            except ValueError:
                continue
        if not thread_key and channel and thread_ts:
            thread_key = f"{channel}:{thread_ts}"
        if thread_key and channel and thread_ts:
            normalized.append(
                {
                    "thread_key": thread_key,
                    "channel": channel,
                    "thread_ts": thread_ts,
                }
            )
    return normalized


def _empty_review(tasks_reviewed: int) -> dict[str, Any]:
    return {
        "tasks_reviewed": tasks_reviewed,
        "below_bar_count": 0,
        "below_bar_rate": 0.0,
        "task_reviews": [],
        "top_failure_modes": [],
        "selected_fixes": [],
    }


def _normalize_review(review: dict[str, Any], *, tasks_reviewed: int) -> dict[str, Any]:
    normalized = _empty_review(tasks_reviewed)
    if not isinstance(review, dict):
        return normalized
    normalized.update(review)
    normalized["tasks_reviewed"] = int(review.get("tasks_reviewed") or tasks_reviewed)
    normalized["below_bar_count"] = int(review.get("below_bar_count") or 0)
    try:
        normalized["below_bar_rate"] = float(review.get("below_bar_rate") or 0.0)
    except (TypeError, ValueError):
        normalized["below_bar_rate"] = 0.0
    normalized["task_reviews"] = list(review.get("task_reviews") or [])
    normalized["top_failure_modes"] = list(review.get("top_failure_modes") or [])
    fixes = []
    for item in list(review.get("selected_fixes") or []):
        if not isinstance(item, dict):
            continue
        fix = dict(item)
        fix["source_threads"] = _normalize_source_threads(fix.get("source_threads"))
        fixes.append(fix)
    normalized["selected_fixes"] = fixes
    return normalized


def _reconstruct_task_from_thread(
    *,
    run: dict[str, Any],
    thread_messages: list[dict[str, Any]],
    source_created_at: dt.datetime,
    next_anchor_at: dt.datetime | None,
) -> dict[str, Any]:
    source_message_id = str(run.get("source_message_id") or "")
    source_message: dict[str, Any] | None = None
    for message in thread_messages:
        if message.get("metadata", {}).get("message_id") == source_message_id:
            source_message = message
            break

    ask_text = (
        source_message.get("text", "") if source_message else str(run.get("ask_text") or "")
    ).strip()
    prior_messages = [
        message
        for message in thread_messages
        if isinstance(message.get("created_at"), dt.datetime)
        and message["created_at"] < source_created_at
    ]
    prior_context = prior_messages[-PRIOR_CONTEXT_WINDOW:]

    cutoff_at = source_created_at + dt.timedelta(hours=FOLLOWUP_CUTOFF_HOURS)
    if next_anchor_at is not None:
        cutoff_at = min(cutoff_at, next_anchor_at)

    followups = [
        message
        for message in thread_messages
        if isinstance(message.get("created_at"), dt.datetime)
        and source_created_at < message["created_at"] < cutoff_at
        and message.get("role") == "user"
    ][:FOLLOWUP_LIMIT]

    channel, thread_ts = _parse_thread_key(str(run.get("thread_key") or ""))
    task_id = f"task:{channel}:{thread_ts}:{source_message_id or run.get('run_id') or 'unknown'}"
    return {
        "task_id": task_id,
        "thread_key": str(run.get("thread_key") or ""),
        "channel": channel,
        "thread_ts": thread_ts,
        "source_message_id": source_message_id,
        "source_created_at": source_created_at.isoformat(),
        "ask_text": ask_text,
        "prior_context": _serialize_messages(prior_context),
        "followups": _serialize_messages(followups),
        "workflow_run_id": str(run.get("run_id") or ""),
    }


async def _fetch_thread_messages(ctx: WorkflowContext, thread_key: str) -> list[dict[str, Any]]:
    rows = await ctx._pool.fetch(
        "SELECT id, role, parts, metadata, created_at "
        "FROM chat_messages WHERE thread_key = $1 ORDER BY created_at ASC",
        thread_key,
    )
    return [_normalize_message(dict(row)) for row in rows]


async def _fetch_live_thread_messages(
    ctx: WorkflowContext,
    *,
    thread_key: str,
) -> list[dict[str, Any]]:
    channel, thread_ts = _parse_thread_key(thread_key)

    async def _fetch() -> list[dict[str, Any]]:
        from api.app import get_tool_manager

        tm = get_tool_manager()
        raw = await tm.call_tool(
            "slack",
            "get_thread_replies",
            {"channel_id": channel, "thread_ts": thread_ts, "limit": 50},
        )
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    step_name = f"live_thread_{channel}_{thread_ts.replace('.', '_')}"
    replies = await ctx.step(step_name, _fetch, step_kind="tool_call")
    messages: list[dict[str, Any]] = []
    for entry in replies:
        if not isinstance(entry, dict):
            continue
        created_at = _slack_ts_to_datetime(str(entry.get("ts") or entry.get("thread_ts") or ""))
        if created_at is None:
            continue
        role = "assistant"
        if not entry.get("bot_id") and entry.get("subtype") != "bot_message":
            role = "user"
        text = str(entry.get("text") or "").strip()
        messages.append(
            {
                "id": str(entry.get("ts") or ""),
                "role": role,
                "parts": [{"type": "text", "text": text}] if text else [],
                "metadata": {"message_id": f"slack:{entry.get('ts') or ''}"},
                "created_at": created_at,
                "text": text,
                "part_types": ["text"] if text else [],
            }
        )
    return messages


async def _fetch_execution_details(
    ctx: WorkflowContext,
    *,
    run_id: str,
) -> dict[str, Any]:
    execution_id = await ctx._pool.fetchval(
        "SELECT execution_id FROM workflow_checkpoints "
        "WHERE run_id = $1 AND execution_id IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        run_id,
    )
    if not execution_id:
        return {}

    execution_row = await ctx._pool.fetchrow(
        "SELECT execution_id, status, terminal_reason, result_text, error_text "
        "FROM agent_execution_requests WHERE execution_id = $1",
        execution_id,
    )
    summary_row = await ctx._pool.fetchrow(
        "SELECT event_json FROM agent_execution_events "
        "WHERE execution_id = $1 AND event_kind = 'execution_summary' "
        "ORDER BY event_id DESC LIMIT 1",
        execution_id,
    )

    payload: dict[str, Any] = {"execution_id": str(execution_id)}
    if execution_row:
        execution = dict(execution_row)
        payload.update(
            {
                "status": str(execution.get("status") or ""),
                "terminal_reason": str(execution.get("terminal_reason") or ""),
                "final_delivery_text": str(
                    execution.get("result_text") or execution.get("error_text") or ""
                ).strip(),
            }
        )

    if summary_row:
        summary = decode_jsonb(dict(summary_row).get("event_json"), {})
        if isinstance(summary, dict):
            payload.update(
                {
                    "duration_s": summary.get("duration_s"),
                    "ttft_ms": summary.get("ttft_ms"),
                    "tool_calls_by_name": summary.get("tool_calls_by_name", {}),
                    "tool_errors_by_name": summary.get("tool_errors_by_name", {}),
                    "tool_error_categories": summary.get("tool_error_categories", {}),
                    "tool_retry_count": summary.get("tool_retry_count", 0),
                    "subagent_events": summary.get("subagent_events", 0),
                    "subagent_failures": summary.get("subagent_failures", 0),
                    "command_error_events": summary.get("command_error_events", 0),
                    "file_change_events": summary.get("file_change_events", 0),
                    "total_tokens": summary.get("total_tokens", 0),
                    "cost_usd": summary.get("cost_usd"),
                    "models": summary.get("models", []),
                    "persona_id": summary.get("persona_id", ""),
                    "prompt_ref": summary.get("prompt_ref", ""),
                    "execution_sequence": summary.get("execution_sequence", 0),
                    "assistant_text_chars": summary.get("assistant_text_chars", 0),
                    "reasoning_events": summary.get("reasoning_events", 0),
                }
            )

    return payload


async def _aggregate_execution_details(
    ctx: WorkflowContext,
    *,
    run_ids: list[str],
) -> dict[str, Any]:
    details = [await _fetch_execution_details(ctx, run_id=run_id) for run_id in run_ids]
    present = [detail for detail in details if detail]
    latest = present[-1] if present else {}
    return {
        "workflow_run_ids": list(run_ids),
        "execution_ids": [
            str(detail.get("execution_id"))
            for detail in present
            if str(detail.get("execution_id") or "").strip()
        ],
        "status": str(latest.get("status") or ""),
        "terminal_reason": str(latest.get("terminal_reason") or ""),
        "final_delivery_text": str(latest.get("final_delivery_text") or ""),
        "tool_calls_by_name": _sum_mapping_values(present, "tool_calls_by_name"),
        "tool_errors_by_name": _sum_mapping_values(present, "tool_errors_by_name"),
        "tool_error_categories": _sum_mapping_values(present, "tool_error_categories"),
        "tool_retry_count": sum(int(detail.get("tool_retry_count") or 0) for detail in present),
        "subagent_events": sum(int(detail.get("subagent_events") or 0) for detail in present),
        "subagent_failures": sum(int(detail.get("subagent_failures") or 0) for detail in present),
        "command_error_events": sum(
            int(detail.get("command_error_events") or 0) for detail in present
        ),
        "file_change_events": sum(int(detail.get("file_change_events") or 0) for detail in present),
        "duration_s": latest.get("duration_s"),
        "ttft_ms": latest.get("ttft_ms"),
        "total_tokens": sum(int(detail.get("total_tokens") or 0) for detail in present),
        "cost_usd": sum(float(detail.get("cost_usd") or 0.0) for detail in present),
        "models": sorted(
            {m for detail in present for m in (detail.get("models") or []) if isinstance(m, str)}
        ),
        "persona_id": str(latest.get("persona_id") or ""),
        "prompt_ref": str(latest.get("prompt_ref") or ""),
        "execution_sequence": max(
            (int(detail.get("execution_sequence") or 0) for detail in present), default=0
        ),
        "assistant_text_chars": sum(
            int(detail.get("assistant_text_chars") or 0) for detail in present
        ),
        "reasoning_events": sum(int(detail.get("reasoning_events") or 0) for detail in present),
    }


async def _collect_evidence_packs(
    ctx: WorkflowContext,
    *,
    review_window_hours: int,
    candidate_limit: int,
) -> list[dict[str, Any]]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=review_window_hours)
    fetch_limit = max(candidate_limit * CANDIDATE_FETCH_FACTOR, candidate_limit)
    rows = await ctx._pool.fetch(
        "SELECT run_id, thread_key, input_json, created_at "
        "FROM workflow_runs "
        "WHERE workflow_name = 'slack_thread_turn' "
        "  AND created_at >= $1 "
        "  AND status IN ('completed', 'failed', 'cancelled') "
        "ORDER BY created_at DESC LIMIT $2",
        since,
        fetch_limit,
    )

    candidate_runs: list[dict[str, Any]] = []
    for row in rows:
        row_data = dict(row)
        input_json = decode_jsonb(row_data.get("input_json"), {})
        parts = input_json.get("parts") if isinstance(input_json, dict) else []
        ask_text = _message_text(parts if isinstance(parts, list) else [])
        thread_key = str(row_data.get("thread_key") or "")
        if not thread_key:
            continue
        candidate_runs.append(
            {
                "run_id": str(row_data.get("run_id") or ""),
                "thread_key": thread_key,
                "created_at": row_data.get("created_at"),
                "source_message_id": input_json.get("message_id") if isinstance(input_json, dict) else "",
                "ask_text": ask_text,
            }
        )

    runs_by_thread: dict[str, list[dict[str, Any]]] = {}
    for run in candidate_runs:
        runs_by_thread.setdefault(run["thread_key"], []).append(run)

    tasks: list[dict[str, Any]] = []
    for thread_key, thread_runs in runs_by_thread.items():
        messages = await _fetch_thread_messages(ctx, thread_key)
        clusters: dict[str, dict[str, Any]] = {}
        for run in thread_runs:
            source_time = run.get("created_at")
            if not isinstance(source_time, dt.datetime):
                continue
            source_message_id = str(run.get("source_message_id") or "")
            for message in messages:
                if message.get("metadata", {}).get("message_id") == source_message_id:
                    source_time = message.get("created_at")
                    break
            if not isinstance(source_time, dt.datetime):
                continue
            cluster_key = source_message_id or str(run.get("run_id") or "")
            cluster = clusters.setdefault(
                cluster_key,
                {
                    "thread_key": thread_key,
                    "source_message_id": source_message_id,
                    "ask_text": str(run.get("ask_text") or "").strip(),
                    "run_ids": [],
                    "source_created_at": source_time,
                },
            )
            cluster["run_ids"].append(str(run.get("run_id") or ""))
            if not cluster.get("ask_text") and str(run.get("ask_text") or "").strip():
                cluster["ask_text"] = str(run.get("ask_text") or "").strip()
            if source_time < cluster["source_created_at"]:
                cluster["source_created_at"] = source_time

        task_anchors = sorted(
            list(clusters.values()),
            key=lambda item: item["source_created_at"],
        )
        for index, anchor in enumerate(task_anchors):
            source_time = anchor["source_created_at"]
            next_anchor_at = (
                task_anchors[index + 1]["source_created_at"]
                if index + 1 < len(task_anchors)
                else None
            )
            task = _reconstruct_task_from_thread(
                run={
                    "run_id": str(anchor["run_ids"][0] or ""),
                    "thread_key": thread_key,
                    "source_message_id": anchor["source_message_id"],
                    "ask_text": anchor["ask_text"],
                },
                thread_messages=messages,
                source_created_at=source_time,
                next_anchor_at=next_anchor_at,
            )
            if _looks_insufficient(task):
                live_messages = await _fetch_live_thread_messages(ctx, thread_key=thread_key)
                if live_messages:
                    task = _reconstruct_task_from_thread(
                        run={
                            "run_id": str(anchor["run_ids"][0] or ""),
                            "thread_key": thread_key,
                            "source_message_id": anchor["source_message_id"],
                            "ask_text": anchor["ask_text"],
                        },
                        thread_messages=live_messages,
                        source_created_at=source_time,
                        next_anchor_at=next_anchor_at,
                    )

            execution = await _aggregate_execution_details(ctx, run_ids=list(anchor["run_ids"]))
            followup_summary = _summarize_followups(task["followups"])
            evidence = {
                "task_id": task["task_id"],
                "thread_key": task["thread_key"],
                "channel": task["channel"],
                "thread_ts": task["thread_ts"],
                "source_message_id": task["source_message_id"],
                "source_created_at": task["source_created_at"],
                "ask_text": task["ask_text"],
                "prior_context": task["prior_context"],
                "followups": task["followups"],
                "workflow_run_ids": execution.get("workflow_run_ids", [task["workflow_run_id"]]),
                "execution_ids": execution.get("execution_ids", []),
                "final_delivery_text": str(
                    execution.get("final_delivery_text") or ""
                )[:MAX_DELIVERY_TEXT_CHARS],
                "status": execution.get("status", ""),
                "terminal_reason": execution.get("terminal_reason", ""),
                "tool_calls_by_name": execution.get("tool_calls_by_name", {}),
                "tool_errors_by_name": execution.get("tool_errors_by_name", {}),
                "tool_error_categories": execution.get("tool_error_categories", {}),
                "tool_retry_count": execution.get("tool_retry_count", 0),
                "subagent_events": execution.get("subagent_events", 0),
                "subagent_failures": execution.get("subagent_failures", 0),
                "command_error_events": execution.get("command_error_events", 0),
                "file_change_events": execution.get("file_change_events", 0),
                "duration_s": execution.get("duration_s"),
                "ttft_ms": execution.get("ttft_ms"),
                "total_tokens": execution.get("total_tokens", 0),
                "cost_usd": execution.get("cost_usd"),
                "models": execution.get("models", []),
                "persona_id": execution.get("persona_id", ""),
                "prompt_ref": execution.get("prompt_ref", ""),
                "execution_sequence": execution.get("execution_sequence", 0),
                "assistant_text_chars": execution.get("assistant_text_chars", 0),
                "reasoning_events": execution.get("reasoning_events", 0),
                "user_followup_summary": followup_summary,
            }
            tasks.append(evidence)
            ctx.log(
                "self_improve_task_reconstructed",
                task_id=evidence["task_id"],
                thread_key=evidence["thread_key"],
            )

    ctx.log(
        "self_improve_tasks_reconstructed",
        task_count=len(tasks),
    )
    return tasks


async def _run_batch_review_pass(
    ctx: WorkflowContext,
    *,
    evidence_packs: list[dict[str, Any]],
    max_selected_fixes: int,
    recent_fix_titles: list[str] | None = None,
) -> dict[str, Any]:
    if not evidence_packs:
        return _empty_review(0)

    recent_titles_block = ""
    if recent_fix_titles:
        recent_titles_block = textwrap.dedent(
            f"""
            Recently attempted fix titles (skip or justify re-attempting):
            ```json
            {json.dumps(recent_fix_titles, indent=2)}
            ```
            """
        ).strip()

    prompt = textwrap.dedent(
        f"""
        Load the `gap-analysis` skill first. Then read `references/rubric.md`.

        Review this batch of reconstructed Slack-thread user tasks.
        For each task, follow the evaluation method exactly:
        1. Restate the user's task in one sentence.
        2. Quote the key evidence from the ask, delivery, or follow-ups.
        3. Answer the binary sub-questions for each of the seven dimensions.
        4. Write a one-sentence reasoning trace per dimension.
        5. Assign the numeric score (0-4) per dimension.
        6. Compute the composite score.
        7. Classify as above-bar or below-bar.

        CRITICAL — use exactly these seven dimension keys in `scores` and `reasoning`:
        `completion`, `correctness`, `research_quality`, `verification_quality`,
        `tool_calling_quality`, `subagent_usage_quality`, `communication_quality`.
        Do NOT use alternate names like task_understanding, efficiency, user_satisfaction,
        instruction_following, or any other synonym. Use exactly the keys above.

        Interpret follow-up messages semantically. Read the actual text of each
        follow-up and determine whether it indicates satisfaction, correction,
        a new request, or frustration. Do not rely on keyword matching.

        Some tasks may be genuinely good. If a task was completed correctly with no
        negative follow-up signals, grade it above-bar. Do not manufacture problems
        where none exist. For conversational brainstorming or ideation tasks where
        verification is not applicable, score verification_quality as 4.

        After grading all tasks, cluster failures and select fixes.
        Keep clustering simple: dominant failure mode + likely fix surface.
        Prioritize user-value failures before style or polish.
        Respect the maximum selected-fix count: {max_selected_fixes}.

        Return JSON only. Use EXACTLY these top-level keys:
        `tasks_reviewed`, `below_bar_count`, `below_bar_rate`, `task_reviews`,
        `top_failure_modes`, `selected_fixes`.

        Each `selected_fixes` entry MUST include:
        `title`, `fix_type`, `target_surface`, `what_to_change`,
        `dominant_failure_mode`, `priority`, `why_now`, `evidence_quotes`,
        `source_threads`, `representative_tasks`.
        The `target_surface` must name a real file in the Centaur codebase.
        Vague recommendations are not acceptable.

        Progress reporting: Generating the final review JSON can be long.
        Between evaluating each task (before you start writing the final JSON
        answer), emit a tiny `shell_command` call like `echo "reviewing N/M"`.
        These shell calls reset the silence watchdog to 30 minutes and are
        ignored by the parser. Do this after every 1-2 tasks you evaluate.
        Then write the final JSON answer as your last message.

        {recent_titles_block}

        Evidence pack batch:
        ```json
        {json.dumps({"max_selected_fixes": max_selected_fixes, "tasks": evidence_packs}, indent=2)}
        ```
        """
    ).strip()

    review_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:gap-analysis",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "batch_review",
        },
    )

    async def _parse_review() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(review_turn.get("result_text") or ""),
            stage="batch_review",
            preferred_keys=REVIEW_PREFERRED_KEYS,
            required_keys=REVIEW_REQUIRED_KEYS,
        )

    review = await ctx.step("batch_review", _parse_review, step_kind="review")
    return _normalize_review(review, tasks_reviewed=len(evidence_packs))


async def _run_learning_synthesis_pass(
    ctx: WorkflowContext,
    *,
    evidence_packs: list[dict[str, Any]],
    max_selected_builds: int,
) -> dict[str, Any]:
    if not evidence_packs:
        return {"sessions_analyzed": 0, "opportunities_found": 0, "opportunities": [], "selected_builds": []}

    prompt = textwrap.dedent(
        f"""
        Load the `learning-synthesis` skill first.

        Analyze this batch of recent Slack-thread user sessions.
        Look for opportunities to improve the system — not quality bugs (those are
        handled separately by gap-analysis), but learnings:
        - Recurring demand patterns that should become new skills
        - Domains or stances that should become new personas
        - Knowledge the bot had to be taught that should be baked in
        - Tool capabilities users need but don't have
        - Manual workflows that should be automated
        - System prompt gaps that cause recurring confusion

        Focus on patterns across 2+ sessions, not one-off requests.
        Every opportunity must name a specific target_surface (file path) and a
        concrete implementation_sketch.
        Select up to {max_selected_builds} opportunities for autonomous implementation.
        Return JSON only matching the output contract in the skill.

        Evidence pack batch:
        ```json
        {json.dumps({"max_selected_builds": max_selected_builds, "tasks": evidence_packs}, indent=2)}
        ```
        """
    ).strip()

    synthesis_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:learning-synthesis",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "learning_synthesis",
        },
    )

    async def _parse_synthesis() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(synthesis_turn.get("result_text") or ""),
            stage="learning_synthesis",
            preferred_keys=SYNTHESIS_PREFERRED_KEYS,
            required_keys=SYNTHESIS_REQUIRED_KEYS,
        )

    synthesis = await ctx.step("learning_synthesis", _parse_synthesis, step_kind="review")
    if not isinstance(synthesis, dict):
        synthesis = {}
    synthesis.setdefault("sessions_analyzed", len(evidence_packs))
    synthesis.setdefault("opportunities_found", len(list(synthesis.get("opportunities") or [])))
    synthesis.setdefault("opportunities", [])
    synthesis.setdefault("selected_builds", [])
    for build in list(synthesis.get("selected_builds") or []):
        if isinstance(build, dict):
            build["source_threads"] = _normalize_source_threads(
                [{"thread_key": tk} for tk in list(build.get("evidence_threads") or [])]
            )
    return synthesis


def _mean_composite(review: dict[str, Any]) -> float:
    task_reviews = list(review.get("task_reviews") or [])
    if not task_reviews:
        return 0.0
    scores = []
    for task in task_reviews:
        if not isinstance(task, dict):
            continue
        composite = task.get("composite_score")
        if composite is not None:
            try:
                scores.append(float(composite))
            except (TypeError, ValueError):
                continue
    return round(sum(scores) / len(scores), 1) if scores else 0.0


def _build_scorecard_markdown(
    *,
    review: dict[str, Any],
    synthesis: dict[str, Any],
    child_results: list[dict[str, Any]],
    notifier_stats: dict[str, int],
) -> str:
    composite = _mean_composite(review)
    top_failure_modes = ", ".join(
        f"{entry.get('failure_mode', 'unknown')} x{entry.get('count', 0)}"
        for entry in list(review.get("top_failure_modes") or [])[:3]
        if isinstance(entry, dict)
    ) or "none"
    selected_fixes = "\n".join(
        f"- `{item.get('fix_type', 'unknown')}` {item.get('title', 'Untitled fix')}"
        for item in list(review.get("selected_fixes") or [])
        if isinstance(item, dict)
    ) or "- none selected"
    opportunities = list(synthesis.get("opportunities") or [])
    selected_builds = list(synthesis.get("selected_builds") or [])
    opportunity_lines = "\n".join(
        f"- `{item.get('opportunity_type', 'unknown')}` {item.get('title', 'Untitled')}"
        for item in opportunities[:5]
        if isinstance(item, dict)
    ) or "- none found"
    build_lines = "\n".join(
        f"- `{item.get('opportunity_type', 'unknown')}` {item.get('title', 'Untitled')}"
        for item in selected_builds
        if isinstance(item, dict)
    ) or "- none selected"
    opened_prs = "\n".join(
        f"- [#{item['pr_number']}]({item['pr_url']}) {item.get('title', '').strip()}"
        for item in child_results
        if item.get("pr_number") and item.get("pr_url")
    ) or "- none opened"
    failed_children = "\n".join(
        f"- {item.get('title') or item.get('child_run_id') or 'unknown child'}: {item.get('error')}"
        for item in child_results
        if item.get("error")
    ) or "- none"
    return textwrap.dedent(
        f"""
        Self Improve Nightly

        Reviewed {review.get('tasks_reviewed', 0)} tasks. Mean score: {composite:.0f}/100. Below-bar rate: {review.get('below_bar_rate', 0.0):.0%}.

        *Gap Analysis*
        - Top failure modes: {top_failure_modes}
        - Selected fixes:
        {selected_fixes}

        *Learning Synthesis*
        - Opportunities found: {len(opportunities)}
        {opportunity_lines}
        - Selected builds:
        {build_lines}

        *Execution*
        - PRs opened:
        {opened_prs}
        - Child workflow errors:
        {failed_children}
        - PRs merged in last 24h: {notifier_stats.get('merged_prs', 0)}
        - PRs deployed in last 24h: {notifier_stats.get('deployed_prs', 0)}
        - Source threads notified in last 24h: {notifier_stats.get('source_threads_notified', 0)}
        """
    ).strip()


def _make_pr_metadata_block(ctx: WorkflowContext, fix_packet: dict[str, Any]) -> str:
    payload = {
        "parent_run_id": fix_packet.get("parent_run_id"),
        "child_run_id": ctx.run_id,
        "fix_type": fix_packet.get("fix_type"),
        "source_threads": _normalize_source_threads(fix_packet.get("source_threads")),
        "summary": fix_packet.get("title") or fix_packet.get("why_now") or "Self-improvement fix",
    }
    return "\n".join(
        [
            PR_METADATA_START,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            PR_METADATA_END,
        ]
    )


async def _start_fix_children(
    ctx: WorkflowContext,
    *,
    selected_fixes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for index, fix in enumerate(selected_fixes, start=1):
        packet = dict(fix)
        packet["parent_run_id"] = ctx.run_id
        packet["source_threads"] = _normalize_source_threads(packet.get("source_threads"))
        started = await ctx.start_workflow(
            f"selected_fix_{index}",
            workflow_name=WORKFLOW_NAME,
            run_input={
                "mode": "fix_child",
                "fix_packet": packet,
            },
            trigger_key=f"self-improve-fix:{ctx.run_id}:{index}",
            eager_start=True,
        )
        children.append(started)
        ctx.log(
            "self_improve_fix_child_started",
            child_run_id=started.get("run_id"),
            fix_type=packet.get("fix_type"),
            title=packet.get("title"),
        )
    return children


async def _wait_for_fix_children(
    ctx: WorkflowContext,
    children: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, child in enumerate(children, start=1):
        run_id = str(child.get("run_id") or "")
        completed = await ctx.wait_for_workflow(
            f"selected_fix_{index}.result",
            run_id=run_id,
            timeout=dt.timedelta(hours=CHILD_TIMEOUT_HOURS),
        )
        output_json = completed.get("output_json") if isinstance(completed, dict) else {}
        if isinstance(output_json, str):
            try:
                output_json = json.loads(output_json)
            except (json.JSONDecodeError, TypeError):
                output_json = {"error": "malformed child output", "raw": output_json[:500]}
        if isinstance(output_json, dict):
            results.append(output_json)
        else:
            results.append({
                "child_run_id": run_id,
                "error": "child output was not a JSON object",
            })
        ctx.log(
            "self_improve_fix_child_completed",
            child_run_id=run_id,
            status=completed.get("status") if isinstance(completed, dict) else None,
        )
    return results


async def _load_recent_fix_titles(ctx: WorkflowContext) -> list[str]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=DEDUP_WINDOW_HOURS)
    rows = await ctx._pool.fetch(
        "SELECT output_json FROM workflow_runs "
        "WHERE workflow_name = $1 AND status = 'completed' "
        "  AND created_at >= $2 AND input_json->>'mode' = 'fix_child'",
        WORKFLOW_NAME,
        since,
    )
    titles: set[str] = set()
    for row in rows:
        output = decode_jsonb(dict(row).get("output_json"), {})
        if isinstance(output, dict):
            title = str(output.get("title") or "").strip().lower()
            if title:
                titles.add(title)
    return sorted(titles)


async def _run_reconcile_fixes_pass(
    ctx: WorkflowContext,
    *,
    gap_fixes: list[dict[str, Any]],
    build_fixes: list[dict[str, Any]],
    recent_titles: list[str],
    max_fixes: int,
) -> list[dict[str, Any]]:
    all_candidates = gap_fixes + build_fixes
    if not all_candidates:
        return []
    if len(all_candidates) == 1 and not recent_titles:
        return all_candidates

    prompt = textwrap.dedent(
        f"""
        You are reconciling proposed self-improvement fixes from two independent
        analysis passes (gap-analysis and learning-synthesis) before spawning
        child workflows to implement them.

        Your job:
        1. Merge semantically duplicate fixes. Two fixes that target the same
           file for the same root cause are duplicates even if worded differently.
        2. Drop any fix that substantially overlaps with a recently attempted fix
           (see recent_titles below). Only keep it if you can articulate why the
           prior attempt was insufficient.
        3. Rank the surviving fixes by expected user-value impact.
        4. Return at most {max_fixes} fixes.

        Each fix in the output must include ALL of these fields:
        `title`, `fix_type`, `target_surface`, `what_to_change`,
        `dominant_failure_mode`, `priority`, `why_now`, `evidence_quotes`,
        `source_threads`, `representative_tasks`.

        If a field was present in the input fix, preserve it. If you merge two
        fixes, combine their evidence and source threads.

        Return JSON only with exactly this top-level key:
        - `reconciled_fixes`: array of fix objects (at most {max_fixes}).

        Recently attempted fix titles (skip these unless clearly insufficient):
        ```json
        {json.dumps(recent_titles, indent=2)}
        ```

        Candidate fixes from gap-analysis:
        ```json
        {json.dumps(gap_fixes, indent=2)}
        ```

        Candidate fixes from learning-synthesis:
        ```json
        {json.dumps(build_fixes, indent=2)}
        ```
        """
    ).strip()

    reconcile_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:reconcile-fixes",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "reconcile_fixes",
        },
    )

    async def _parse_reconcile() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(reconcile_turn.get("result_text") or ""),
            stage="reconcile_fixes",
            preferred_keys=RECONCILE_PREFERRED_KEYS,
            required_keys=RECONCILE_REQUIRED_KEYS,
        )

    reconciled = await ctx.step("reconcile_fixes", _parse_reconcile, step_kind="review")
    fixes = list(reconciled.get("reconciled_fixes") or [])
    for fix in fixes:
        if isinstance(fix, dict):
            fix["source_threads"] = _normalize_source_threads(fix.get("source_threads"))
    ctx.log(
        "self_improve_reconcile_completed",
        input_count=len(all_candidates),
        output_count=len(fixes),
        recent_titles_count=len(recent_titles),
    )
    return [fix for fix in fixes if isinstance(fix, dict)][:max_fixes]


async def _load_recent_notifier_stats(ctx: WorkflowContext) -> dict[str, int]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=NOTIFIER_STATS_WINDOW_HOURS)
    rows = await ctx._pool.fetch(
        "SELECT output_json FROM workflow_runs "
        "WHERE workflow_name = $1 AND status = 'completed' AND created_at >= $2",
        "self_improve_deploy_notifier",
        since,
    )
    merged_prs = 0
    deployed_prs = 0
    source_threads_notified = 0
    for row in rows:
        output_json = decode_jsonb(dict(row).get("output_json"), {})
        if not isinstance(output_json, dict):
            continue
        merged_prs += int(output_json.get("merged_prs", 0) or 0)
        deployed_prs += int(output_json.get("deployed_prs", 0) or 0)
        source_threads_notified += int(output_json.get("source_threads_notified", 0) or 0)
    return {
        "merged_prs": merged_prs,
        "deployed_prs": deployed_prs,
        "source_threads_notified": source_threads_notified,
    }


async def _run_parent(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    async def _collect() -> list[dict[str, Any]]:
        return await _collect_evidence_packs(
            ctx,
            review_window_hours=max(inp.review_window_hours, 1),
            candidate_limit=max(inp.candidate_limit, 1),
        )

    all_tasks = await ctx.step("collect_tasks", _collect, step_kind="gather")
    ctx.log("self_improve_batch_collected", tasks_total=len(all_tasks))

    selected_ids = await _run_triage_pass(
        ctx,
        tasks=all_tasks,
        limit=max(inp.candidate_limit, 1),
    )
    id_set = set(selected_ids)
    evidence_packs = [
        task for task in all_tasks if str(task.get("task_id") or "") in id_set
    ]
    if not evidence_packs:
        evidence_packs = all_tasks[:max(inp.candidate_limit, 1)]
    ctx.log(
        "self_improve_triage_applied",
        total_tasks=len(all_tasks),
        selected_tasks=len(evidence_packs),
    )

    review = await _run_batch_review_pass(
        ctx,
        evidence_packs=evidence_packs,
        max_selected_fixes=max(inp.max_selected_fixes, 1),
    )

    synthesis = await _run_learning_synthesis_pass(
        ctx,
        evidence_packs=evidence_packs,
        max_selected_builds=max(inp.max_selected_fixes, 1),
    )
    ctx.log(
        "self_improve_learning_synthesis",
        opportunities_found=synthesis.get("opportunities_found", 0),
        selected_builds=len(list(synthesis.get("selected_builds") or [])),
    )

    async def _load_dedup_titles() -> list[str]:
        return await _load_recent_fix_titles(ctx)

    recent_titles = list(
        await ctx.step("load_recent_fix_titles", _load_dedup_titles, step_kind="gather")
    )

    gap_fixes = list(review.get("selected_fixes") or [])
    build_fixes = []
    for build in list(synthesis.get("selected_builds") or []):
        if not isinstance(build, dict):
            continue
        build_fixes.append({
            "title": build.get("title", ""),
            "fix_type": build.get("opportunity_type", "new_skill"),
            "target_surface": build.get("target_surface", ""),
            "what_to_change": build.get("implementation_sketch", ""),
            "dominant_failure_mode": f"learning: {build.get('opportunity_type', 'unknown')}",
            "priority": "medium",
            "why_now": build.get("user_value", ""),
            "evidence_quotes": [build.get("evidence_summary", "")],
            "source_threads": build.get("source_threads", []),
            "representative_tasks": [],
            "new_capability_justification": build.get("what_should_exist", ""),
        })

    selected_fixes = await _run_reconcile_fixes_pass(
        ctx,
        gap_fixes=gap_fixes,
        build_fixes=build_fixes,
        recent_titles=recent_titles,
        max_fixes=max(inp.max_selected_fixes, 1),
    )

    children = await _start_fix_children(ctx, selected_fixes=selected_fixes)
    child_results = await _wait_for_fix_children(ctx, children)

    async def _load_stats() -> dict[str, int]:
        return await _load_recent_notifier_stats(ctx)

    notifier_stats = await ctx.step("load_notifier_stats", _load_stats, step_kind="gather")
    scorecard = _build_scorecard_markdown(
        review=review,
        synthesis=synthesis,
        child_results=child_results,
        notifier_stats=notifier_stats,
    )
    await ctx.post_to_slack("ai-v2", scorecard)
    ctx.log(
        "self_improve_scorecard_posted",
        tasks_reviewed=review.get("tasks_reviewed", 0),
        below_bar_count=review.get("below_bar_count", 0),
        selected_fix_count=len(selected_fixes),
        opportunities_found=synthesis.get("opportunities_found", 0),
    )
    return {
        "mode": "parent",
        "review": review,
        "synthesis": synthesis,
        "selected_fixes": selected_fixes,
        "opened_prs": child_results,
        "merged_prs": notifier_stats.get("merged_prs", 0),
        "deployed_prs": notifier_stats.get("deployed_prs", 0),
        "source_threads_notified": notifier_stats.get("source_threads_notified", 0),
        "scorecard": scorecard,
    }


async def _run_phase(
    ctx: WorkflowContext,
    *,
    phase_name: str,
    agent_thread_key: str,
    prompt: str,
    preferred_keys: tuple[str, ...] = (),
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    async def _phase() -> dict[str, Any]:
        result = await ctx.agent_turn(
            prompt,
            thread_key=agent_thread_key,
            delivery=Delivery.dev(),
            prompt_selector="eng",
            metadata={
                "source": WORKFLOW_NAME,
                "mode": "fix_child",
                "phase": phase_name,
            },
        )
        return _extract_required_json_payload(
            str(result.get("result_text") or ""),
            stage=phase_name,
            preferred_keys=preferred_keys,
            required_keys=required_keys,
        )

    return await ctx.step(phase_name, _phase, step_kind="phase")


async def _run_fix_child(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    fix_packet = dict(inp.fix_packet or {})
    if not fix_packet:
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            "fix_child mode requires fix_packet",
            422,
        )

    fix_packet["source_threads"] = _normalize_source_threads(fix_packet.get("source_threads"))
    agent_thread_key = f"workflow:{ctx.run_id}:fix"
    metadata_block = _make_pr_metadata_block(ctx, fix_packet)

    research_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        This is the research phase for one selected self-improvement fix.
        Keep scope narrow.
        If the fix type is `new_skill` or `new_persona`, include an explicit
        justification for why this is a missing-capability problem instead of a
        code, workflow, prompt, or tool fix.
        Use `git-branch paradigmxyz/centaur` before editing because the mounted
        repo is read-only.
        Return JSON only.

        Fix packet:
        ```json
        {json.dumps(fix_packet, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()
    research = await _run_phase(
        ctx,
        phase_name="research",
        agent_thread_key=agent_thread_key,
        prompt=research_prompt,
        preferred_keys=RESEARCH_PREFERRED_KEYS,
        required_keys=RESEARCH_REQUIRED_KEYS,
    )
    ctx.log("self_improve_fix_phase", phase="research", fix_type=fix_packet.get("fix_type"))

    plan_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        This is the plan phase for the same selected self-improvement fix.
        Keep the plan tightly scoped to one focused PR.
        Return JSON only.

        Fix packet:
        ```json
        {json.dumps(fix_packet, indent=2, ensure_ascii=False)}
        ```

        Research output:
        ```json
        {json.dumps(research, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()
    plan = await _run_phase(
        ctx,
        phase_name="plan",
        agent_thread_key=agent_thread_key,
        prompt=plan_prompt,
        preferred_keys=PLAN_PREFERRED_KEYS,
        required_keys=PLAN_REQUIRED_KEYS,
    )
    ctx.log("self_improve_fix_phase", phase="plan", fix_type=fix_packet.get("fix_type"))

    implement_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        This is the implement phase.
        Apply the planned change in the writable clone and keep the change tightly focused.
        Return JSON only.

        Fix packet:
        ```json
        {json.dumps(fix_packet, indent=2, ensure_ascii=False)}
        ```

        Research output:
        ```json
        {json.dumps(research, indent=2, ensure_ascii=False)}
        ```

        Plan output:
        ```json
        {json.dumps(plan, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()
    implementation = await _run_phase(
        ctx,
        phase_name="implement",
        agent_thread_key=agent_thread_key,
        prompt=implement_prompt,
        preferred_keys=IMPLEMENT_PREFERRED_KEYS,
        required_keys=IMPLEMENT_REQUIRED_KEYS,
    )
    ctx.log("self_improve_fix_phase", phase="implement", fix_type=fix_packet.get("fix_type"))

    validate_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        This is the validate phase.
        Run the smallest relevant checks for this focused change.
        Return JSON only.

        Plan output:
        ```json
        {json.dumps(plan, indent=2, ensure_ascii=False)}
        ```

        Implement output:
        ```json
        {json.dumps(implementation, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()
    validation = await _run_phase(
        ctx,
        phase_name="validate",
        agent_thread_key=agent_thread_key,
        prompt=validate_prompt,
        preferred_keys=VALIDATE_PREFERRED_KEYS,
        required_keys=VALIDATE_REQUIRED_KEYS,
    )
    ctx.log("self_improve_fix_phase", phase="validate", fix_type=fix_packet.get("fix_type"))

    open_pr_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        This is the open PR phase.
        Commit the focused change, push the branch, and open one focused PR.
        The PR must include labels `self-improve` and `fix-type:{fix_packet.get('fix_type', 'unknown')}`.
        The PR body must include this hidden metadata block exactly:

        {metadata_block}

        After opening the PR, verify with `gh pr view` that the labels and metadata block are
        present. Fix the PR if verification fails.

        Return JSON only with:
        - branch
        - commit
        - pr_number
        - pr_url
        - pr_title
        - verified_handoff

        Plan output:
        ```json
        {json.dumps(plan, indent=2, ensure_ascii=False)}
        ```

        Validate output:
        ```json
        {json.dumps(validation, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()
    pr = await _run_phase(
        ctx,
        phase_name="open_pr",
        agent_thread_key=agent_thread_key,
        prompt=open_pr_prompt,
        preferred_keys=OPEN_PR_PREFERRED_KEYS,
        required_keys=OPEN_PR_REQUIRED_KEYS,
    )
    ctx.log(
        "self_improve_fix_phase",
        phase="open_pr",
        fix_type=fix_packet.get("fix_type"),
        pr_number=pr.get("pr_number"),
    )

    return {
        "mode": "fix_child",
        "title": fix_packet.get("title"),
        "fix_type": fix_packet.get("fix_type"),
        "source_threads": fix_packet.get("source_threads", []),
        "research": research,
        "plan": plan,
        "implementation": implementation,
        "validation": validation,
        "pr_number": pr.get("pr_number"),
        "pr_url": pr.get("pr_url"),
        "branch": pr.get("branch"),
        "title_draft": pr.get("pr_title") or plan.get("pr_title"),
        "verified_handoff": bool(pr.get("verified_handoff", False)),
    }


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    if inp.mode == "parent":
        return await _run_parent(inp, ctx)
    if inp.mode == "fix_child":
        return await _run_fix_child(inp, ctx)
    raise ControlPlaneError(
        "INVALID_WORKFLOW_INPUT",
        f"unsupported mode: {inp.mode}",
        422,
    )
