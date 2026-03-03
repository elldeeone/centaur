"""AlphaSense Enterprise API client."""

import os
from datetime import datetime

import httpx
from shared.tool_sdk import secret


class AlphaSenseClient:
    """Client for AlphaSense Enterprise API (GraphQL + REST)."""

    def __init__(
        self,
        api_key: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 60.0,
    ):
        self._api_key = api_key
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self.auth_url = "https://api.alpha-sense.com/auth"
        self.graphql_url = "https://api.alpha-sense.com/gql"
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _get_credentials(self) -> tuple[str, str, str, str, str]:
        """Get credentials from instance or env vars."""
        api_key = self._api_key or secret("ALPHASENSE_API_KEY", "")
        client_id = self._client_id or secret("ALPHASENSE_CLIENT_ID", "")
        client_secret = self._client_secret or secret("ALPHASENSE_CLIENT_SECRET", "")
        username = self._username or os.getenv("ALPHASENSE_USERNAME")
        password = self._password or secret("ALPHASENSE_PASSWORD", "")

        if not api_key:
            raise RuntimeError("ALPHASENSE_API_KEY not set.")
        if not client_id:
            raise RuntimeError("ALPHASENSE_CLIENT_ID not set.")
        if not client_secret:
            raise RuntimeError("ALPHASENSE_CLIENT_SECRET not set.")
        if not username:
            raise RuntimeError("ALPHASENSE_USERNAME not set.")
        if not password:
            raise RuntimeError("ALPHASENSE_PASSWORD not set.")

        return api_key, client_id, client_secret, username, password

    def _get_access_token(self) -> str:
        """Get or refresh access token."""
        if self._access_token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._access_token

        api_key, client_id, client_secret, username, password = self._get_credentials()

        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        try:
            response = self.client.post(self.auth_url, headers=headers, data=data)
            response.raise_for_status()
            result = response.json()
            self._access_token = result["access_token"]
            expires_in = result.get("expires_in", 3600)
            from datetime import timedelta

            self._token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
            return self._access_token
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Auth error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise RuntimeError(f"Auth request failed: {e}")

    def _graphql_request(self, query: str, variables: dict | None = None) -> dict:
        """Make a GraphQL API request."""
        api_key, client_id, _, _, _ = self._get_credentials()
        access_token = self._get_access_token()

        headers = {
            "x-api-key": api_key,
            "clientid": client_id,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            response = self.client.post(self.graphql_url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            if "errors" in result:
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return result.get("data", {})
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"API error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise RuntimeError(f"Request failed: {e}")

    def search(
        self,
        query: str,
        limit: int = 20,
        date_preset: str = "LAST_90_DAYS",
        sort_field: str = "DATE",
        sort_direction: str = "DESC",
        cursor: str | None = None,
        companies: list[str] | None = None,
        doc_types: list[str] | None = None,
        countries: list[str] | None = None,
    ) -> dict:
        """Search for documents."""
        gql = """
        query Search($filter: SearchFilter!, $limit: Int!, $sorting: SearchSorting, $cursor: String) {
            search(filter: $filter, limit: $limit, sorting: $sorting, cursor: $cursor) {
                totalCount
                cursor
                documents {
                    id
                    title
                    releasedAt
                    pageCount
                    relevanceScore
                    type {
                        ids
                        display
                    }
                    source {
                        originalUrl
                    }
                    companies {
                        id
                        name
                        primaryTickerCode
                        country {
                            code
                        }
                    }
                    countryCodes
                    documentAuthors
                }
            }
        }
        """

        filter_obj: dict = {
            "keyword": {"query": query},
            "date": {"preset": date_preset},
        }

        if companies:
            filter_obj["companies"] = {"ids": companies}
        if doc_types:
            filter_obj["types"] = {"ids": doc_types}
        if countries:
            filter_obj["countries"] = countries

        variables = {
            "filter": filter_obj,
            "limit": min(limit, 100),
            "sorting": {"field": sort_field, "direction": sort_direction},
        }
        if cursor:
            variables["cursor"] = cursor

        return self._graphql_request(gql, variables)

    def get_document(self, doc_id: str) -> dict:
        """Get document details by ID."""
        gql = """
        query GetDocument($docId: String!) {
            searchByDocId(docId: $docId) {
                id
                title
                releasedAt
                pageCount
                type {
                    ids
                    display
                }
                source {
                    originalUrl
                }
                companies {
                    id
                    name
                    primaryTickerCode
                    type
                    country {
                        code
                    }
                }
                countryCodes
                documentAuthors
                summary
                sentiment {
                    score
                    previousScore
                    changePercentage
                }
                industries {
                    id
                    name
                }
            }
        }
        """
        return self._graphql_request(gql, {"docId": doc_id})

    def lookup_companies(self, identifiers: list[dict]) -> dict:
        """Look up companies by various identifiers."""
        gql = """
        query Companies($inputs: [CompanyIdInput!]!) {
            companies(inputs: $inputs) {
                id
                name
                primaryTickerCode
                isin
                cik
                crunchbaseId
                type
                country {
                    code
                    name
                }
            }
        }
        """
        return self._graphql_request(gql, {"inputs": identifiers})

    def get_saved_searches(self) -> dict:
        """Get list of saved searches."""
        gql = """
        query SavedSearches {
            savedSearches {
                id
                name
            }
        }
        """
        return self._graphql_request(gql)

    def search_by_saved_id(
        self,
        saved_search_id: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict:
        """Execute a saved search by ID."""
        gql = """
        query SearchById($id: String!, $limit: Int, $cursor: String) {
            searchById(id: $id, limit: $limit, cursor: $cursor) {
                search {
                    totalCount
                    cursor
                    documents {
                        id
                        title
                        releasedAt
                        pageCount
                        type {
                            ids
                            display
                        }
                        companies {
                            id
                            name
                            primaryTickerCode
                        }
                    }
                }
            }
        }
        """
        variables = {"id": saved_search_id, "limit": min(limit, 100)}
        if cursor:
            variables["cursor"] = cursor
        return self._graphql_request(gql, variables)

    def get_user(self) -> dict:
        """Get current user info."""
        gql = """
        query User {
            user {
                id
            }
        }
        """
        return self._graphql_request(gql)

    def get_watchlists(self) -> dict:
        """Get user's watchlists."""
        gql = """
        query Watchlists {
            myWatchlists {
                id
                name
                companiesCount
                type
            }
        }
        """
        return self._graphql_request(gql)

    def get_watchlist_companies(self, watchlist_id: str) -> dict:
        """Get companies in a watchlist."""
        gql = """
        query WatchlistCompanies($id: String!) {
            myWatchlist(id: $id) {
                id
                name
                companies {
                    id
                    name
                    primaryTickerCode
                    country {
                        code
                    }
                }
            }
        }
        """
        return self._graphql_request(gql, {"id": watchlist_id})

    def gen_search(
        self,
        prompt: str,
        max_attempts: int = 30,
        poll_interval: float = 5.0,
    ) -> dict:
        """Run a generative search (async with polling)."""
        import time

        # Step 1: Initiate the search
        init_mutation = """
        mutation GenSearch($input: GenSearchInput!) {
            genSearch {
                thinkLonger(input: $input) {
                    id
                }
            }
        }
        """
        init_result = self._graphql_request(init_mutation, {"input": {"prompt": prompt}})
        conversation_id = init_result.get("genSearch", {}).get("thinkLonger", {}).get("id")
        if not conversation_id:
            raise RuntimeError(f"Failed to initiate genSearch: {init_result}")

        # Step 2: Poll for results
        poll_query = """
        query Query($conversationId: String!) {
            genSearch {
                conversation(id: $conversationId) {
                    id
                    markdown
                    progress
                    error {
                        code
                    }
                }
            }
        }
        """

        for attempt in range(max_attempts):
            result = self._graphql_request(poll_query, {"conversationId": conversation_id})
            conversation = result.get("genSearch", {}).get("conversation", {})
            progress = conversation.get("progress", 0)

            if progress == 1.0:
                return {
                    "id": conversation.get("id"),
                    "markdown": conversation.get("markdown", ""),
                    "progress": progress,
                }

            if conversation.get("error"):
                raise RuntimeError(f"GenSearch error: {conversation['error']}")

            time.sleep(poll_interval)

        raise RuntimeError(f"GenSearch timed out after {max_attempts} attempts")

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> AlphaSenseClient:
    return AlphaSenseClient()
