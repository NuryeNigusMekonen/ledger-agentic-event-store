#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def main() -> None:
    from src.refinery.pipeline import DocumentRefineryPipeline

    parser = argparse.ArgumentParser(description="Run Week 3 Document Intelligence Refinery")
    parser.add_argument("document", type=Path, help="Path to input document")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(".refinery/facts.db"),
        help="SQLite database path for structured facts",
    )
    parser.add_argument(
        "--gemini-api-key",
        type=str,
        default=os.getenv("GEMINI_API_KEY"),
        help="Optional Gemini API key. Falls back to GEMINI_API_KEY env var.",
    )
    parser.add_argument(
        "--gemini-model",
        type=str,
        default=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        help="Gemini model name used by vision strategy when API key is present.",
    )
    args = parser.parse_args()

    pipeline = DocumentRefineryPipeline(
        sqlite_db_path=args.db,
        gemini_api_key=args.gemini_api_key,
        gemini_model=args.gemini_model,
    )
    result = pipeline.run(args.document)

    print(json.dumps(
        {
            "document_id": result.profile.document_id,
            "strategy": result.extracted.strategy_used,
            "confidence": result.extracted.confidence_score,
            "gemini_status": result.extracted.metadata.get("gemini_status", "unknown"),
            "chunks": len(result.chunks),
            "facts": result.facts_count,
            "page_index_children": len(result.page_index.root.child_sections),
        },
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
