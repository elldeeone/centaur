"""Environment-variable secret manager backend.

Loads secrets from ``os.environ``, optionally filtered by a key prefix.
Useful for local development or environments where secrets are injected
directly as env vars (e.g., via Docker, Kubernetes, or CI).
"""

from __future__ import annotations

import logging
import os

from centaur_sdk.providers.base import SecretManagerBackend

log = logging.getLogger("secret_manager")


_EXCLUDED_KEYS = frozenset(
    {
        "HOME",
        "HOSTNAME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "PATH",
        "PWD",
        "SECRET_ENV_PREFIX",
        "SECRET_MANAGER_BACKEND",
        "SECRET_REFRESH_RETRY_SECONDS",
        "SECRET_REFRESH_SECONDS",
        "SHELL",
        "SHLVL",
        "TERM",
        "USER",
    }
)


class EnvSecretManagerBackend(SecretManagerBackend):
    """Load secrets from environment variables.

    Args:
        prefix: If set, only env vars starting with this prefix are loaded
                and the prefix is stripped from the key name.
                E.g. ``prefix="SECRET_"`` turns ``SECRET_FOO=bar`` into ``FOO=bar``.
                When no prefix is set, operational/system env vars are excluded
                (see ``_EXCLUDED_KEYS``).
    """

    def __init__(self, prefix: str | None = None) -> None:
        self._prefix = prefix

    @property
    def supports_refresh(self) -> bool:
        return False

    async def load_all(self) -> dict[str, str]:
        if self._prefix:
            result = {
                k[len(self._prefix) :]: v
                for k, v in os.environ.items()
                if k.startswith(self._prefix)
            }
        else:
            result = {k: v for k, v in os.environ.items() if k not in _EXCLUDED_KEYS}

        log.info("loaded %d keys from environment", len(result))
        return result
