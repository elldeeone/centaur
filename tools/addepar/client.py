"""Addepar API client for entities, prices, portfolio queries, and imports."""

import base64
import json
import os
from typing import Any

import httpx

BASE_URL = "https://paradigm.addepar.com/api"
ADDEPAR_FIRM_ID = "1962"
MAX_PAGE_SIZE = 2000

DEFAULT_PORTFOLIO_COLUMNS = [
    "value",
    "performance_contribution",
    "performance_contribution_inception",
]


class AddeparClient:

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: float = 60.0,
    ):
        self._api_key = api_key or os.getenv("ADDEPAR_API_KEY")
        self._api_secret = api_secret or os.getenv("ADDEPAR_API_SECRET")

        if not self._api_key or not self._api_secret:
            raise RuntimeError(
                "Addepar credentials not set.\n"
                "Required: ADDEPAR_API_KEY, ADDEPAR_API_SECRET"
            )

        credentials = f"{self._api_key}:{self._api_secret}"
        self._auth_header = f"Basic {base64.b64encode(credentials.encode()).decode()}"
        self._timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE_URL,
                headers={
                    "Authorization": self._auth_header,
                    "Addepar-Firm": ADDEPAR_FIRM_ID,
                },
                timeout=self._timeout,
            )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        body: dict | str | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Addepar API."""
        extra_headers = headers or {}

        if method.upper() == "GET":
            response = self.client.get(path, params=params, headers=extra_headers)
        elif method.upper() == "POST":
            if isinstance(body, str):
                response = self.client.post(
                    path, content=body, params=params, headers=extra_headers
                )
            else:
                response = self.client.post(
                    path, json=body, params=params, headers=extra_headers
                )
        elif method.upper() == "DELETE":
            response = self.client.delete(path, params=params, headers=extra_headers)
        else:
            response = self.client.request(
                method, path, json=body, params=params, headers=extra_headers
            )

        if response.status_code >= 400:
            try:
                error = response.json()
                msg = json.dumps(error, indent=2)
            except Exception:
                msg = response.text
            raise RuntimeError(f"Addepar API error ({response.status_code}): {msg}")

        return response.json()

    def _fetch_all_pages(
        self, path: str, params: dict | None = None
    ) -> list[dict[str, Any]]:
        """Fetch all pages from a JSON:API paginated endpoint."""
        all_data: list[dict[str, Any]] = []
        next_url: str | None = path
        base_params = {"page[limit]": MAX_PAGE_SIZE, **(params or {})}

        while next_url:
            result = self._request("GET", next_url, params=base_params)
            data = result.get("data", [])
            if isinstance(data, list):
                all_data.extend(data)
            else:
                all_data.append(data)

            links = result.get("links", {})
            next_url = links.get("next")
            # After first request, params are encoded in the next URL
            base_params = {}

        return all_data

    def list_entities(self, limit: int = 100) -> list[dict[str, Any]]:
        """List entities from Addepar.

        Args:
            limit: Maximum number of entities to return.

        Returns:
            List of entity objects with id, type, and attributes.
        """
        if limit >= MAX_PAGE_SIZE:
            return self._fetch_all_pages("/v1/entities")

        result = self._request("GET", "/v1/entities", params={"page[limit]": limit})
        return result.get("data", [])

    def get_entity(self, entity_id: int) -> dict[str, Any]:
        """Get a single entity by ID.

        Args:
            entity_id: The Addepar entity ID.

        Returns:
            Entity object with id, type, and attributes.
        """
        result = self._request("GET", f"/v1/entities/{entity_id}")
        return result.get("data", result)

    def get_entity_prices(
        self, entity_id: int, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """Get historical prices for an entity.

        Args:
            entity_id: The Addepar entity ID.
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.

        Returns:
            List of historical price objects.
        """
        result = self._request(
            "GET",
            f"/v1/entities/{entity_id}/prices",
            params={"start_date": start_date, "end_date": end_date},
        )
        return result.get("data", [])

    def portfolio_query(
        self,
        start_date: str,
        end_date: str,
        portfolio_id: int = 0,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a portfolio performance contribution query.

        Args:
            start_date: Start date in YYYY-MM-DD format.
            end_date: End date in YYYY-MM-DD format.
            portfolio_id: Portfolio node ID (0 for the firm-level portfolio).
            columns: List of column attribute keys to include. Defaults to
                value, performance_contribution, performance_contribution_inception.

        Returns:
            Portfolio query response with meta and data.
        """
        cols = columns or DEFAULT_PORTFOLIO_COLUMNS
        query = {
            "data": {
                "type": "portfolio_views",
                "attributes": {
                    "portfolio_type": "ENTITY",
                    "portfolio_id": portfolio_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "columns": [{"key": c} for c in cols],
                    "groupings": [{"key": "security"}],
                },
            }
        }
        return self._request(
            "POST",
            "/v1/portfolio/query",
            body=query,
            headers={
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            },
        )

    def import_prices(self, csv_data: str) -> dict[str, Any]:
        """Import historical prices via CSV upload.

        Args:
            csv_data: CSV string with columns: Entity ID, Price, Date (MM/DD/YYYY).

        Returns:
            Import job result with job ID and status.
        """
        return self._request(
            "POST",
            "/v1/imports",
            body=csv_data,
            params={
                "import_type": "HISTORICAL_PRICES",
                "ignore_warnings": "true",
                "is_dry_run": "false",
            },
            headers={"Content-Type": "text/plain"},
        )

    def get_import_status(self, import_id: str) -> dict[str, Any]:
        """Check the status of an import job.

        Args:
            import_id: The import job UUID.

        Returns:
            Import job status object.
        """
        return self._request("GET", f"/v1/imports/{import_id}")

    def raw_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """Make a raw API request to any Addepar endpoint."""
        return self._request(method, path, body=body, params=params)

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> AddeparClient:
    from shared.tool_sdk import secret

    return AddeparClient(
        api_key=secret("ADDEPAR_API_KEY"),
        api_secret=secret("ADDEPAR_API_SECRET"),
    )
