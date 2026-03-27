from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.sandbox.base import SandboxSession


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _insert_assignment(db_pool, thread_key: str, generation: int = 1) -> None:
    await db_pool.execute(
        "INSERT INTO agent_runtime_assignments ("
        "thread_key, assignment_generation, runtime_id, harness, engine, "
        "persona_id, prompt_ref, effective_agents_md_sha256, state"
        ") VALUES ($1, $2, $3, 'amp', 'amp', NULL, 'harness:amp', 'sha', 'active')",
        thread_key,
        generation,
        f"rt-{thread_key}-{generation}",
    )


@pytest.mark.asyncio
async def test_message_requires_active_assignment(client, api_key: str):
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    res = await client.post(
        "/agent/message",
        headers=_auth(api_key),
        json={
            "thread_key": thread_key,
            "assignment_generation": 1,
            "message_id": "msg-1",
            "event": {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
            },
        },
    )

    assert res.status_code == 409
    assert res.json()["code"] == "NO_ACTIVE_ASSIGNMENT"


@pytest.mark.asyncio
async def test_message_stores_attachment_refs(client, db_pool, api_key: str):
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    await _insert_assignment(db_pool, thread_key, generation=3)

    payload = {
        "thread_key": thread_key,
        "assignment_generation": 3,
        "message_id": "msg-attachment",
        "event": {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what do you see?"},
                    {
                        "type": "image",
                        "source_path": "file:///tmp/example.jpg",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(b"hello-image").decode("utf-8"),
                        },
                    },
                ],
            },
        },
    }

    res = await client.post("/agent/message", headers=_auth(api_key), json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert len(data["attachment_ids"]) == 1

    row = await db_pool.fetchrow(
        "SELECT event_json FROM agent_message_requests WHERE thread_key = $1 AND message_id = $2",
        thread_key,
        "msg-attachment",
    )
    assert row is not None
    event_json = row["event_json"]
    if isinstance(event_json, str):
        event_json = json.loads(event_json)

    content = event_json["message"]["content"]
    assert content[1]["type"] == "attachment_ref"
    assert content[1]["attachment_id"] == data["attachment_ids"][0]


@pytest.mark.asyncio
async def test_execute_rejects_stale_generation(client, db_pool, api_key: str):
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    await _insert_assignment(db_pool, thread_key, generation=5)

    res = await client.post(
        "/agent/execute",
        headers=_auth(api_key),
        json={
            "thread_key": thread_key,
            "assignment_generation": 4,
            "execute_id": "exec-stale",
            "delivery": {"platform": "slack"},
        },
    )

    assert res.status_code == 409
    assert res.json()["code"] == "ASSIGNMENT_GENERATION_STALE"


@pytest.mark.asyncio
async def test_execute_enqueues_and_creates_outbox(client, db_pool, api_key: str):
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    await _insert_assignment(db_pool, thread_key, generation=1)

    res = await client.post(
        "/agent/execute",
        headers=_auth(api_key),
        json={
            "thread_key": thread_key,
            "assignment_generation": 1,
            "execute_id": "exec-1",
            "delivery": {"platform": "slack", "channel": "C-test"},
        },
    )

    assert res.status_code == 202
    body = res.json()
    execution_id = body["execution_id"]
    assert body["status"] == "queued"

    execution_row = await db_pool.fetchrow(
        "SELECT status FROM agent_execution_requests WHERE execution_id = $1",
        execution_id,
    )
    assert execution_row is not None
    assert execution_row["status"] == "queued"

    outbox_row = await db_pool.fetchrow(
        "SELECT state FROM agent_final_delivery_outbox WHERE execution_id = $1",
        execution_id,
    )
    assert outbox_row is not None
    assert outbox_row["state"] == "awaiting_terminal"


@pytest.mark.asyncio
async def test_final_delivery_claim_and_mark_delivered(client, db_pool, api_key: str):
    execution_id = f"exe-{uuid.uuid4().hex[:10]}"
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox ("
        "execution_id, thread_key, delivery, state, final_payload, next_attempt_at"
        ") VALUES ($1, $2, $3::jsonb, 'pending', $4::jsonb, NOW())",
        execution_id,
        thread_key,
        json.dumps({"platform": "slack", "channel": "C-test"}),
        json.dumps({"type": "final", "result_text": "done"}),
    )

    claim = await client.post(
        "/agent/final-deliveries/claim",
        headers=_auth(api_key),
        json={"consumer_id": "slackbot:test", "limit": 1, "lease_seconds": 30},
    )
    assert claim.status_code == 200
    deliveries = claim.json()["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["execution_id"] == execution_id
    assert deliveries[0]["attempt_count"] == 1

    delivered = await client.post(
        f"/agent/final-deliveries/{execution_id}/delivered",
        headers=_auth(api_key),
        json={"consumer_id": "slackbot:test"},
    )
    assert delivered.status_code == 200

    row = await db_pool.fetchrow(
        "SELECT state, lease_owner FROM agent_final_delivery_outbox WHERE execution_id = $1",
        execution_id,
    )
    assert row is not None
    assert row["state"] == "delivered"
    assert row["lease_owner"] is None


@pytest.mark.asyncio
async def test_final_delivery_claim_filters_platform(client, db_pool, api_key: str):
    slack_execution_id = f"exe-{uuid.uuid4().hex[:10]}"
    web_execution_id = f"exe-{uuid.uuid4().hex[:10]}"
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"

    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox ("
        "execution_id, thread_key, delivery, state, final_payload, next_attempt_at"
        ") VALUES ($1, $2, $3::jsonb, 'pending', $4::jsonb, NOW())",
        slack_execution_id,
        thread_key,
        json.dumps({"platform": "slack", "channel": "C-test"}),
        json.dumps({"type": "final", "result_text": "done"}),
    )
    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox ("
        "execution_id, thread_key, delivery, state, final_payload, next_attempt_at"
        ") VALUES ($1, $2, $3::jsonb, 'pending', $4::jsonb, NOW())",
        web_execution_id,
        thread_key,
        json.dumps({"platform": "web"}),
        json.dumps({"type": "final", "result_text": "done"}),
    )

    claim = await client.post(
        "/agent/final-deliveries/claim",
        headers=_auth(api_key),
        json={
            "consumer_id": "slackbot:test",
            "limit": 10,
            "lease_seconds": 30,
            "platform": "slack",
        },
    )
    assert claim.status_code == 200
    deliveries = claim.json()["deliveries"]
    assert [delivery["execution_id"] for delivery in deliveries] == [slack_execution_id]


@pytest.mark.asyncio
async def test_spawn_without_explicit_prompt_reuses_active_assignment_and_refreshes_runtime(
    client,
    db_pool,
    api_key: str,
):
    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    prior_runtime_id = f"rt-old-{uuid.uuid4().hex[:8]}"
    resumed_runtime_id = f"rt-new-{uuid.uuid4().hex[:8]}"
    persona_sha = hashlib.sha256("persona:legal".encode("utf-8")).hexdigest()
    await db_pool.execute(
        "INSERT INTO agent_runtime_assignments ("
        "thread_key, assignment_generation, runtime_id, harness, engine, "
        "persona_id, prompt_ref, effective_agents_md_sha256, state"
        ") VALUES ($1, 3, $2, 'legal', 'amp', 'legal', 'persona:legal', $3, 'active')",
        thread_key,
        prior_runtime_id,
        persona_sha,
    )

    resumed = SandboxSession(
        sandbox_id=resumed_runtime_id,
        thread_key=thread_key,
        harness="legal",
        engine="amp",
    )

    with patch("api.runtime_control.get_or_spawn", new=AsyncMock(return_value=resumed)):
        res = await client.post(
            "/agent/spawn",
            headers=_auth(api_key),
            json={
                "thread_key": thread_key,
                "spawn_id": "spawn-adopt-active",
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["assignment_generation"] == 3
    assert body["persona_id"] == "legal"
    assert body["runtime_id"] == resumed_runtime_id

    assignment = await db_pool.fetchrow(
        "SELECT runtime_id FROM agent_runtime_assignments "
        "WHERE thread_key = $1 AND assignment_generation = 3",
        thread_key,
    )
    assert assignment is not None
    assert assignment["runtime_id"] == resumed_runtime_id


@pytest.mark.asyncio
async def test_worker_marks_turn_done_error_as_failed_and_updates_runtime(db_pool):
    from api.runtime_control import _process_execution

    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    execution_id = f"exe-{uuid.uuid4().hex[:12]}"
    prior_runtime_id = f"rt-old-{uuid.uuid4().hex[:8]}"
    resumed_runtime_id = f"rt-new-{uuid.uuid4().hex[:8]}"

    await db_pool.execute(
        "INSERT INTO agent_runtime_assignments ("
        "thread_key, assignment_generation, runtime_id, harness, engine, "
        "persona_id, prompt_ref, effective_agents_md_sha256, state"
        ") VALUES ($1, 1, $2, 'amp', 'amp', NULL, 'harness:amp', 'sha', 'active')",
        thread_key,
        prior_runtime_id,
    )
    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, hard_deadline_at"
        ") VALUES ($1, $2, 1, 'exec-idem', 'hash', 'running', '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '10 minutes')",
        execution_id,
        thread_key,
    )
    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox (execution_id, thread_key, delivery, state) "
        "VALUES ($1, $2, '{}'::jsonb, 'awaiting_terminal')",
        execution_id,
        thread_key,
    )

    row = {
        "execution_id": execution_id,
        "thread_key": thread_key,
        "assignment_generation": 1,
        "delivery": {},
        "hard_deadline_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
    }

    session = SandboxSession(
        sandbox_id=resumed_runtime_id,
        thread_key=thread_key,
        harness="amp",
        engine="amp",
    )

    async def _fake_stream(*_args, **_kwargs):
        yield {
            "data": json.dumps(
                {
                    "type": "turn.done",
                    "result": "Timed out while reconnecting. Please retry after reconnecting.",
                    "is_error": True,
                    "error": "Timed out while reconnecting. Please retry after reconnecting.",
                }
            )
        }

    with (
        patch("api.runtime_control.get_or_spawn", new=AsyncMock(return_value=session)),
        patch(
            "api.runtime_control.inject_stdin",
            new=AsyncMock(return_value={"ok": True, "injected": True, "durable_turn_id": "turn-1"}),
        ),
        patch("api.runtime_control.get_backend", return_value=object()),
        patch(
            "api.runtime_control._get_runtime",
            return_value=SimpleNamespace(turn_counter=1),
        ),
        patch("api.runtime_control._stream_stdout", _fake_stream),
    ):
        await _process_execution(db_pool, row)

    execution = await db_pool.fetchrow(
        "SELECT status, terminal_reason, error_text FROM agent_execution_requests WHERE execution_id = $1",
        execution_id,
    )
    assert execution is not None
    assert execution["status"] == "failed_permanent"
    assert execution["terminal_reason"] == "amp_reconnect_timeout"
    assert "Timed out while reconnecting" in (execution["error_text"] or "")

    assignment = await db_pool.fetchrow(
        "SELECT runtime_id FROM agent_runtime_assignments WHERE thread_key = $1 AND assignment_generation = 1",
        thread_key,
    )
    assert assignment is not None
    assert assignment["runtime_id"] == resumed_runtime_id


@pytest.mark.asyncio
async def test_claim_next_execution_runs_different_threads_concurrently_but_serializes_each_thread(
    db_pool,
):
    from api.runtime_control import _claim_next_execution

    thread_a = f"slack:C-test:{uuid.uuid4().hex}:a"
    thread_b = f"slack:C-test:{uuid.uuid4().hex}:b"
    execution_a1 = f"exe-{uuid.uuid4().hex[:12]}"
    execution_a2 = f"exe-{uuid.uuid4().hex[:12]}"
    execution_b1 = f"exe-{uuid.uuid4().hex[:12]}"

    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, created_at"
        ") VALUES ($1, $2, 1, 'exec-a1', 'hash-a1', 'queued', '{}'::jsonb, '{}'::jsonb, "
        "NOW() - INTERVAL '3 seconds')",
        execution_a1,
        thread_a,
    )
    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, created_at"
        ") VALUES ($1, $2, 1, 'exec-a2', 'hash-a2', 'queued', '{}'::jsonb, '{}'::jsonb, "
        "NOW() - INTERVAL '2 seconds')",
        execution_a2,
        thread_a,
    )
    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, created_at"
        ") VALUES ($1, $2, 1, 'exec-b1', 'hash-b1', 'queued', '{}'::jsonb, '{}'::jsonb, "
        "NOW() - INTERVAL '1 second')",
        execution_b1,
        thread_b,
    )

    first_claim = await _claim_next_execution(db_pool)
    second_claim = await _claim_next_execution(db_pool)

    assert first_claim is not None
    assert second_claim is not None
    assert first_claim["execution_id"] == execution_a1
    assert second_claim["execution_id"] == execution_b1

    rows = await db_pool.fetch(
        "SELECT execution_id, status FROM agent_execution_requests "
        "WHERE execution_id = ANY($1::text[])",
        [execution_a1, execution_a2, execution_b1],
    )
    statuses = {row["execution_id"]: row["status"] for row in rows}
    assert statuses == {
        execution_a1: "running",
        execution_a2: "queued",
        execution_b1: "running",
    }


