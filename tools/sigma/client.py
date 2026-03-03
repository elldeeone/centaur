"""Sigma Computing API client with OAuth2 client credentials flow."""

import os
import time
from typing import Any

import httpx
from shared.tool_sdk import secret

BASE_URL = "https://api.sigmacomputing.com/v2"


class SigmaClient:
    """Client for Sigma Computing API."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 30.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self.timeout = timeout
        self._token_cache: dict[str, Any] = {}

    def _get_credentials(self) -> tuple[str, str]:
        """Get client credentials from environment."""
        client_id = self._client_id or secret("SIGMA_CLIENT_ID", "")
        client_secret = self._client_secret or secret("SIGMA_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET must be set.\n"
                "Create API credentials in Sigma Admin > Developer Access."
            )
        return client_id, client_secret

    def _get_access_token(self) -> str:
        """Get access token using client credentials grant, with caching."""
        if (
            self._token_cache.get("access_token")
            and self._token_cache.get("expires_at", 0) > time.time() + 60
        ):
            return self._token_cache["access_token"]

        client_id, client_secret = self._get_credentials()

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{BASE_URL}/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Failed to get access token: {response.text}")

            data = response.json()
            self._token_cache = {
                "access_token": data["access_token"],
                "expires_at": time.time() + data.get("expires_in", 3600),
            }
            return self._token_cache["access_token"]

    def _get_http_client(self) -> httpx.Client:
        """Get authenticated Sigma HTTP client."""
        token = self._get_access_token()
        return httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Make authenticated request to Sigma API."""
        with self._get_http_client() as client:
            response = client.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    error = response.json()
                    msg = error.get("message", error.get("error", response.text))
                except Exception:
                    msg = response.text
                raise RuntimeError(f"Sigma API error ({response.status_code}): {msg}")
            return response.json()

    def list_workbooks(self, limit: int = 50) -> list[dict]:
        """List all workbooks."""
        data = self._request("GET", "/workbooks", params={"limit": limit})
        return data.get("entries", [])

    def get_workbook(self, workbook_id: str) -> dict:
        """Get workbook details."""
        return self._request("GET", f"/workbooks/{workbook_id}")

    def list_pages(self, workbook_id: str) -> list[dict]:
        """List pages in a workbook."""
        data = self._request("GET", f"/workbooks/{workbook_id}/pages")
        return data.get("entries", [])

    def list_members(self, limit: int = 50) -> list[dict]:
        """List organization members."""
        data = self._request("GET", "/members", params={"limit": limit})
        return data.get("entries", [])

    def list_teams(self, limit: int = 50) -> list[dict]:
        """List teams."""
        data = self._request("GET", "/teams", params={"limit": limit})
        return data.get("entries", [])

    def raw_request(self, method: str, endpoint: str, body: dict | None = None) -> Any:
        """Make raw API request."""
        kwargs = {}
        if body:
            kwargs["json"] = body
        return self._request(method, endpoint, **kwargs)

    def generate_embed_url(
        self,
        workbook_id: str,
        email: str,
        account_type: str = "viewer",
        teams: list[str] | None = None,
        session_length: int = 3600,
        mode: str = "userbacked",
    ) -> str:
        """Generate embed URL for a workbook using the embed secret."""
        import jwt

        embed_secret = secret("SIGMA_EMBED_SECRET", "")
        if not embed_secret:
            raise RuntimeError(
                "SIGMA_EMBED_SECRET must be set.\n"
                "Create an embed secret in Sigma Admin > Developer Access > Embedding."
            )

        now = int(time.time())
        claims = {
            "sub": email,
            "iat": now,
            "exp": now + session_length,
            "account_type": account_type,
            "teams": teams or [],
            "mode": mode,
        }

        token = jwt.encode(claims, embed_secret, algorithm="HS256")

        base_embed_url = os.environ.get(
            "SIGMA_EMBED_BASE_URL", "https://app.sigmacomputing.com/embed"
        )
        return f"{base_embed_url}/{workbook_id}?:jwt={token}"


def _client() -> SigmaClient:
    return SigmaClient()
