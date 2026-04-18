#!/usr/bin/env python
"""Inspect symbol chunks in the index for a given installation.

Usage:
    uv run scripts/inspect-symbols.py <repo_name>
    uv run scripts/inspect-symbols.py svc-r12n
    uv run scripts/inspect-symbols.py svc-r12n --limit 50
"""
from __future__ import annotations

import argparse

import lancedb

from spark.config import SparkConfig
from spark.indexer.builder import TABLE_NAME


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect symbol chunks for a repo")
    parser.add_argument("repo", help="Installation name (e.g. svc-r12n)")
    parser.add_argument("--limit", type=int, default=30, help="Max symbols to show (default 30)")
    args = parser.parse_args()

    config = SparkConfig.load()
    db = lancedb.connect(str(config.index_path))
    table = db.open_table(TABLE_NAME)

    sym_count = table.count_rows(f'chunk_type = "symbol" AND installation = "{args.repo}"')
    fil_count = table.count_rows(f'chunk_type = "file" AND installation = "{args.repo}"')
    total = table.count_rows(f'installation = "{args.repo}"')

    print(f"\n{args.repo} — index breakdown")
    print(f"  symbol chunks : {sym_count}")
    print(f"  file chunks   : {fil_count}  (fallback: yaml/json/md/etc)")
    print(f"  total         : {total}")
    print(f"\n  symbol_chunking_enabled: {config.symbol_chunking_enabled}")

    if sym_count == 0:
        print("\n  No symbol chunks found. Run: spark activate", args.repo)
        return

    rows = (
        table.search()
        .where(f'chunk_type = "symbol" AND installation = "{args.repo}"')
        .select(["symbol_type", "symbol_name", "file_path"])
        .limit(args.limit)
        .to_list()
    )

    print(f"\n  Symbols (first {min(args.limit, sym_count)} of {sym_count}):")
    for r in rows:
        print(f"    {r['symbol_type']:12} {r['symbol_name']:35} {r['file_path']}")


if __name__ == "__main__":
    main()