@pytest.mark.asyncio
async def test_worker_marks_silence_deadline_exceeded_and_stops_session(db_pool):
    from api.runtime_control import _process_execution

    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    execution_id = f"exe-{uuid.uuid4().hex[:12]}"
    prior_runtime_id = f"rt-old-{uuid.uuid4().hex[:8]}"
    resumed_runtime_id = f"rt-new-{uuid.uuid4().hex[:8]}"

    await db_pool.execute(
        "INSERT INTO agent_runtime_assignments ("
        "thread_key, assignment_generation, runtime_id, harness, engine, "
        "persona_id, prompt_ref, effective_agents_md_sha256, state"
        ") VALUES ($1, 1, $2, 'amp', 'amp', NULL, 'harness:amp', 'sha', 'active')",
        thread_key,
        prior_runtime_id,
    )
    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, hard_deadline_at"
        ") VALUES ($1, $2, 1, 'exec-idem', 'hash', 'running', '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '10 minutes')",
        execution_id,
        thread_key,
    )
    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox (execution_id, thread_key, delivery, state) "
        "VALUES ($1, $2, '{}'::jsonb, 'awaiting_terminal')",
        execution_id,
        thread_key,
    )

    row = {
        "execution_id": execution_id,
        "thread_key": thread_key,
        "assignment_generation": 1,
        "delivery": {},
        "hard_deadline_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
    }
    session = SandboxSession(
        sandbox_id=resumed_runtime_id,
        thread_key=thread_key,
        harness="amp",
        engine="amp",
    )

    async def _silent_stream(*_args, **_kwargs):
        await asyncio.sleep(60)
        if False:
            yield {}

    stop_session_mock = AsyncMock()
    with (
        patch("api.runtime_control.get_or_spawn", new=AsyncMock(return_value=session)),
        patch(
            "api.runtime_control.inject_stdin",
            new=AsyncMock(return_value={"ok": True, "injected": True, "durable_turn_id": "turn-1"}),
        ),
        patch("api.runtime_control.get_backend", return_value=object()),
        patch(
            "api.runtime_control._get_runtime",
            return_value=SimpleNamespace(turn_counter=1),
        ),
        patch("api.runtime_control._stream_stdout", _silent_stream),
        patch("api.runtime_control.stop_session", stop_session_mock),
        patch("api.runtime_control.EXECUTION_SILENCE_TIMEOUT_S", 0.05),
        patch("api.runtime_control.EXECUTION_WATCHDOG_POLL_S", 0.01),
    ):
        await _process_execution(db_pool, row)

    execution = await db_pool.fetchrow(
        "SELECT status, terminal_reason, error_text FROM agent_execution_requests WHERE execution_id = $1",
        execution_id,
    )
    assert execution is not None
    assert execution["status"] == "failed_permanent"
    assert execution["terminal_reason"] == "silence_deadline_exceeded"
    assert "no progress" in (execution["error_text"] or "")
    stop_session_mock.assert_awaited_once_with(thread_key)


