"""LegiStorm Congressional API client."""


import httpx
from centaur_sdk.tool_sdk import secret


class LegiStormClient:
    """Client for LegiStorm Congressional API."""

    BASE_URL = "https://api.legistorm.com/v2.019.10/congress"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 60.0,
    ):
        self._api_key = api_key
        self.timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _get_api_key(self) -> str:
        api_key = self._api_key or secret("LEGISTORM_API_KEY", "")
        if not api_key:
            raise RuntimeError("LEGISTORM_API_KEY not set.")
        return api_key

    def _request(self, endpoint: str, params: dict | None = None) -> dict:
        """Make an API request."""
        api_key = self._get_api_key()
        url = f"{self.BASE_URL}{endpoint}"

        headers = {
            "X-Api-Key": api_key,
            "Accept": "application/json",
        }

        try:
            response = self.client.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"API error: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            raise RuntimeError(f"Request failed: {e}")

    def get_members(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        member_id: int | None = None,
        state_id: str | None = None,
        status: str = "a",
    ) -> dict:
        """Get congressional members.

        Args:
            updated_from: YYYY-MM-DD - entities updated after this date
            updated_to: YYYY-MM-DD - entities updated before this date
            limit: Max results (up to 1000)
            page: Page number
            member_id: Specific member ID
            state_id: State postal abbreviation (e.g., CA, NY)
            status: a=all, c=current, i=incoming, d=departing
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 1000),
            "page": page,
            "status": status,
        }
        if member_id:
            params["id"] = member_id
        if state_id:
            params["state_id"] = state_id

        return self._request("/member/list", params)

    def get_staff(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        staff_id: int | None = None,
        member_id: int | None = None,
        office_id: int | None = None,
    ) -> dict:
        """Get congressional staff.

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            limit: Max results (up to 1000)
            page: Page number
            staff_id: Specific staff ID
            member_id: Staff for a specific member
            office_id: Staff for a specific office
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 1000),
            "page": page,
        }
        if staff_id:
            params["id"] = staff_id
        if member_id:
            params["member_id"] = member_id
        if office_id:
            params["office_id"] = office_id

        return self._request("/staff/list", params)

    def get_staff_retired_ids(self) -> dict:
        """Get IDs of staff no longer employed by Congress."""
        return self._request("/staff/retired-ids")

    def get_offices(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        office_id: int | None = None,
    ) -> dict:
        """Get offices (committees, subcommittees, commissions, admin offices).

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            limit: Max results (up to 1000)
            page: Page number
            office_id: Specific office ID
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 1000),
            "page": page,
        }
        if office_id:
            params["id"] = office_id

        return self._request("/office/list", params)

    def get_offices_retired_ids(self) -> dict:
        """Get IDs of inactive offices."""
        return self._request("/office/retired-ids")

    def get_caucuses(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        caucus_id: int | None = None,
    ) -> dict:
        """Get congressional caucuses (requires caucus subscription).

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            limit: Max results (up to 1000)
            page: Page number
            caucus_id: Specific caucus ID
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 1000),
            "page": page,
        }
        if caucus_id:
            params["id"] = caucus_id

        return self._request("/caucus/list", params)

    def get_caucuses_retired_ids(self) -> dict:
        """Get IDs of inactive/deleted caucuses."""
        return self._request("/caucus/retired-ids")

    def get_townhalls(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        townhall_id: int | None = None,
    ) -> dict:
        """Get town hall events.

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            limit: Max results (up to 100)
            page: Page number
            townhall_id: Specific town hall ID
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 100),
            "page": page,
        }
        if townhall_id:
            params["id"] = townhall_id

        return self._request("/townhall/list", params)

    def get_trips(
        self,
        updated_from: str,
        updated_to: str,
        limit: int = 20,
        page: int = 1,
        trip_id: int | None = None,
    ) -> dict:
        """Get privately funded travel.

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            limit: Max results (up to 100)
            page: Page number
            trip_id: Specific trip ID
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "limit": min(limit, 100),
            "page": page,
        }
        if trip_id:
            params["id"] = trip_id

        return self._request("/trips/list", params)

    def get_hearings(
        self,
        updated_from: str,
        updated_to: str,
        chamber: str = "H",
        limit: int = 20,
        page: int = 1,
        hearing_id: int | None = None,
        office_id: int | None = None,
        hearing_date_from: str | None = None,
        hearing_date_to: str | None = None,
    ) -> dict:
        """Get congressional hearings.

        Args:
            updated_from: YYYY-MM-DD
            updated_to: YYYY-MM-DD
            chamber: H=House, S=Senate
            limit: Max results (up to 100)
            page: Page number
            hearing_id: Specific hearing ID
            office_id: Filter by committee/office
            hearing_date_from: YYYY-MM-DD filter by hearing date
            hearing_date_to: YYYY-MM-DD filter by hearing date
        """
        params = {
            "updated_from": updated_from,
            "updated_to": updated_to,
            "chamber": chamber,
            "limit": min(limit, 100),
            "page": page,
        }
        if hearing_id:
            params["id"] = hearing_id
        if office_id:
            params["office_id"] = office_id
        if hearing_date_from:
            params["hearing_date_from"] = hearing_date_from
        if hearing_date_to:
            params["hearing_date_to"] = hearing_date_to

        return self._request("/hearings/list", params)

    def close(self):
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> LegiStormClient:
    return LegiStormClient()
