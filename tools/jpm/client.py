"""JPMorgan Open Banking API client with JWT/OAuth2 authentication."""

import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import jwt

BASE_URL = "https://openbanking.jpmorgan.com"
OAUTH_TOKEN_URL = "https://idag2.jpmorganchase.com/adfs/oauth2/token/"
CLIENT_ID = "CC-104221-B080567-399284-PROD"
CERTIFICATE_THUMBPRINT = "8A:49:16:EB:E2:F6:50:19:8D:E2:A8:75:74:71:A3:D2:C6:7E:8E:36"
KEY_ID = CERTIFICATE_THUMBPRINT.replace(":", "")


class JPMClient:
    """Client for JPMorgan Open Banking API."""

    def __init__(
        self,
        private_key: str | None = None,
        account_ids: list[str] | None = None,
    ):
        raw_key = private_key or os.getenv("JPM_API_PRIVATE_KEY") or ""
        self._private_key = raw_key.replace("\\n", "\n")

        raw_ids = os.getenv("JPM_API_ACCOUNT_IDS") or ""
        self._account_ids = account_ids or [
            a.strip() for a in raw_ids.split(",") if a.strip()
        ]

        if not self._private_key:
            raise RuntimeError(
                "JPM API private key not set.\n"
                "Required: JPM_API_PRIVATE_KEY (PEM-encoded RSA private key)\n"
                "Optional: JPM_API_ACCOUNT_IDS (comma-separated account IDs)"
            )

        self._token: str | None = None
        self._token_expires_at: float = 0

    def _build_jwt(self) -> str:
        """Construct the JWT assertion for client_credentials grant."""
        now = int(time.time())
        payload = {
            "iss": CLIENT_ID,
            "aud": OAUTH_TOKEN_URL,
            "sub": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(
            payload,
            self._private_key,
            algorithm="RS256",
            headers={"alg": "RS256", "kid": KEY_ID},
        )

    def _get_token(self) -> str:
        """Mint JWT, exchange for bearer token, cache until expiry."""
        if self._token and time.time() < self._token_expires_at:
            return self._token

        client_assertion = self._build_jwt()
        form_data = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion,
            "client_id": CLIENT_ID,
            "resource": "https://apigeeproductProd.jpmchase.net",
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                OAUTH_TOKEN_URL,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"JPM OAuth token request failed ({response.status_code}): {response.text}"
                )
            data = response.json()

        self._token = data["access_token"]
        # Token expires in 12 hours; refresh 5 minutes early
        self._token_expires_at = time.time() + (12 * 3600) - 300
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make an authenticated request to JPMorgan Open Banking API."""
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        with httpx.Client(base_url=BASE_URL, headers=headers, timeout=60.0) as client:
            if method == "GET":
                response = client.get(path, params=params)
            elif method == "POST":
                response = client.post(path, json=body)
            else:
                response = client.request(method, path, json=body, params=params)

            if response.status_code >= 400:
                try:
                    error = response.json()
                    msg = error.get("message", error.get("error", response.text))
                except Exception:
                    msg = response.text
                raise RuntimeError(f"JPM API error ({response.status_code}): {msg}")
            return response.json()

    def get_cash_balances(self, start_date: str) -> list[dict]:
        """Get cash balances for all configured accounts.

        Args:
            start_date: Balance date in YYYY-MM-DD format.

        Returns:
            List of account balance records (errors filtered out).
        """
        body = {
            "accountList": [{"accountId": aid} for aid in self._account_ids],
            "startDate": start_date,
        }
        data = self._request("POST", "/accessapi/balance", body=body)
        return [
            account
            for account in data.get("accountList", [])
            if "errorCode" not in account
        ]

    def get_transactions(self, date: str, limit: int = 100) -> list[dict]:
        """Get transactions for all configured accounts on a given date.

        Auto-paginates through all result pages.

        Args:
            date: Transaction date in YYYY-MM-DD format.
            limit: Max transactions per page (default 100).

        Returns:
            List of transaction records.
        """
        start_date = date
        end_dt = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        end_date = end_dt.strftime("%Y-%m-%d")

        all_transactions: list[dict] = []
        page_number: int | None = None

        while True:
            params: dict[str, Any] = {
                "accountIds": self._account_ids,
                "startDate": start_date,
                "endDate": end_date,
            }
            if page_number is not None:
                params["pageNumber"] = page_number

            data = self._request("GET", "/tsapi/v3/transactions", params=params)
            all_transactions.extend(data.get("data", []))

            pagination = data.get("pagination", {})
            current_page = pagination.get("pageNumber", 1)
            total_pages = pagination.get("totalPages", 1)

            if current_page >= total_pages:
                break
            page_number = current_page + 1

        return all_transactions


def _client() -> JPMClient:
    from shared.tool_sdk import secret

    return JPMClient(
        private_key=secret("JPM_API_PRIVATE_KEY"),
        account_ids=[
            a.strip()
            for a in (secret("JPM_API_ACCOUNT_IDS", "") or "").split(",")
            if a.strip()
        ],
    )
