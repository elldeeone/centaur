"""Ironclad API client."""

import os
from typing import Any

import httpx
from shared.tool_sdk import secret

DEFAULT_BASE_URL = "https://na1.ironcladapp.com"

# SECURITY: All Ironclad API requests MUST use the service account email.
# Do NOT change this value or make it configurable (e.g. via env var or constructor param).
# Impersonating other users is not permitted.
_IMPERSONATE_EMAIL = "svc_ai@paradigm.xyz"

_OAUTH_SCOPES = "public.records.readRecords public.records.readSchemas public.workflows.readWorkflows public.entities.readEntities"


class IroncladClient:
    """Client for Ironclad CLM API."""

    def __init__(
        self,
        api_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("IRONCLAD_BASE_URL", DEFAULT_BASE_URL)).rstrip(
            "/"
        )
        self.api_url = f"{self.base_url}/public/api/v1"

        self.api_token = api_token or secret("IRONCLAD_API_TOKEN", "")
        self.client_id = client_id or secret("IRONCLAD_CLIENT_ID", "")
        self.client_secret = client_secret or secret("IRONCLAD_CLIENT_SECRET", "")

        if not self.api_token and not (self.client_id and self.client_secret):
            raise RuntimeError(
                "Ironclad authentication required.\n"
                "Set IRONCLAD_API_TOKEN or IRONCLAD_CLIENT_ID + IRONCLAD_CLIENT_SECRET.\n"
                "Get a token at: Company Settings → API"
            )

        self._access_token: str | None = None
        self._client = httpx.Client(timeout=30.0)

    def _get_access_token(self) -> str:
        """Get access token, using cached token or OAuth2 flow."""
        if self.api_token:
            return self.api_token

        if self._access_token:
            return self._access_token

        response = self._client.post(
            f"{self.base_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": _OAUTH_SCOPES,
            },
        )

        if response.status_code != 200:
            raise RuntimeError(f"Ironclad OAuth2 token request failed: {response.text}")

        data = response.json()
        self._access_token = data.get("access_token")
        if not self._access_token:
            raise RuntimeError("Ironclad OAuth2 response missing access_token")

        return self._access_token

    def _request(
        self, method: str, endpoint: str, params: dict | None = None, json_data: dict | None = None
    ) -> dict:
        """Make an authenticated request to the Ironclad API."""
        # SECURITY: Reject any attempt to override the impersonation email via env var.
        env_override = os.environ.get("IRONCLAD_IMPERSONATE_EMAIL")
        if env_override and env_override != _IMPERSONATE_EMAIL:
            raise RuntimeError(
                f"Impersonating other users is not permitted. "
                f"IRONCLAD_IMPERSONATE_EMAIL must not be set (got '{env_override}'). "
                f"All requests use {_IMPERSONATE_EMAIL}."
            )

        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-as-user-email": _IMPERSONATE_EMAIL,
        }

        url = f"{self.api_url}/{endpoint.lstrip('/')}"

        response = self._client.request(method, url, headers=headers, params=params, json=json_data)

        if response.status_code == 401:
            raise RuntimeError("Ironclad authentication failed - invalid or expired token")
        elif response.status_code == 403:
            raise RuntimeError("Ironclad access denied - insufficient permissions")
        elif response.status_code == 404:
            return {}
        elif response.status_code >= 400:
            raise RuntimeError(
                f"Ironclad API request failed ({response.status_code}): {response.text}"
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

    def _paginate(
        self, endpoint: str, params: dict | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch paginated results."""
        params = params or {}
        all_results = []
        page = 0
        page_size = min(limit, 100)

        while len(all_results) < limit:
            params["page"] = page
            params["pageSize"] = page_size

            result = self._get(endpoint, params)

            if isinstance(result, list):
                items = result
            else:
                items = result.get("list", result.get("results", result.get("data", [])))

            if not items:
                break

            all_results.extend(items)

            if len(items) < page_size:
                break

            page += 1

        return all_results[:limit]

    # ============== Workflows ==============

    def workflows(
        self,
        status: str | None = None,
        template_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List all workflows."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if template_id:
            params["template"] = template_id
        return self._paginate("workflows", params, limit=limit)

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        """Get workflow details."""
        return self._get(f"workflows/{workflow_id}")

    def workflow_approvals(self, workflow_id: str) -> list[dict[str, Any]]:
        """Get workflow approval requests."""
        result = self._get(f"workflows/{workflow_id}/approvals")
        return result.get("list", result) if isinstance(result, dict) else result

    def workflow_signers(self, workflow_id: str) -> list[dict[str, Any]]:
        """Get workflow signers."""
        result = self._get(f"workflows/{workflow_id}/signers")
        return result.get("list", result) if isinstance(result, dict) else result

    def workflow_documents(self, workflow_id: str) -> list[dict[str, Any]]:
        """Get workflow documents."""
        result = self._get(f"workflows/{workflow_id}/documents")
        return result.get("list", result) if isinstance(result, dict) else result

    def workflow_comments(self, workflow_id: str) -> list[dict[str, Any]]:
        """Get workflow comments."""
        result = self._get(f"workflows/{workflow_id}/comments")
        return result.get("list", result) if isinstance(result, dict) else result

    # ============== Workflow Schemas (Templates) ==============

    def schemas(self) -> list[dict[str, Any]]:
        """List all workflow schemas (templates)."""
        result = self._get("workflowSchemas")
        return result.get("list", result) if isinstance(result, dict) else result

    def schema(self, schema_id: str) -> dict[str, Any]:
        """Get workflow schema details."""
        return self._get(f"workflowSchemas/{schema_id}")

    # ============== Records ==============

    def records(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all records (completed contracts)."""
        return self._paginate("records", limit=limit)

    def record(self, record_id: str) -> dict[str, Any]:
        """Get record details."""
        return self._get(f"records/{record_id}")

    def record_attachments(self, record_id: str) -> list[dict[str, Any]]:
        """Get record attachments."""
        result = self._get(f"records/{record_id}/attachments")
        return result.get("list", result) if isinstance(result, dict) else result

    def records_schema(self) -> dict[str, Any]:
        """Get records schema (property definitions)."""
        return self._get("records/metadata")

    # ============== Entities ==============

    def entities(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all entities."""
        return self._paginate("entities", limit=limit)

    def entity(self, entity_id: str) -> dict[str, Any]:
        """Get entity details."""
        return self._get(f"entities/{entity_id}")

    def entity_types(self) -> list[dict[str, Any]]:
        """Get entity relationship types."""
        result = self._get("entityRelationshipTypes")
        return result.get("list", result) if isinstance(result, dict) else result

    # ============== Webhooks ==============

    def webhooks(self) -> list[dict[str, Any]]:
        """List all webhooks."""
        result = self._get("webhooks")
        return result.get("list", result) if isinstance(result, dict) else result

    def webhook(self, webhook_id: str) -> dict[str, Any]:
        """Get webhook details."""
        return self._get(f"webhooks/{webhook_id}")

    # ============== Obligations ==============

    def obligations(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all obligations."""
        return self._paginate("obligations", limit=limit)

    def obligation(self, obligation_id: str) -> dict[str, Any]:
        """Get obligation details."""
        return self._get(f"obligations/{obligation_id}")


def _client() -> IroncladClient:
    return IroncladClient()
