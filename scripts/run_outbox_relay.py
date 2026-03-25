#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


async def _run(args: argparse.Namespace) -> None:
    from src.event_store import EventStore
    from src.outbox import OutboxRelay, PostgresOutboxSinkPublisher

    dsn = args.database_url or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required (or pass --database-url).")

    store = await EventStore.from_dsn(dsn, min_size=1, max_size=8)
    try:
        if args.apply_schema:
            await store.apply_schema(ROOT_DIR / "src" / "schema.sql")

        publisher = PostgresOutboxSinkPublisher(store)
        await publisher.ensure_schema()

        relay = OutboxRelay(
            store=store,
            publisher=publisher,
            batch_size=args.batch_size,
            max_attempts=args.max_attempts,
            retry_base_seconds=args.retry_base_seconds,
            retry_max_seconds=args.retry_max_seconds,
            claim_ttl_seconds=args.claim_ttl_seconds,
        )

        if args.once:
            result = await relay.run_once()
            print(
                json.dumps(
                    {
                        "claimed": result.claimed,
                        "published": result.published,
                        "failed": result.failed,
                        "dead_lettered": result.dead_lettered,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return

        await relay.run_forever(poll_interval=args.poll_interval)
    finally:
        await store.close()


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")

    parser = argparse.ArgumentParser(description="Run outbox relay worker.")
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL DSN. Falls back to DATABASE_URL env var.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one batch and exit.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Apply src/schema.sql before running.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds to sleep when no pending outbox rows are found.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Max outbox rows to claim per batch.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="Move message to dead_letter after this many attempts.",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=0.5,
        help="Base delay used for exponential retry backoff.",
    )
    parser.add_argument(
        "--retry-max-seconds",
        type=float,
        default=60.0,
        help="Max delay cap used for retry backoff.",
    )
    parser.add_argument(
        "--claim-ttl-seconds",
        type=float,
        default=30.0,
        help="How long a claimed message stays invisible to other workers.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
