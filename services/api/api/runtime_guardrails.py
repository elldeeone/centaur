from __future__ import annotations

import os
from urllib.parse import quote

import httpx


def runtime_credential_guard_enabled() -> bool:
    return os.getenv("RUNTIME_CREDENTIAL_GUARD_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def required_runtime_secret_keys() -> list[str]:
    raw = os.getenv("REQUIRED_RUNTIME_SECRET_KEYS", "AMP_API_KEY")
    return [k.strip() for k in raw.split(",") if k.strip()]


async def check_runtime_credentials() -> dict[str, object]:
    enabled = runtime_credential_guard_enabled()
    keys = required_runtime_secret_keys()
    if not enabled:
        return {
            "enabled": False,
            "status": "skipped",
            "required_keys": keys,
            "missing_keys": [],
            "errors": [],
            "key_lengths": {},
        }

    firewall_url = os.getenv("FIREWALL_HEALTH_URL", "http://firewall:8081").rstrip("/")
    control_token = os.environ.get("FIREWALL_CONTROL_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {control_token}"} if control_token else {}
    missing_keys: list[str] = []
    errors: list[str] = []
    key_lengths: dict[str, int] = {}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for key in keys:
                url = f"{firewall_url}/secrets/{quote(key, safe='')}"
                try:
                    resp = await client.get(url, headers=headers)
                except Exception as exc:  # pragma: no cover - network failures are environment-specific
                    errors.append(f"{key}:request_failed:{exc}")
                    continue

                if resp.status_code == 404:
                    missing_keys.append(key)
                    continue
                if resp.status_code != 200:
                    errors.append(f"{key}:unexpected_status:{resp.status_code}")
                    continue

                try:
                    payload = resp.json()
                except Exception:
                    errors.append(f"{key}:invalid_json")
                    continue

                value = payload.get("value")
                if not isinstance(value, str) or not value:
                    missing_keys.append(key)
                    continue
                key_lengths[key] = len(value)
    except Exception as exc:  # pragma: no cover - network failures are environment-specific
        errors.append(f"credential_check_failed:{exc}")

    status = "ok" if not missing_keys and not errors else "failed"
    return {
        "enabled": True,
        "status": status,
        "required_keys": keys,
        "missing_keys": missing_keys,
        "errors": errors,
        "key_lengths": key_lengths,
    }


async def assert_runtime_credentials_ready() -> None:
    report = await check_runtime_credentials()
    if report.get("enabled") and report.get("status") != "ok":
        missing = ",".join(report.get("missing_keys", []))
        errors = ";".join(report.get("errors", []))
        raise RuntimeError(
            "runtime credential guard failed"
            + (f" missing_keys={missing}" if missing else "")
            + (f" errors={errors}" if errors else "")
        )
