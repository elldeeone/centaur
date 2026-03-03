"""ParadigmDB tool — Paradigm internal database, Shift notes, and BigQuery."""


class ParadigmDBClient:
    """Client for Paradigm's internal databases and Shift notes."""

    def _ensure_tunnel(self) -> None:
        from .database import is_tunnel_running, start_persistent_tunnel
        if not is_tunnel_running():
            start_persistent_tunnel()

    def db_query(self, query: str, limit: int = 20) -> list[dict]:
        """Execute a read-only SQL query against Paradigm's internal PostgreSQL database.

        Args:
            query: SQL query to execute
            limit: Max rows to return
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        results = db.query(query)
        return results[:limit] if results else []

    def db_tables(self, schema: str = "public") -> list[str]:
        """List all tables in the internal database.

        Args:
            schema: Database schema (default: public)
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.list_tables(schema=schema)

    def db_describe(self, table_name: str, schema: str = "public") -> list[dict]:
        """Describe columns of a database table.

        Args:
            table_name: Name of the table to describe
            schema: Database schema (default: public)
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.describe_table(table_name, schema=schema)

    def db_funds(self, limit: int = 100) -> list[dict]:
        """Get list of funds.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_funds(limit=limit)

    def db_assets(self, limit: int = 100) -> list[dict]:
        """Get list of assets.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_assets(limit=limit)

    def db_asset_by_symbol(self, symbol: str) -> dict | None:
        """Get asset by ticker symbol.

        Args:
            symbol: Ticker symbol (e.g. ETH, BTC)
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_asset_by_symbol(symbol)

    def db_daily_prices(
        self, asset_id: int, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        """Get daily prices for an asset.

        Args:
            asset_id: The asset ID
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_daily_prices(asset_id, start_date=start_date, end_date=end_date)

    def db_transactions(self, limit: int = 100) -> list[dict]:
        """Get recent transactions.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_transactions(limit=limit)

    def db_organizations(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get organizations, optionally filtered by name.

        Args:
            search: Filter by name (case-insensitive substring match)
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_organizations(search=search, limit=limit)

    def db_organization(self, org_id: str) -> dict | None:
        """Get a single organization by id.

        Args:
            org_id: The organization ID
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_organization(org_id)

    def db_people(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get people, optionally filtered by name.

        Args:
            search: Filter by name (case-insensitive substring match)
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_people(search=search, limit=limit)

    def db_person(self, person_id: str) -> dict | None:
        """Get a single person by id.

        Args:
            person_id: The person ID
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_person(person_id)

    def db_positions(self, fund: str | None = None, limit: int = 100) -> list[dict]:
        """Get latest portfolio positions (asset performance snapshots) with market values.

        Args:
            fund: Filter by fund name (case-insensitive substring match)
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_positions(fund=fund, limit=limit)

    def db_events(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get hosted events, optionally filtered by name.

        Args:
            search: Filter by name (case-insensitive substring match)
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_events(search=search, limit=limit)

    def db_funding_rounds(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get equity financing rounds, optionally filtered by name.

        Args:
            search: Filter by name (case-insensitive substring match)
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_funding_rounds(search=search, limit=limit)

    def db_equity_financing(self, limit: int = 100) -> list[dict]:
        """Get equity financing events.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_equity_financing(limit=limit)

    def db_valuations(self, limit: int = 100) -> list[dict]:
        """Get organization valuations.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_valuations(limit=limit)

    def db_corrections(self, limit: int = 100) -> list[dict]:
        """Get asset performance corrections.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_corrections(limit=limit)

    def db_cash_balances(self, limit: int = 100) -> list[dict]:
        """Get JPM bank cash balances.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_cash_balances(limit=limit)

    def db_jpm_transactions(self, limit: int = 100) -> list[dict]:
        """Get JPM transactions.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_jpm_transactions(limit=limit)

    def db_anchorage_balances(self, limit: int = 100) -> list[dict]:
        """Get Anchorage wallet balances.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_anchorage_balances(limit=limit)

    def db_coinbase_balances(self, limit: int = 100) -> list[dict]:
        """Get Coinbase wallet balances.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .database import get_db
        db = get_db()
        return db.get_coinbase_balances(limit=limit)

    def bq_query(self, query: str, limit: int = 100) -> list[dict]:
        """Execute a BigQuery SQL query against custody-dashboard views.

        Args:
            query: BigQuery SQL query
            limit: Max rows to return
        """
        from .bigquery import query_bigquery
        return query_bigquery(query, limit=limit)

    def bq_tables(self) -> list[str]:
        """List all tables/views in the BigQuery shift_prod_public_views dataset."""
        from .bigquery import list_tables
        return list_tables()

    def bq_describe(self, table_name: str) -> list[dict]:
        """Get schema for a BigQuery table/view.

        Args:
            table_name: Name of the table to describe
        """
        from .bigquery import describe_table
        return describe_table(table_name)

    def bq_transactions(
        self,
        ticker: str | None = None,
        fund: str | None = None,
        transaction_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query transactions from BigQuery with optional filters.

        Args:
            ticker: Filter by ticker symbol
            fund: Filter by fund (PF, P1, P2)
            transaction_type: Filter by transaction type
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            limit: Max results
        """
        from .bigquery import get_transactions
        return get_transactions(
            ticker=ticker,
            fund=fund,
            transaction_type=transaction_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    def notes_search(self, query: str, note_type: str = "", limit: int = 20) -> list[dict]:
        """Search Shift notes from the investment process.

        Args:
            query: Search text
            note_type: Filter by type (OPPORTUNITY, PORTCO_UPDATE, PORTCO_REVIEW, TALENT, GTM, etc.)
            limit: Max results
        """
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        notes = client.search_notes(query, note_type=note_type or None, limit=limit)
        return [
            {
                "id": n.id,
                "title": n.title,
                "type": n.note_type,
                "created_at": n.created_at.isoformat(),
                "created_by": n.created_by_name,
                "notes": n.notes[:500],
            }
            for n in notes
        ]

    def notes_read(self, note_id: str) -> dict:
        """Read a full Shift note by ID.

        Args:
            note_id: The note ID
        """
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        data = client.get_note_with_relations(note_id)
        if not data:
            return {"error": f"Note '{note_id}' not found"}
        note = data["note"]
        return {
            "id": note.id,
            "title": note.title,
            "type": note.note_type,
            "source": note.source,
            "created_at": note.created_at.isoformat(),
            "created_by": note.created_by_name,
            "organizations": data["organizations"],
            "people": data["people"],
            "notes": note.notes,
        }

    def notes_list(self, note_type: str = "", limit: int = 20) -> list[dict]:
        """List recent Shift notes.

        Args:
            note_type: Filter by type (OPPORTUNITY, PORTCO_UPDATE, PORTCO_REVIEW, TALENT, GTM, etc.)
            limit: Max results
        """
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        notes = client.list_notes(note_type=note_type or None, limit=limit)
        return [
            {
                "id": n.id,
                "title": n.title,
                "type": n.note_type,
                "created_at": n.created_at.isoformat(),
                "created_by": n.created_by_name,
                "notes": n.notes[:500],
            }
            for n in notes
        ]

    def notes_stats(self) -> dict:
        """Get statistics about Shift notes."""
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        return client.get_stats()

    def notes_for_org(self, org_name: str, limit: int = 20) -> list[dict]:
        """Get notes related to an organization.

        Args:
            org_name: Organization name to search for
            limit: Max results
        """
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        notes = client.get_notes_for_organization(org_name, limit=limit)
        return [
            {
                "id": n.id,
                "title": n.title,
                "type": n.note_type,
                "created_at": n.created_at.isoformat(),
                "created_by": n.created_by_name,
                "notes": n.notes[:500],
            }
            for n in notes
        ]

    def notes_authors(self, limit: int = 20) -> list[dict]:
        """Get top Shift note authors.

        Args:
            limit: Max results
        """
        self._ensure_tunnel()
        from .notes import get_notes_client
        client = get_notes_client()
        return client.get_authors(limit=limit)


def _client() -> ParadigmDBClient:
    return ParadigmDBClient()
