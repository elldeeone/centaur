"""PostgreSQL database client for Paradigm's internal database.

Automatically manages SSH tunnel through GCP bastion host.

Key tables:
    - Fund: list of funds
    - XAssetBase: list of assets
    - XTransactionBase: transactions
    - Flow: assets involved in trades/investments
    - XAssetDailyPrice: daily asset prices
"""

import os
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from shared.tool_sdk import secret

# PID file for persistent tunnel
TUNNEL_PID_FILE = Path.home() / ".reshift-tunnel.pid"

# Database connection details — all resolved via secret() at connect time.
DB_HOST = "10.78.18.5"
DB_PORT = 5432
DB_NAME = "pmadmin"

def _find_free_port() -> int:
    """Find a free local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

class SSHTunnel:
    """SSH tunnel using gcloud compute ssh."""

    DEFAULT_PORT = 54321
    GCP_ZONE = "us-central1-a"
    GCP_PROJECT = "custody-dashboard"
    BASTION_HOST = "bastion"

    def __init__(self, remote_host: str, remote_port: int):
        self.remote_host = remote_host
        self.remote_port = remote_port
        self._process: subprocess.Popen | None = None
        self._local_port: int = self.DEFAULT_PORT

    @property
    def local_bind_port(self) -> int:
        return self._local_port

    @property
    def is_active(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self):
        """Start the SSH tunnel via gcloud."""
        if self.is_active:
            return

        cmd = [
            "gcloud",
            "compute",
            "ssh",
            "--zone",
            self.GCP_ZONE,
            "--project",
            self.GCP_PROJECT,
            self.BASTION_HOST,
            "--",
            "-N",  # Don't execute remote command
            "-L",
            f"127.0.0.1:{self._local_port}:{self.remote_host}:{self.remote_port}",
        ]

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait for tunnel to be ready
        for _ in range(50):  # 5 seconds
            time.sleep(0.1)
            if self._process.poll() is not None:
                stderr = self._process.stderr.read().decode() if self._process.stderr else ""
                raise RuntimeError(f"SSH tunnel failed: {stderr}")
            try:
                with socket.create_connection(("127.0.0.1", self._local_port), timeout=0.1):
                    return
            except (TimeoutError, ConnectionRefusedError, OSError):
                continue

        raise RuntimeError("SSH tunnel timeout - could not connect")

    def stop(self):
        """Stop the SSH tunnel."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

def start_persistent_tunnel() -> int:
    """Start a persistent SSH tunnel and save PID to file. Returns PID."""
    # Check if tunnel already running
    if is_tunnel_running():
        pid = int(TUNNEL_PID_FILE.read_text().strip())
        return pid

    cmd = [
        "gcloud",
        "compute",
        "ssh",
        "--zone",
        SSHTunnel.GCP_ZONE,
        "--project",
        SSHTunnel.GCP_PROJECT,
        "--tunnel-through-iap",
        SSHTunnel.BASTION_HOST,
        "--",
        "-N",
        "-L",
        f"127.0.0.1:{SSHTunnel.DEFAULT_PORT}:{DB_HOST}:{DB_PORT}",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # Detach from parent
    )

    # Save PID
    TUNNEL_PID_FILE.write_text(str(process.pid))

    # Wait for tunnel to be ready (IAP tunnels can take 30+ seconds)
    for _ in range(120):
        time.sleep(0.5)
        try:
            with socket.create_connection(("127.0.0.1", SSHTunnel.DEFAULT_PORT), timeout=0.1):
                return process.pid
        except (TimeoutError, ConnectionRefusedError, OSError):
            continue

    raise RuntimeError("SSH tunnel timeout - could not connect")

