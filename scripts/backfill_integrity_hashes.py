#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repair missing or invalid event integrity metadata (previous_hash/integrity_hash). "
            "Defaults to dry-run for safety."
        )
    )
    parser.add_argument("--stream-id", help="Repair one exact stream_id.", default=None)
    parser.add_argument(
        "--stream-prefix",
        help="Repair streams matching this prefix (LIKE '<prefix>%%').",
        default=None,
    )
    parser.add_argument(
        "--mode",
        choices=("missing", "missing_or_invalid"),
        default="missing",
        help=(
            "missing: only fill missing hash metadata (safe default). "
            "missing_or_invalid: rewrite missing and mismatched hash metadata."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply repair changes. If omitted, runs dry-run only.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL. If omitted, loaded from environment/.env.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output only.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    from src.event_store import EventStore

    load_dotenv(PROJECT_ROOT / ".env")
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print(
            "DATABASE_URL is not set. Define it in environment or pass --database-url.",
            file=sys.stderr,
        )
        return 2

    store = await EventStore.from_dsn(database_url, min_size=1, max_size=5, connect_timeout=5.0)
    try:
        result = await store.backfill_integrity_hashes(
            stream_id=args.stream_id,
            stream_prefix=args.stream_prefix,
            mode=args.mode,
            dry_run=not args.apply,
        )
    finally:
        await store.close()

    payload = asdict(result)
    payload["applied"] = bool(args.apply)

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print("Integrity backfill summary:")
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not args.apply:
            print("Dry-run only. Re-run with --apply to persist changes.")

    if result.unresolved_violations > 0:
        return 1
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