@pytest.mark.asyncio
async def test_worker_reapplies_agents_override_on_runtime_replacement(db_pool):
    from api.runtime_control import _process_execution

    thread_key = f"slack:C-test:{uuid.uuid4().hex}"
    execution_id = f"exe-{uuid.uuid4().hex[:12]}"
    prior_runtime_id = f"rt-old-{uuid.uuid4().hex[:8]}"
    resumed_runtime_id = f"rt-new-{uuid.uuid4().hex[:8]}"
    await db_pool.execute(
        "INSERT INTO agent_runtime_assignments ("
        "thread_key, assignment_generation, runtime_id, harness, engine, persona_id, "
        "prompt_ref, effective_agents_md_sha256, agents_md_override, state"
        ") VALUES ($1, 1, $2, 'amp', 'amp', NULL, 'harness:amp', 'sha', $3, 'active')",
        thread_key,
        prior_runtime_id,
        "You are a very specific persona.",
    )
    await db_pool.execute(
        "INSERT INTO agent_execution_requests ("
        "execution_id, thread_key, assignment_generation, execute_id, request_hash, status, "
        "delivery, metadata, hard_deadline_at"
        ") VALUES ($1, $2, 1, 'exec-idem', 'hash', 'running', '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '10 minutes')",
        execution_id,
        thread_key,
    )
    await db_pool.execute(
        "INSERT INTO agent_final_delivery_outbox (execution_id, thread_key, delivery, state) "
        "VALUES ($1, $2, '{}'::jsonb, 'awaiting_terminal')",
        execution_id,
        thread_key,
    )

    row = {
        "execution_id": execution_id,
        "thread_key": thread_key,
        "assignment_generation": 1,
        "delivery": {},
        "hard_deadline_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10),
    }
    session = SandboxSession(
        sandbox_id=resumed_runtime_id,
        thread_key=thread_key,
        harness="amp",
        engine="amp",
    )

    async def _fake_stream(*_args, **_kwargs):
        yield {
            "data": json.dumps(
                {
                    "type": "turn.done",
                    "result": "done",
                    "is_error": False,
                }
            )
        }

    write_override = AsyncMock()
    with (
        patch("api.runtime_control.get_or_spawn", new=AsyncMock(return_value=session)),
        patch(
            "api.runtime_control.inject_stdin",
            new=AsyncMock(return_value={"ok": True, "injected": True, "durable_turn_id": "turn-1"}),
        ),
        patch("api.runtime_control.get_backend", return_value=object()),
        patch(
            "api.runtime_control._get_runtime",
            return_value=SimpleNamespace(turn_counter=1),
        ),
        patch("api.runtime_control._stream_stdout", _fake_stream),
        patch("api.runtime_control._write_agents_override", write_override),
    ):
        await _process_execution(db_pool, row)

    write_override.assert_awaited_once_with(
        resumed_runtime_id,
        "You are a very specific persona.",
    )


