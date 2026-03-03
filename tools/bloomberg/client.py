"""Bloomberg Data License REST API client with JWT authentication."""

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64encode

import httpx
from shared.tool_sdk import secret


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _hex_to_bytes(hex_string: str) -> bytes:
    """Convert hex string to bytes. Supports both hex-encoded and raw strings."""
    cleaned = hex_string.strip()
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return cleaned.encode("utf-8")


class BloombergClient:
    """Client for Bloomberg Data License REST API."""

    PROD_HOST = "api.bloomberg.com"
    BETA_HOST = "beta.api.bloomberg.com"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        dl_number: str | None = None,
        use_beta: bool = False,
        timeout: float = 60.0,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._dl_number = dl_number
        self.use_beta = use_beta
        self.timeout = timeout
        self._http_client: httpx.Client | None = None

    @property
    def dl_number(self) -> str:
        """Get the DL (Data License) number."""
        if self._dl_number:
            return self._dl_number
        dl = secret("BLOOMBERG_DL_NUMBER", "")
        if dl:
            return dl
        raise RuntimeError("Bloomberg DL number not found. Set BLOOMBERG_DL_NUMBER env var.")

    @property
    def host(self) -> str:
        return self.BETA_HOST if self.use_beta else self.PROD_HOST

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def http_client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._http_client

    def _get_credentials(self) -> tuple[str, str]:
        """Get credentials from instance or environment variables."""
        if self._client_id and self._client_secret:
            return self._client_id, self._client_secret

        client_id = secret("BLOOMBERG_CLIENT_ID", "")
        client_secret = secret("BLOOMBERG_CLIENT_SECRET", "")
        if client_id and client_secret:
            return client_id, client_secret

        api_key = secret("BLOOMBERG_API_KEY", "")
        if api_key and ":" in api_key:
            client_id, client_secret = api_key.split(":", 1)
            return client_id, client_secret

        raise RuntimeError(
            "Bloomberg credentials not found. Set BLOOMBERG_CLIENT_ID and "
            "BLOOMBERG_CLIENT_SECRET env vars."
        )

    def _generate_jwt(self, method: str, path: str) -> str:
        """Generate a signed JWT for Bloomberg API authentication."""
        client_id, client_secret = self._get_credentials()
        secret_bytes = _hex_to_bytes(client_secret)

        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iss": client_id,
            "exp": now + 25,
            "nbf": now,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "region": "ny",
            "method": method.upper(),
            "path": path,
            "host": self.host,
            "client_id": client_id,
        }

        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        unsigned_token = f"{header_b64}.{payload_b64}"

        signature = hmac.new(secret_bytes, unsigned_token.encode(), hashlib.sha256).digest()
        signature_b64 = _b64url_encode(signature)

        return f"{unsigned_token}.{signature_b64}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        max_redirects: int = 5,
    ) -> dict | list:
        """Make an authenticated API request with manual redirect handling."""
        current_path = path
        current_method = method

        for _ in range(max_redirects + 1):
            if isinstance(current_path, tuple):
                jwt_path, url_path = current_path
            else:
                jwt_path = url_path = current_path

            jwt_token = self._generate_jwt(current_method, jwt_path)
            url = f"{self.base_url}{url_path}"
            headers = {
                "jwt": jwt_token,
                "Content-Type": "application/json",
                "api-version": "2",
            }

            try:
                response = self.http_client.request(
                    method=current_method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )

                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if location:
                        from urllib.parse import urlparse

                        if location.startswith("http"):
                            parsed = urlparse(location)
                            jwt_path = parsed.path
                            url_path = location if not location.startswith("http") else parsed.path
                            if parsed.query:
                                url_path = f"{parsed.path}?{parsed.query}"
                        else:
                            if "?" in location:
                                jwt_path = location.split("?")[0]
                                url_path = location
                            else:
                                jwt_path = location
                                url_path = location
                        current_path = (jwt_path, url_path)
                        if response.status_code == 303:
                            current_method = "GET"
                            json_body = None
                            params = None
                        continue

                response.raise_for_status()
                if response.text:
                    return response.json()
                return {}
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"API error: {e.response.status_code} - {e.response.text}")
            except httpx.RequestError as e:
                raise RuntimeError(f"Request failed: {e}")

        raise RuntimeError(f"Too many redirects (max {max_redirects})")

    def get_catalog(self) -> dict:
        """Get the data catalog."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/")

    def get_datasets(self) -> list:
        """List available datasets."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/datasets/")

    def get_dataset(self, dataset: str) -> dict:
        """Get dataset details."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/datasets/{dataset}/")

    def get_fields(self) -> list:
        """List available fields."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/fields/")

    def get_universes(self) -> list:
        """List user universes."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/universes/")

    def create_universe(self, name: str, identifiers: list[str]) -> dict:
        """Create a universe of securities."""
        body = {
            "identifier": name,
            "title": name,
            "contains": [
                {"@type": "Identifier", "identifierType": "TICKER", "identifierValue": i}
                for i in identifiers
            ],
        }
        return self._request("POST", f"/eap/catalogs/{self.dl_number}/universes/", json_body=body)

    def get_requests(self) -> list:
        """List data requests."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/requests/")

    def create_request(
        self,
        universe_id: str,
        fields: list[str],
        request_id: str | None = None,
    ) -> dict:
        """Create a data request."""
        body = {
            "identifier": request_id or str(uuid.uuid4()),
            "universe": universe_id,
            "fieldList": [{"mnemonic": f} for f in fields],
        }
        return self._request("POST", f"/eap/catalogs/{self.dl_number}/requests/", json_body=body)

    def get_request_status(self, request_id: str) -> dict:
        """Get status of a data request."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/requests/{request_id}/")

    def get_distributions(self) -> list:
        """List available distributions (completed request outputs)."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/distributions/")

    def get_distribution(self, distribution_id: str) -> dict:
        """Get distribution details."""
        return self._request(
            "GET", f"/eap/catalogs/{self.dl_number}/distributions/{distribution_id}/"
        )

    def download_distribution(self, distribution_id: str, filename: str) -> bytes:
        """Download a distribution file."""
        path = f"/eap/catalogs/{self.dl_number}/distributions/{distribution_id}/files/{filename}"
        jwt_token = self._generate_jwt("GET", path)
        url = f"{self.base_url}{path}"
        headers = {"jwt": jwt_token, "api-version": "2"}

        response = self.http_client.get(url, headers=headers)
        response.raise_for_status()
        return response.content

    def get_schedules(self) -> list:
        """List scheduled jobs."""
        return self._request("GET", f"/eap/catalogs/{self.dl_number}/schedules/")

    def close(self):
        """Close the HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> BloombergClient:
    return BloombergClient()
