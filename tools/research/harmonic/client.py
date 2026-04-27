"""Harmonic.AI API client."""


import httpx

from centaur_sdk import secret


def _clean_secret(value: str | None) -> str | None:
    """Clean a secret value that may come from a multi-line 1Password field."""
    if not value:
        return value
    value = value.strip()
    if "\n" not in value:
        return value or None
    for line in value.splitlines():
        line = line.strip()
        if not line or line.startswith("===") or line.startswith("#"):
            continue
        return line
    return None


class HarmonicClient:
    """Client for Harmonic.AI API."""

    def __init__(self, api_key: str | None = None, timeout: float = 60.0):
        self._api_key = _clean_secret(api_key)
        self.base_url = "https://api.harmonic.ai"
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _get_api_key(self) -> str | None:
        """Get API key from instance or env var."""
        if self._api_key:
            return self._api_key
        return _clean_secret(secret("HARMONIC_API_KEY", ""))

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict | list:
        """Make an API request."""
        api_key = self._get_api_key()
        if not api_key:
            raise RuntimeError("HARMONIC_API_KEY not set.")

        url = f"{self.base_url}{endpoint}"
        headers = {"apikey": api_key, "Content-Type": "application/json"}

        try:
            if method.upper() == "GET":
                response = self.client.get(url, params=params, headers=headers)
            elif method.upper() == "POST":
                response = self.client.post(url, params=params, headers=headers, json=json_body)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"API error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise RuntimeError(f"Request failed: {e}")

    def enrich_company(
        self,
        website_url: str | None = None,
        website_domain: str | None = None,
        linkedin_url: str | None = None,
        crunchbase_url: str | None = None,
        pitchbook_url: str | None = None,
        twitter_url: str | None = None,
    ) -> dict:
        """Enrich a company by passing one or more identifiers."""
        params = {}
        if website_url:
            params["website_url"] = website_url
        if website_domain:
            params["website_domain"] = website_domain
        if linkedin_url:
            params["linkedin_url"] = linkedin_url
        if crunchbase_url:
            params["crunchbase_url"] = crunchbase_url
        if pitchbook_url:
            params["pitchbook_url"] = pitchbook_url
        if twitter_url:
            params["twitter_url"] = twitter_url

        if not params:
            raise ValueError("At least one identifier is required")

        return self._request("POST", "/companies", params=params)

    def enrich_person(self, linkedin_url: str) -> dict:
        """Enrich a person by LinkedIn URL."""
        return self._request("POST", "/persons", params={"linkedin_url": linkedin_url})

    def get_enrichment_status(
        self,
        ids: list[str] | None = None,
        urns: list[str] | None = None,
    ) -> dict:
        """Get enrichment status for given IDs or URNs."""
        params = {}
        if ids:
            params["ids"] = ",".join(ids)
        if urns:
            params["urns"] = ",".join(urns)
        return self._request("GET", "/enrichment_status", params=params)

    def get_saved_searches(self) -> dict:
        """Get all saved searches accessible to your account."""
        return self._request("GET", "/savedSearches")

    def get_saved_search_results(
        self,
        id_or_urn: str,
        cursor: str | None = None,
        size: int = 50,
    ) -> dict:
        """Get results from a saved search."""
        params = {"size": size}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/savedSearches:results/{id_or_urn}", params=params)

    def get_saved_search_net_new(
        self,
        id_or_urn: str,
        new_results_since: str | None = None,
        cursor: str | None = None,
        size: int = 50,
    ) -> dict:
        """Get net new results for a subscribed saved search."""
        params = {"size": size}
        if new_results_since:
            params["new_results_since"] = new_results_since
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/savedSearches:netNewResults/{id_or_urn}", params=params)

    def clear_net_new_results(self, id_or_urn: str) -> dict:
        """Clear net new results for a saved search."""
        return self._request("POST", f"/savedSearches:netNewResults/{id_or_urn}:clear")

    def search_companies_natural_language(
        self,
        query: str,
        size: int = 25,
        cursor: str | None = None,
        similarity_threshold: float | None = None,
    ) -> dict:
        """Search companies using natural language (Scout Search)."""
        params = {"query": query, "size": size}
        if cursor:
            params["cursor"] = cursor
        if similarity_threshold is not None:
            params["similarity_threshold"] = similarity_threshold
        return self._request("GET", "/search/search_agent", params=params)

    def get_similar_companies(
        self,
        company_id: str | int,
        size: int = 25,
    ) -> dict:
        """Get companies similar to a given company."""
        params = {"size": size}
        return self._request("GET", f"/search/similar_companies/{company_id}", params=params)

    def search_typeahead(
        self,
        query: str,
        search_type: str = "COMPANY",
    ) -> dict:
        """Typeahead search for companies, people, or investors."""
        params = {"query": query, "search_type": search_type}
        return self._request("GET", "/search/typeahead", params=params)

    def get_company_connections(self, company_id: str | int) -> dict:
        """Get team network connections to a company."""
        return self._request("GET", f"/companies/{company_id}/userConnections")

    def create_saved_search(self, name: str, keywords: str) -> dict:
        """Create a new saved search."""
        body = {"name": name, "keywords": keywords}
        return self._request("POST", "/savedSearches", json_body=body)

    def raw(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict | list:
        """Make a raw API call."""
        return self._request(method, endpoint, params=params, json_body=json_body)

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> HarmonicClient:
    # Seed the client with the secret placeholder at load time so the firewall
    # can replace it in-flight, matching the pattern used by other proxy-backed tools.
    return HarmonicClient(api_key=secret("HARMONIC_API_KEY", ""))