@pytest.mark.asyncio
async def test_legacy_connect_endpoint_disabled_by_default(client, api_key: str):
    res = await client.post(
        "/agent/connect",
        headers=_auth(api_key),
        json={"thread_key": f"slack:C-test:{uuid.uuid4().hex}"},
    )
    assert res.status_code == 410
    body = res.json()
    assert body["code"] == "LEGACY_ENDPOINT_REMOVED"


@pytest.mark.asyncio
async def test_legacy_reconnect_endpoint_disabled_by_default(client, api_key: str):
    res = await client.post(
        "/agent/reconnect",
        headers=_auth(api_key),
        json={"thread_key": f"slack:C-test:{uuid.uuid4().hex}"},
    )
    assert res.status_code == 410
    body = res.json()
    assert body["code"] == "LEGACY_ENDPOINT_REMOVED"


@pytest.mark.asyncio
async def test_bootstrap_service_api_keys_inserts_missing_rows(db_pool, monkeypatch):
    import api.api_keys as api_keys

    slack_key = f"aiv2_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    monkeypatch.setenv("SLACKBOT_API_KEY", slack_key)
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_API_KEY", raising=False)

    bootstrapped = await api_keys.bootstrap_service_api_keys(db_pool, secret_manager_url="")

    assert [info.name for info in bootstrapped] == ["service:slackbot"]
    row = await db_pool.fetchrow(
        "SELECT name, key_prefix, scopes, revoked_at, created_by FROM api_keys WHERE key_hash = $1",
        hashlib.sha256(slack_key.encode()).hexdigest(),
    )
    assert row is not None
    assert row["name"] == "service:slackbot"
    assert row["key_prefix"] == slack_key[:8]
    assert list(row["scopes"]) == ["agent"]
    assert row["revoked_at"] is None
    assert row["created_by"] == "service-bootstrap"

    resolved = await api_keys.lookup_key(db_pool, slack_key)
    assert resolved is not None
    assert resolved.name == "service:slackbot"
    assert resolved.scopes == ["agent"]


