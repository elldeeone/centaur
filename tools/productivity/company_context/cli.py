from __future__ import annotations

import argparse
import json
import sys

from client import _client


def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search indexed company context")
    subcommands = parser.add_subparsers(dest="command", required=True)

    search_parser = subcommands.add_parser("search", help="Search company context")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=8)
    search_parser.add_argument("--source")
    search_parser.add_argument("--source-type")
    search_parser.add_argument("--occurred-after")
    search_parser.add_argument("--occurred-before")

    list_parser = subcommands.add_parser("list-documents", help="List indexed documents")
    list_parser.add_argument("--limit", type=int, default=8)
    list_parser.add_argument("--source")
    list_parser.add_argument("--source-type")
    list_parser.add_argument("--occurred-after")
    list_parser.add_argument("--occurred-before")

    read_parser = subcommands.add_parser("read-document", help="Read one indexed document")
    read_parser.add_argument("document_id")
    read_parser.add_argument("--max-chars", type=int, default=0)
    read_parser.add_argument("--include-related", action="store_true")
    read_parser.add_argument("--max-related-children", type=int, default=12)

    latest_parser = subcommands.add_parser("latest-date", help="Show latest indexed date")
    latest_parser.add_argument("--source")
    latest_parser.add_argument("--source-type")

    args = parser.parse_args(argv)
    client = _client()

    if args.command == "search":
        emit(
            client.search(
                args.query,
                limit=args.limit,
                source=args.source,
                source_type=args.source_type,
                occurred_after=args.occurred_after,
                occurred_before=args.occurred_before,
            )
        )
    elif args.command == "list-documents":
        emit(
            client.list_documents(
                limit=args.limit,
                source=args.source,
                source_type=args.source_type,
                occurred_after=args.occurred_after,
                occurred_before=args.occurred_before,
            )
        )
    elif args.command == "read-document":
        emit(
            client.read_document(
                args.document_id,
                max_chars=args.max_chars,
                include_related=args.include_related,
                max_related_children=args.max_related_children,
            )
        )
    elif args.command == "latest-date":
        emit(client.latest_date(source=args.source, source_type=args.source_type))
    else:  # pragma: no cover - argparse enforces known commands.
        parser.error(f"unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
