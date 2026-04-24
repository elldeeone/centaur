from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from api.apps import AppManager


@pytest.mark.asyncio
async def test_recover_apps_skips_docker_client_for_kubernetes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = AppManager()
    pool = AsyncMock()

    monkeypatch.setenv("SANDBOX_BACKEND", "kubernetes")

    def fail_if_called():
        raise AssertionError("docker client should not be constructed")

    monkeypatch.setattr(manager, "_get_client", fail_if_called)

    recovered = await manager.recover_apps(pool)

    assert recovered == 0
    pool.fetch.assert_not_called()
