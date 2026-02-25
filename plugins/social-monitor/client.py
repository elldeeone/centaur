"""Social monitor client — orchestrates scanning, classification, and digests."""

from __future__ import annotations

from .classifier import classify_unprocessed
from .db import (
    add_category as db_add_category,
)
from .db import (
    add_person as db_add_person,
)
from .db import (
    add_person_to_category,
    get_categories,
    get_db,
    get_people,
    import_people_csv,
)
from .db import (
    get_unnotified_signals as db_get_unnotified_signals,
)
from .digest import send_digest as digest_send
from .scanner import scan_all


class SocialMonitorClient:
    """Client for social feed monitoring and career signal detection."""

    def add_person(
        self,
        name: str,
        twitter: str | None = None,
        linkedin: str | None = None,
        company: str | None = None,
        role: str | None = None,
        category: str | None = None,
    ) -> int:
        """Add a person to track. Optionally assign to a category."""
        conn = get_db()
        try:
            person_id = db_add_person(
                conn,
                name=name,
                twitter_handle=twitter,
                linkedin_url=linkedin,
                company=company,
                role=role,
            )
            if category:
                row = conn.execute(
                    "SELECT id FROM categories WHERE name = ?", (category,)
                ).fetchone()
                if row:
                    add_person_to_category(conn, person_id, row["id"])
            return person_id
        finally:
            conn.close()

    def add_category(self, name: str, description: str | None = None) -> int:
        """Add a tracking category."""
        conn = get_db()
        try:
            return db_add_category(conn, name, description)
        finally:
            conn.close()

    def import_people(self, csv_path: str, category_name: str) -> int:
        """Import people from a CSV file into a category."""
        conn = get_db()
        try:
            return import_people_csv(conn, csv_path, category_name)
        finally:
            conn.close()

    def list_people(self, category: str | None = None) -> list[dict]:
        """List tracked people, optionally filtered by category."""
        conn = get_db()
        try:
            return get_people(conn, category=category)
        finally:
            conn.close()

    def list_categories(self) -> list[dict]:
        """List all categories."""
        conn = get_db()
        try:
            return get_categories(conn)
        finally:
            conn.close()

    def scan(self, limit_per_person: int = 20) -> int:
        """Run Twitter scan for all tracked people. Returns new post count."""
        conn = get_db()
        try:
            return scan_all(conn, limit_per_person=limit_per_person)
        finally:
            conn.close()

    def classify(self) -> int:
        """Classify unprocessed posts. Returns number of signals detected."""
        conn = get_db()
        try:
            return classify_unprocessed(conn)
        finally:
            conn.close()

    def get_signals(self, limit: int = 20, min_confidence: float = 0.5) -> list[dict]:
        """Get recent signals above confidence threshold."""
        conn = get_db()
        try:
            rows = conn.execute(
                """SELECT s.*, p.content AS post_content, p.post_url,
                          pe.name AS person_name, pe.twitter_handle, pe.company
                   FROM signals s
                   JOIN posts p ON s.post_id = p.id
                   JOIN people pe ON p.person_id = pe.id
                   WHERE s.signal_type != 'NONE' AND s.confidence >= ?
                   ORDER BY s.created_at DESC
                   LIMIT ?""",
                (min_confidence, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_unnotified_signals(self, min_confidence: float = 0.5) -> list[dict]:
        """Get signals that haven't been sent in a digest yet."""
        conn = get_db()
        try:
            return db_get_unnotified_signals(conn, min_confidence=min_confidence)
        finally:
            conn.close()

    def send_digest(self, channel: str | None = None) -> int:
        """Send digest to Slack. Returns number of signals sent."""
        conn = get_db()
        try:
            return digest_send(conn, channel=channel)
        finally:
            conn.close()

    def run_pipeline(self, limit: int = 20, channel: str | None = None) -> dict:
        """Run full pipeline: scan → classify → digest. Returns summary dict."""
        conn = get_db()
        try:
            new_posts = scan_all(conn, limit_per_person=limit)
            signals = classify_unprocessed(conn)
            sent = digest_send(conn, channel=channel)
            return {"new_posts": new_posts, "signals_detected": signals, "digest_sent": sent}
        finally:
            conn.close()

    def stats(self) -> dict:
        """Get database statistics."""
        conn = get_db()
        try:
            people_count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            posts_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            signals_count = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE signal_type != 'NONE'"
            ).fetchone()[0]

            categories = conn.execute(
                """SELECT c.name, COUNT(pc.person_id) AS count
                   FROM categories c
                   LEFT JOIN person_categories pc ON c.id = pc.category_id
                   GROUP BY c.id
                   ORDER BY c.name"""
            ).fetchall()

            return {
                "people": people_count,
                "posts": posts_count,
                "signals": signals_count,
                "categories": {row["name"]: row["count"] for row in categories},
            }
        finally:
            conn.close()


def _client() -> SocialMonitorClient:
    return SocialMonitorClient()
