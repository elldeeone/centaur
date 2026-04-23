from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.last_headers: dict[str, str] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None):
        self.last_headers = headers
        return self._responses[url]


@pytest.mark.asyncio
async def test_check_runtime_credentials_skipped_when_guard_disabled() -> None:
    from api.runtime_guardrails import check_runtime_credentials

    with patch.dict(
        "os.environ",
        {
            "RUNTIME_CREDENTIAL_GUARD_ENABLED": "0",
            "REQUIRED_RUNTIME_SECRET_KEYS": "AMP_API_KEY",
        },
        clear=False,
    ):
        report = await check_runtime_credentials()

    assert report["enabled"] is False
    assert report["status"] == "skipped"


@pytest.mark.asyncio
async def test_check_runtime_credentials_ok_when_key_present() -> None:
    from api.runtime_guardrails import check_runtime_credentials

    base = "http://firewall:8081"
    url = f"{base}/secrets/AMP_API_KEY"

    with (
        patch.dict(
            "os.environ",
            {
                "RUNTIME_CREDENTIAL_GUARD_ENABLED": "1",
                "REQUIRED_RUNTIME_SECRET_KEYS": "AMP_API_KEY",
                "FIREWALL_HEALTH_URL": base,
            },
            clear=False,
        ),
        patch(
            "api.runtime_guardrails.httpx.AsyncClient",
            return_value=_FakeClient({url: _FakeResponse(200, {"value": "abc123"})}),
        ),
    ):
        report = await check_runtime_credentials()

    assert report["enabled"] is True
    assert report["status"] == "ok"
    assert report["key_lengths"] == {"AMP_API_KEY": 6}


@pytest.mark.asyncio
async def test_check_runtime_credentials_sends_bearer_when_token_set() -> None:
    """Verify the firewall control token is sent as Authorization: Bearer."""
    from api.runtime_guardrails import check_runtime_credentials

    base = "http://firewall:8081"
    url = f"{base}/secrets/AMP_API_KEY"
    fake = _FakeClient({url: _FakeResponse(200, {"value": "abc"})})

    with (
        patch.dict(
            "os.environ",
            {
                "RUNTIME_CREDENTIAL_GUARD_ENABLED": "1",
                "REQUIRED_RUNTIME_SECRET_KEYS": "AMP_API_KEY",
                "FIREWALL_HEALTH_URL": base,
                "FIREWALL_CONTROL_TOKEN": "test-token-xyz",
            },
            clear=False,
        ),
        patch(
            "api.runtime_guardrails.httpx.AsyncClient",
            return_value=fake,
        ),
    ):
        await check_runtime_credentials()

    assert fake.last_headers == {"Authorization": "Bearer test-token-xyz"}


@pytest.mark.asyncio
async def test_assert_runtime_credentials_ready_raises_when_missing() -> None:
    from api.runtime_guardrails import assert_runtime_credentials_ready

    base = "http://firewall:8081"
    url = f"{base}/secrets/AMP_API_KEY"

    with (
        patch.dict(
            "os.environ",
            {
                "RUNTIME_CREDENTIAL_GUARD_ENABLED": "1",
                "REQUIRED_RUNTIME_SECRET_KEYS": "AMP_API_KEY",
                "FIREWALL_HEALTH_URL": base,
            },
            clear=False,
        ),
        patch(
            "api.runtime_guardrails.httpx.AsyncClient",
            return_value=_FakeClient({url: _FakeResponse(404, {"error": "not found"})}),
        ),
    ):
        with pytest.raises(RuntimeError, match="runtime credential guard failed"):
            await assert_runtime_credentials_ready()
