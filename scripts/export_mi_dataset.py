#!/usr/bin/env python3
"""Phase 12 — Export MI candidate logs from journal for offline training."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg


async def export_candidates(
  *,
  database_url: str,
  out_path: Path,
  limit: int = 5000,
) -> int:
  pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
  rows: list[dict] = []
  try:
    async with pool.acquire() as conn:
      records = await conn.fetch(
        """
        SELECT ts, metadata
        FROM system_events
        WHERE event_type = 'mi_candidate_log'
        ORDER BY ts DESC
        LIMIT $1
        """,
        limit,
      )
      for row in records:
        meta = row["metadata"]
        if isinstance(meta, str):
          meta = json.loads(meta)
        payload = dict(meta or {})
        payload["exported_at"] = datetime.now(tz=timezone.utc).isoformat()
        payload["event_ts"] = row["ts"].isoformat() if row["ts"] else None
        rows.append(payload)
  finally:
    await pool.close()

  out_path.parent.mkdir(parents=True, exist_ok=True)
  with out_path.open("w", encoding="utf-8") as handle:
    for item in reversed(rows):
      handle.write(json.dumps(item, default=str) + "\n")
  return len(rows)


def main() -> None:
  parser = argparse.ArgumentParser(description="Export MI candidate dataset (JSONL)")
  parser.add_argument(
    "--database-url",
    default=os.environ.get(
      "DATABASE_URL", "postgresql://algoflat:algoflat@localhost:5432/algoflat"
    ),
  )
  parser.add_argument(
    "--out",
    default=str(Path(__file__).resolve().parents[1] / "reports" / "mi_candidates.jsonl"),
  )
  parser.add_argument("--limit", type=int, default=5000)
  args = parser.parse_args()

  count = asyncio.run(
    export_candidates(
      database_url=args.database_url,
      out_path=Path(args.out),
      limit=args.limit,
    )
  )
  print(f"Exported {count} MI candidate rows to {args.out}")


if __name__ == "__main__":
  main()
