"""BitGo API client."""

from typing import Any

import httpx
from shared.tool_sdk import secret

BASE_URL = "https://app.bitgo.com/api/v2"
STAKING_BASE_URL = "https://app.bitgo.com/api/staking/v1"


class BitGoClient:

    def __init__(self, access_token: str | None = None):
        self._token = access_token or secret("BITGO_API_KEY", "")
        if not self._token:
            raise RuntimeError(
                "BITGO_API_KEY not set.\n"
                "Generate one at https://app.bitgo.com/settings/developer-options"
            )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers=headers,
            timeout=30.0,
        )
        self._staking_client = httpx.Client(
            base_url=STAKING_BASE_URL,
            headers=headers,
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Make authenticated request to BitGo API."""
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json()
                msg = error.get("error", error.get("message", response.text))
            except Exception:
                msg = response.text
            raise RuntimeError(f"BitGo API error ({response.status_code}): {msg}")
        return response.json()

    def list_wallets(self, coin: str | None = None, limit: int = 25) -> list[dict]:
        """List all wallets or wallets for a specific coin."""
        params: dict[str, Any] = {"limit": limit}
        if coin:
            data = self._request("GET", f"/{coin}/wallet", params=params)
        else:
            data = self._request("GET", "/wallets", params=params)
        return data.get("wallets", [])

    def get_wallet(self, coin: str, wallet_id: str) -> dict:
        """Get wallet details by coin and ID."""
        return self._request("GET", f"/{coin}/wallet/{wallet_id}")

    def get_wallet_balance(self, coin: str, wallet_id: str) -> dict:
        """Get wallet balance."""
        data = self._request("GET", f"/{coin}/wallet/{wallet_id}")
        return {
            "coin": data.get("coin"),
            "label": data.get("label"),
            "balance": data.get("balance"),
            "balanceString": data.get("balanceString"),
            "confirmedBalance": data.get("confirmedBalance"),
            "confirmedBalanceString": data.get("confirmedBalanceString"),
            "spendableBalance": data.get("spendableBalance"),
            "spendableBalanceString": data.get("spendableBalanceString"),
        }

    def get_total_balances(self, enterprise_id: str | None = None) -> dict:
        """Get total balances across all wallets."""
        params = {}
        if enterprise_id:
            params["enterprise"] = enterprise_id
        return self._request("GET", "/wallet/balances", params=params if params else None)

    def list_transactions(
        self, coin: str, wallet_id: str, limit: int = 25, prev_id: str | None = None
    ) -> list[dict]:
        """List transactions (transfers) for a wallet."""
        params: dict[str, Any] = {"limit": limit}
        if prev_id:
            params["prevId"] = prev_id
        data = self._request("GET", f"/{coin}/wallet/{wallet_id}/transfer", params=params)
        return data.get("transfers", [])

    def get_transaction(self, coin: str, wallet_id: str, transfer_id: str) -> dict:
        """Get transaction details."""
        return self._request("GET", f"/{coin}/wallet/{wallet_id}/transfer/{transfer_id}")

    def list_addresses(self, coin: str, wallet_id: str, limit: int = 25) -> list[dict]:
        """List addresses for a wallet."""
        params: dict[str, Any] = {"limit": limit}
        data = self._request("GET", f"/{coin}/wallet/{wallet_id}/addresses", params=params)
        return data.get("addresses", [])

    def list_enterprises(self) -> list[dict]:
        """List enterprises the user belongs to."""
        data = self._request("GET", "/enterprise")
        return data.get("enterprises", [])

    def get_enterprise(self, enterprise_id: str) -> dict:
        """Get enterprise details."""
        return self._request("GET", f"/enterprise/{enterprise_id}")

    def raw_request(self, endpoint: str, method: str = "GET", **kwargs) -> dict | list:
        """Make a raw API call to any endpoint."""
        return self._request(method, endpoint, **kwargs)

    def _staking_request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Make authenticated request to BitGo Staking API (/api/staking/v1)."""
        response = self._staking_client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                error = response.json()
                msg = error.get("error", error.get("message", response.text))
            except Exception:
                msg = response.text
            raise RuntimeError(f"BitGo API error ({response.status_code}): {msg}")
        return response.json()

    def list_staking_coins(self) -> list[dict]:
        """List coins available for staking."""
        data = self._staking_request("GET", "/coins")
        return data.get("coins", []) if isinstance(data, dict) else data

    def list_staking_requests(
        self, enterprise_id: str | None = None, limit: int = 25
    ) -> list[dict]:
        """List staking requests for an enterprise."""
        params: dict[str, Any] = {"limit": limit}
        if enterprise_id:
            data = self._staking_request(
                "GET", f"/enterprises/{enterprise_id}/requests", params=params
            )
        else:
            data = self._staking_request("GET", "/requests", params=params)
        return data.get("requests", []) if isinstance(data, dict) else data

    def get_staking_request(self, request_id: str) -> dict:
        """Get details of a staking request."""
        return self._staking_request("GET", f"/requests/{request_id}")

    def list_staking_delegations(
        self, coin: str, wallet_id: str, limit: int = 25
    ) -> list[dict]:
        """List staking delegations for a wallet."""
        params: dict[str, Any] = {"limit": limit}
        data = self._staking_request(
            "GET", f"/{coin}/wallets/{wallet_id}/delegations", params=params
        )
        return data.get("delegations", []) if isinstance(data, dict) else data

    def list_staking_rewards(self, enterprise_id: str) -> list[dict]:
        """List staking rewards for an enterprise."""
        data = self._staking_request("GET", f"/enterprises/{enterprise_id}/rewards")
        return data.get("rewards", []) if isinstance(data, dict) else data

    def get_staking_wallet_attributes(self, coin: str, wallet_id: str) -> dict:
        """Get staking attributes for a wallet."""
        return self._staking_request("GET", f"/{coin}/wallets/{wallet_id}")

    def list_partnered_validators(self, coin: str | None = None) -> list[dict]:
        """List partnered validators for staking."""
        params = {}
        if coin:
            params["coin"] = coin
        data = self._staking_request("GET", "/validators", params=params if params else None)
        return data.get("validators", []) if isinstance(data, dict) else data

    def close(self):
        self._client.close()
        self._staking_client.close()


def _client() -> BitGoClient:
    return BitGoClient(access_token=secret("BITGO_API_KEY"))