@pytest.mark.asyncio
async def test_bootstrap_service_api_keys_fetches_and_reactivates_revoked_rows(
    db_pool,
    monkeypatch,
):
    import api.api_keys as api_keys

    web_key = f"aiv2_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    await db_pool.execute(
        "INSERT INTO api_keys (name, key_prefix, key_hash, scopes, created_by, revoked_at) "
        "VALUES ($1, $2, $3, $4, $5, NOW())",
        "old-web-key",
        web_key[:8],
        hashlib.sha256(web_key.encode()).hexdigest(),
        ["agent"],
        "manual",
    )
    monkeypatch.delenv("SLACKBOT_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_API_KEY", raising=False)

    async def fake_fetch_secret_value(secret_manager_url: str, key: str) -> str | None:
        assert secret_manager_url == "http://secret-manager"
        return web_key if key == "WEB_API_KEY" else None

    monkeypatch.setattr(api_keys, "_fetch_secret_value", fake_fetch_secret_value)

    bootstrapped = await api_keys.bootstrap_service_api_keys(
        db_pool,
        secret_manager_url="http://secret-manager",
    )

    assert [info.name for info in bootstrapped] == ["service:web"]
    row = await db_pool.fetchrow(
        "SELECT name, scopes, revoked_at, created_by FROM api_keys WHERE key_hash = $1",
        hashlib.sha256(web_key.encode()).hexdigest(),
    )
    assert row is not None
    assert row["name"] == "service:web"
    assert list(row["scopes"]) == ["agent", "tools:paradigmdb"]
    assert row["revoked_at"] is None
    assert row["created_by"] == "service-bootstrap"


@pytest.mark.asyncio
async def test_bootstrap_service_api_keys_includes_local_dev_key(db_pool, monkeypatch):
    import api.api_keys as api_keys

    local_dev_key = f"aiv2_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    monkeypatch.delenv("SLACKBOT_API_KEY", raising=False)
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DEV_API_KEY", local_dev_key)

    bootstrapped = await api_keys.bootstrap_service_api_keys(db_pool, secret_manager_url="")

    assert [info.name for info in bootstrapped] == ["service:local-dev"]
    row = await db_pool.fetchrow(
        "SELECT name, scopes, revoked_at, created_by FROM api_keys WHERE key_hash = $1",
        hashlib.sha256(local_dev_key.encode()).hexdigest(),
    )
    assert row is not None
    assert row["name"] == "service:local-dev"
    assert list(row["scopes"]) == ["admin", "agent", "threads", "tools:*"]
    assert row["revoked_at"] is None
    assert row["created_by"] == "service-bootstrap"