def stop_persistent_tunnel() -> bool:
    """Stop the persistent SSH tunnel. Returns True if stopped."""
    if not TUNNEL_PID_FILE.exists():
        return False

    try:
        pid = int(TUNNEL_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        TUNNEL_PID_FILE.unlink()
        return True
    except (ProcessLookupError, ValueError):
        # Process already dead
        TUNNEL_PID_FILE.unlink(missing_ok=True)
        return False

def is_tunnel_running() -> bool:
    """Check if persistent tunnel is running.

    First checks if the port is reachable (works in containers with host networking),
    then falls back to PID file check for local process management.
    """
    # Check if port is open (works across host/container boundary with network_mode=host)
    try:
        with socket.create_connection(("127.0.0.1", SSHTunnel.DEFAULT_PORT), timeout=0.5):
            return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        pass

    # Fall back to PID file check for local tunnel management
    if not TUNNEL_PID_FILE.exists():
        return False

    try:
        pid = int(TUNNEL_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        # Also verify port is open
        with socket.create_connection(("127.0.0.1", SSHTunnel.DEFAULT_PORT), timeout=0.5):
            return True
    except (TimeoutError, ProcessLookupError, ValueError, ConnectionRefusedError, OSError):
        TUNNEL_PID_FILE.unlink(missing_ok=True)
        return False

class Database:
    """PostgreSQL database client with automatic SSH tunneling."""

    def __init__(
        self,
        db_host: str = DB_HOST,
        db_port: int = DB_PORT,
        db_name: str = DB_NAME,
        db_user: str | None = None,
        db_password: str | None = None,
    ):
        self.db_host = db_host
        self.db_port = db_port
        self.db_name = db_name
        self._db_user = db_user
        self._db_password = db_password
        self._tunnel: SSHTunnel | None = None
        self._conn: psycopg.Connection | None = None
        self._using_external_tunnel = False

    @property
    def db_user(self) -> str:
        return self._db_user or secret("RESHIFT_DB_USER")

    @property
    def db_password(self) -> str:
        return self._db_password or secret("RESHIFT_DB_PASSWORD")

    def _check_external_tunnel(self) -> bool:
        """Check if an external tunnel is already running on the default port."""
        try:
            with socket.create_connection(("127.0.0.1", SSHTunnel.DEFAULT_PORT), timeout=0.5):
                return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            return False

    def _start_tunnel(self) -> SSHTunnel:
        """Start SSH tunnel via gcloud."""
        if self._tunnel and self._tunnel.is_active:
            return self._tunnel

        self._tunnel = SSHTunnel(
            remote_host=self.db_host,
            remote_port=self.db_port,
        )
        self._tunnel.start()
        return self._tunnel

    def _stop_tunnel(self):
        """Stop SSH tunnel."""
        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None

    def connect(self) -> psycopg.Connection:
        """Get or create a database connection.

        Uses external tunnel if available, else starts one.
        """
        if self._conn is None or self._conn.closed:
            # Check for existing external tunnel first
            if self._check_external_tunnel():
                local_port = SSHTunnel.DEFAULT_PORT
                self._using_external_tunnel = True
            else:
                tunnel = self._start_tunnel()
                local_port = tunnel.local_bind_port
                self._using_external_tunnel = False

            dsn = (
                f"postgresql://{self.db_user}:{self.db_password}"
                f"@127.0.0.1:{local_port}/{self.db_name}"
            )
            self._conn = psycopg.connect(dsn, row_factory=dict_row)
        return self._conn

    def close(self):
        """Close database connection and SSH tunnel."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None
        self._stop_tunnel()

    @contextmanager
    def cursor(self):
        """Context manager for database cursor."""
        conn = self.connect()
        with conn.cursor() as cur:
            yield cur

    def query(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        """Execute a query and return all results as list of dicts."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def query_one(self, sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
        """Execute a query and return first result."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def execute(self, sql: str, params: tuple | dict | None = None) -> int:
        """Execute a statement and return rows affected."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    # Convenience methods for common queries

    def list_tables(self, schema: str = "public") -> list[str]:
        """List all tables in a schema."""
        rows = self.query(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema,),
        )
        return [r["table_name"] for r in rows]

    def describe_table(self, table: str, schema: str = "public") -> list[dict]:
        """Get column info for a table."""
        return self.query(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )

    def get_funds(self, limit: int = 100) -> list[dict]:
        """Get list of funds."""
        return self.query(
            'SELECT * FROM "Fund" ORDER BY name LIMIT %s',
            (limit,),
        )

    def get_assets(self, limit: int = 100) -> list[dict]:
        """Get list of assets."""
        return self.query(
            'SELECT * FROM "XAssetBase" ORDER BY name LIMIT %s',
            (limit,),
        )

    def get_asset_by_symbol(self, symbol: str) -> dict | None:
        """Get asset by symbol."""
        return self.query_one(
            'SELECT * FROM "XAssetBase" WHERE symbol = %s',
            (symbol,),
        )

    def get_daily_prices(
        self, asset_id: int, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        """Get daily prices for an asset."""
        sql = 'SELECT * FROM "XAssetDailyPrice" WHERE "assetId" = %s'
        params: list = [asset_id]

        if start_date:
            sql += " AND date >= %s"
            params.append(start_date)
        if end_date:
            sql += " AND date <= %s"
            params.append(end_date)

        sql += " ORDER BY date DESC"
        return self.query(sql, tuple(params))

    def get_transactions(self, limit: int = 100) -> list[dict]:
        """Get recent transactions."""
        return self.query(
            'SELECT * FROM "XTransactionBase" ORDER BY "createdAt" DESC LIMIT %s',
            (limit,),
        )

    # --- Organizations ---

    def get_organizations(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get organizations, optionally filtered by name."""
        if search:
            return self.query(
                'SELECT * FROM "Organization" WHERE name ILIKE %s '
                "ORDER BY name LIMIT %s",
                (f"%{search}%", limit),
            )
        return self.query(
            'SELECT * FROM "Organization" ORDER BY name LIMIT %s',
            (limit,),
        )

    def get_organization(self, org_id: str) -> dict | None:
        """Get a single organization by id."""
        return self.query_one(
            'SELECT * FROM "Organization" WHERE id = %s',
            (org_id,),
        )

    # --- People ---

    def get_people(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get people, optionally filtered by name."""
        if search:
            return self.query(
                'SELECT * FROM "Person" WHERE "fullName" ILIKE %s '
                'ORDER BY "lastName" LIMIT %s',
                (f"%{search}%", limit),
            )
        return self.query(
            'SELECT * FROM "Person" ORDER BY "lastName" LIMIT %s',
            (limit,),
        )

    def get_person(self, person_id: str) -> dict | None:
        """Get a single person by id."""
        return self.query_one(
            'SELECT * FROM "Person" WHERE id = %s',
            (person_id,),
        )

    # --- Portfolio Positions / Snapshots ---

    def get_positions(self, fund: str | None = None, limit: int = 100) -> list[dict]:
        """Get latest asset performance snapshots (positions with market values)."""
        if fund:
            return self.query(
                'SELECT s.* FROM "XAssetPerformanceSnapshot" s '
                'INNER JOIN "Fund" f ON s."fundId" = f.id '
                'WHERE f.name ILIKE %s '
                'ORDER BY s."eodDate" DESC, s."marketValue" DESC LIMIT %s',
                (f"%{fund}%", limit),
            )
        return self.query(
            'SELECT * FROM "XAssetPerformanceSnapshot" '
            'ORDER BY "eodDate" DESC, "marketValue" DESC LIMIT %s',
            (limit,),
        )

    # --- Events ---

    def get_events(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get hosted events, optionally filtered by name."""
        if search:
            return self.query(
                'SELECT * FROM "HostedEvent" WHERE name ILIKE %s '
                "ORDER BY timestamp DESC LIMIT %s",
                (f"%{search}%", limit),
            )
        return self.query(
            'SELECT * FROM "HostedEvent" ORDER BY timestamp DESC LIMIT %s',
            (limit,),
        )

    # --- Funding Rounds ---

    def get_funding_rounds(self, search: str | None = None, limit: int = 100) -> list[dict]:
        """Get equity financing rounds, optionally filtered by name."""
        if search:
            return self.query(
                'SELECT * FROM "EquityFinancingRound" WHERE name ILIKE %s '
                "ORDER BY date DESC LIMIT %s",
                (f"%{search}%", limit),
            )
        return self.query(
            'SELECT * FROM "EquityFinancingRound" ORDER BY date DESC LIMIT %s',
            (limit,),
        )

    # --- Equity Financing ---

    def get_equity_financing(self, limit: int = 100) -> list[dict]:
        """Get equity financing events."""
        return self.query(
            'SELECT * FROM "EquityFinancingEvent" ORDER BY "closingDate" DESC LIMIT %s',
            (limit,),
        )

    # --- Valuations ---

    def get_valuations(self, limit: int = 100) -> list[dict]:
        """Get organization valuations."""
        return self.query(
            'SELECT * FROM "OrganizationValuation" ORDER BY timestamp DESC LIMIT %s',
            (limit,),
        )

    # --- Corrections ---

    def get_corrections(self, limit: int = 100) -> list[dict]:
        """Get asset performance corrections."""
        return self.query(
            'SELECT * FROM "AssetPerformanceCorrection" ORDER BY "effectiveAt" DESC LIMIT %s',
            (limit,),
        )

    # --- Cash / Banking ---

    def get_cash_balances(self, limit: int = 100) -> list[dict]:
        """Get JPM bank cash balances."""
        return self.query(
            'SELECT * FROM "JpmBankCashBalance" ORDER BY "asOfDate" DESC LIMIT %s',
            (limit,),
        )

    def get_jpm_transactions(self, limit: int = 100) -> list[dict]:
        """Get JPM transactions."""
        return self.query(
            'SELECT * FROM "JPMTransaction" ORDER BY "settleDate" DESC LIMIT %s',
            (limit,),
        )

    def get_anchorage_balances(self, limit: int = 100) -> list[dict]:
        """Get Anchorage wallet balances."""
        return self.query(
            'SELECT * FROM "AnchorageWalletBalance" ORDER BY "updated_at" DESC LIMIT %s',
            (limit,),
        )

    def get_coinbase_balances(self, limit: int = 100) -> list[dict]:
        """Get Coinbase wallet balances."""
        return self.query(
            'SELECT * FROM "CoinbaseWalletBalance" ORDER BY "updated_at" DESC LIMIT %s',
            (limit,),
        )

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

# Singleton instance
_db: Database | None = None

def get_db() -> Database:
    """Get the singleton database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db
