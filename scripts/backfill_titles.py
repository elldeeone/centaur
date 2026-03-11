"""One-off: backfill AI-generated thread titles for all existing sessions."""

import asyncio
import os
import sys

import asyncpg
from openai import OpenAI

try:
    from centaur_sdk.tool_sdk import _sm_read
except ImportError:
    _sm_read = None  # type: ignore[assignment]


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key and _sm_read:
        openai_key = _sm_read("OPENAI_API_KEY")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)
    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)

    client = OpenAI(api_key=openai_key)
    conn = await asyncpg.connect(db_url)

    rows = await conn.fetch("""
        SELECT ss.thread_key, ss.thread_name, cm.parts
        FROM sandbox_sessions ss
        LEFT JOIN LATERAL (
            SELECT parts
            FROM chat_messages WHERE thread_key = ss.thread_key AND role = 'user'
            ORDER BY created_at ASC LIMIT 1
        ) cm ON true
        WHERE cm.parts IS NOT NULL
        ORDER BY ss.started_at DESC
    """)

    print(f"Found {len(rows)} sessions")
    updated = 0
    failed = 0

    for row in rows:
        key = row["thread_key"]
        parts = row["parts"] or []
        user_msg = ""
        if isinstance(parts, list) and parts:
            user_msg = (
                parts[0].get("text", "") if isinstance(parts[0], dict) else ""
            ).strip()
        if not user_msg:
            continue

        prompt = user_msg[:500]

        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-nano",
                max_tokens=30,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write a short human-readable title (3-6 words) for this AI agent conversation. "
                            "Use sentence case, no period. "
                            "Examples: 'Quantum computing landscape analysis', 'Fix Slack bot retry logic', "
                            "'Debug deploy pipeline errors', 'Crypto job trends research'. "
                            "Reply with ONLY the title, nothing else."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            title = (
                (resp.choices[0].message.content or "")
                .strip()
                .strip('"')
                .strip("'")
                .rstrip(".")
            )
            if not title:
                continue
            title = title[:60]

            await conn.execute(
                "UPDATE sandbox_sessions SET thread_name = $1, updated_at = NOW() WHERE thread_key = $2",
                title,
                key,
            )

            old = (row["thread_name"] or "")[:40]
            updated += 1
            print(f"  [{updated}] {old:<40} -> {title}")
        except Exception as e:
            print(f"  FAIL {key[:30]}: {e}")
            failed += 1

    await conn.close()
    print(f"\nDone: {updated} updated, {failed} failed")


if __name__ == "__main__":
    asyncio.run(main())
