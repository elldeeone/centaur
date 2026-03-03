"""DocuSign eSignature API client with JWT authentication."""

import os
import time
from typing import Any

import httpx
import jwt
from shared.tool_sdk import secret

DEMO_AUTH_URL = "https://account-d.docusign.com"
DEMO_API_URL = "https://demo.docusign.net/restapi"
PROD_AUTH_URL = "https://account.docusign.com"
PROD_API_URL = "https://na1.docusign.net/restapi"


class DocuSignClient:
    """Client for DocuSign eSignature API with JWT authentication."""

    def __init__(
        self,
        integration_key: str | None = None,
        user_id: str | None = None,
        account_id: str | None = None,
        private_key: str | None = None,
        private_key_path: str | None = None,
        env: str | None = None,
    ):
        self.integration_key = integration_key or secret("DOCUSIGN_INTEGRATION_KEY", "")
        self.user_id = user_id or os.environ.get("DOCUSIGN_USER_ID")
        self.account_id = account_id or secret("DOCUSIGN_ACCOUNT_ID", "")

        private_key_path = private_key_path or os.environ.get("DOCUSIGN_PRIVATE_KEY_PATH")
        self.private_key = private_key or secret("DOCUSIGN_PRIVATE_KEY", "")

        if private_key_path and not self.private_key:
            try:
                with open(private_key_path) as f:
                    self.private_key = f.read()
            except FileNotFoundError:
                raise RuntimeError(f"DocuSign private key file not found: {private_key_path}")

        if not all([self.integration_key, self.user_id, self.private_key]):
            missing = []
            if not self.integration_key:
                missing.append("DOCUSIGN_INTEGRATION_KEY")
            if not self.user_id:
                missing.append("DOCUSIGN_USER_ID")
            if not self.private_key:
                missing.append("DOCUSIGN_PRIVATE_KEY or DOCUSIGN_PRIVATE_KEY_PATH")
            raise RuntimeError(
                f"DocuSign authentication required. Missing: {', '.join(missing)}"
            )

        env = env or os.environ.get("DOCUSIGN_ENV", "demo")
        is_prod = env.lower() in ("production", "prod")

        self.auth_url = PROD_AUTH_URL if is_prod else DEMO_AUTH_URL
        self.api_url = PROD_API_URL if is_prod else DEMO_API_URL

        self._access_token: str | None = None
        self._token_expires: float = 0
        self._client = httpx.Client(timeout=30.0)

    def _get_access_token(self) -> str:
        """Get access token using JWT grant."""
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        now = int(time.time())
        payload = {
            "iss": self.integration_key,
            "sub": self.user_id,
            "aud": self.auth_url.replace("https://", ""),
            "iat": now,
            "exp": now + 3600,
            "scope": "signature impersonation",
        }

        assertion = jwt.encode(payload, self.private_key, algorithm="RS256")

        response = self._client.post(
            f"{self.auth_url}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )

        if response.status_code != 200:
            error_msg = response.text
            if "consent_required" in error_msg:
                consent_url = f"{self.auth_url}/oauth/auth?response_type=code&scope=signature%20impersonation&client_id={self.integration_key}&redirect_uri=https://www.docusign.com"
                raise RuntimeError(
                    f"DocuSign user consent required. Visit: {consent_url}"
                )
            else:
                raise RuntimeError(f"DocuSign OAuth token request failed: {error_msg}")

        data = response.json()
        self._access_token = data.get("access_token")
        self._token_expires = time.time() + data.get("expires_in", 3600)

        return self._access_token

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        """Make an authenticated request to the DocuSign API."""
        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        base = f"{self.api_url}/v2.1/accounts/{self.account_id}"
        url = f"{base}/{endpoint.lstrip('/')}"

        response = self._client.request(method, url, headers=headers, params=params, json=json_data)

        if response.status_code == 401:
            self._access_token = None
            self._token_expires = 0
            raise RuntimeError("DocuSign authentication failed - token expired or invalid")
        elif response.status_code == 403:
            raise RuntimeError("DocuSign access denied - insufficient permissions")
        elif response.status_code == 404:
            return {}
        elif response.status_code >= 400:
            raise RuntimeError(
                f"DocuSign API request failed ({response.status_code}): {response.text}"
            )

        if not response.text:
            return {}

        return response.json()

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a GET request."""
        return self._request("GET", endpoint, params=params)

    def _post(self, endpoint: str, json_data: dict | None = None) -> dict:
        """Make a POST request."""
        return self._request("POST", endpoint, json_data=json_data)

    # ============== Account ==============

    def account_info(self) -> dict[str, Any]:
        """Get account information."""
        token = self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        response = self._client.get(f"{self.auth_url}/oauth/userinfo", headers=headers)
        if response.status_code != 200:
            return {}

        return response.json()

    # ============== Envelopes ==============

    def envelopes(
        self,
        status: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List envelopes."""
        params: dict[str, Any] = {"count": min(limit, 100)}

        if status:
            params["status"] = status
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if search:
            params["search_text"] = search

        result = self._get("envelopes", params)
        return result.get("envelopes", [])

    def envelope(self, envelope_id: str) -> dict[str, Any]:
        """Get envelope details."""
        return self._get(f"envelopes/{envelope_id}")

    def envelope_recipients(self, envelope_id: str) -> dict[str, Any]:
        """Get envelope recipients."""
        return self._get(f"envelopes/{envelope_id}/recipients")

    def envelope_documents(self, envelope_id: str) -> list[dict[str, Any]]:
        """Get envelope documents."""
        result = self._get(f"envelopes/{envelope_id}/documents")
        return result.get("envelopeDocuments", [])

    def envelope_audit_events(self, envelope_id: str) -> list[dict[str, Any]]:
        """Get envelope audit events."""
        result = self._get(f"envelopes/{envelope_id}/audit_events")
        return result.get("auditEvents", [])

    def resend_envelope(self, envelope_id: str) -> dict[str, Any]:
        """Resend envelope notifications."""
        self.envelope_recipients(envelope_id)
        return self._request(
            "PUT", f"envelopes/{envelope_id}/recipients", params={"resend_envelope": "true"}
        )

    def void_envelope(self, envelope_id: str, reason: str) -> dict[str, Any]:
        """Void an envelope."""
        return self._request(
            "PUT",
            f"envelopes/{envelope_id}",
            json_data={"status": "voided", "voidedReason": reason},
        )

    # ============== Templates ==============

    def templates(self, limit: int = 100) -> list[dict[str, Any]]:
        """List templates."""
        params: dict[str, Any] = {"count": min(limit, 100)}
        result = self._get("templates", params)
        return result.get("envelopeTemplates", [])

    def template(self, template_id: str) -> dict[str, Any]:
        """Get template details."""
        return self._get(f"templates/{template_id}")

    def template_documents(self, template_id: str) -> list[dict[str, Any]]:
        """Get template documents."""
        result = self._get(f"templates/{template_id}/documents")
        return result.get("templateDocuments", [])

    # ============== Users ==============

    def users(self, limit: int = 100) -> list[dict[str, Any]]:
        """List users in the account."""
        params: dict[str, Any] = {"count": min(limit, 100)}
        result = self._get("users", params)
        return result.get("users", [])

    def user(self, user_id: str) -> dict[str, Any]:
        """Get user details."""
        return self._get(f"users/{user_id}")

    # ============== Folders ==============

    def folders(self) -> list[dict[str, Any]]:
        """List folders."""
        result = self._get("folders")
        return result.get("folders", [])

    def folder_items(self, folder_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get items in a folder."""
        params: dict[str, Any] = {"count": min(limit, 100)}
        result = self._get(f"folders/{folder_id}", params)
        return result.get("folderItems", [])

    # ============== Signing Groups ==============

    def signing_groups(self) -> list[dict[str, Any]]:
        """List signing groups."""
        result = self._get("signing_groups")
        return result.get("groups", [])

    # ============== Brands ==============

    def brands(self) -> list[dict[str, Any]]:
        """List brands."""
        result = self._get("brands")
        return result.get("brands", [])


def _client() -> DocuSignClient:
    """Factory: create a DocuSignClient from env vars."""
    return DocuSignClient()
