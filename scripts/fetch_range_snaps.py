#!/usr/bin/env python3
"""Fetch day snaps for a date range from Flattrade (spot + optional options).

Writes manifest first, then fetches missing/incomplete snaps one session per call.

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/fetch_range_snaps.py \\
    --start 2026-05-01 --end 2026-07-31 --underlying NIFTY

  # Manifest only (no API calls):
  trading-engine/.venv/bin/python scripts/fetch_range_snaps.py \\
    --start 2026-05-01 --end 2026-07-31 --manifest-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("STRUCTLOG_LEVEL", "WARNING")

from algoflat.contract_selector.expiry import (  # noqa: E402
    format_expiry_tag,
    next_weekly_expiry_dates,
)

REPORT_DIR = ROOT / "reports"
MANIFEST_DIR = REPORT_DIR / "fetch_manifests"


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _expected_expiry(d: date, underlying: str = "NIFTY") -> str:
    cal = next_weekly_expiry_dates(
        underlying=underlying, as_of=d, count=1, include_today_if_expiry=True
    )
    return format_expiry_tag(cal[0]) if cal else "?"


def _snap_status(day: date, underlying: str) -> dict[str, Any]:
    meta_p = ROOT / "reports" / "snaps" / day.isoformat() / underlying / "meta.json"
    expected = _expected_expiry(day, underlying)
    if not meta_p.exists():
        return {
            "date": day.isoformat(),
            "weekday": day.strftime("%a"),
            "expected_expiry": expected,
            "status": "MISSING",
            "spot_1m": 0,
            "option_tokens": 0,
            "universe_instruments": 0,
            "snap_expiry": "",
        }
    meta = json.loads(meta_p.read_text())
    spot_1m = int((meta.get("spot_bars") or {}).get("1m", 0))
    opts = int(meta.get("option_tokens", 0))
    inst = int(meta.get("universe_instruments", 0))
    snap_exp = str(meta.get("expiry_symbol", ""))
    exp_ok = snap_exp == expected
    if spot_1m < 30:
        status = "INCOMPLETE"
    elif not exp_ok:
        status = "EXPIRY_MISMATCH"
    elif opts == 0:
        status = "SPOT_ONLY"
    else:
        status = "OK"
    return {
        "date": day.isoformat(),
        "weekday": day.strftime("%a"),
        "expected_expiry": expected,
        "status": status,
        "spot_1m": spot_1m,
        "option_tokens": opts,
        "universe_instruments": inst,
        "snap_expiry": snap_exp,
    }


def build_manifest(
    start: date,
    end: date,
    *,
    underlying: str = "NIFTY",
) -> dict[str, Any]:
    days = _weekdays(start, end)
    rows = [_snap_status(d, underlying) for d in days]
    need_fetch = [
        r for r in rows
        if r["status"] in ("MISSING", "INCOMPLETE", "EXPIRY_MISMATCH")
    ]
    return {
        "underlying": underlying,
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "total_weekdays": len(days),
        "need_fetch": len(need_fetch),
        "by_status": {
            s: sum(1 for r in rows if r["status"] == s)
            for s in sorted({r["status"] for r in rows})
        },
        "days": rows,
        "fetch_queue": [r["date"] for r in need_fetch],
        "notes": [
            "NIFTY weekly expiry = Tuesday (Mon/Tue → expiring series; Wed+ → next Tue).",
            "Flattrade retains ~90d of 1m history; May 1–8 may be unavailable.",
            "Expired weekly options are not in scripmaster — snaps use BS premiums.",
            "May sessions often use NSE index 26000 when historical fut token has no bars.",
        ],
    }


def write_manifest(manifest: dict[str, Any]) -> Path:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"fetch_{manifest['underlying']}_{manifest['range_start']}_{manifest['range_end']}"
    json_path = MANIFEST_DIR / f"{stem}.json"
    md_path = MANIFEST_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(manifest, indent=2))

    lines = [
        f"# Fetch manifest — {manifest['underlying']} "
        f"({manifest['range_start']} → {manifest['range_end']})",
        "",
        f"- Weekdays: **{manifest['total_weekdays']}**",
        f"- Need fetch: **{manifest['need_fetch']}**",
        f"- Status: {manifest['by_status']}",
        "",
        "| Date | DOW | Expected expiry | Status | Spot 1m | Options | Universe | Snap expiry |",
        "|------|-----|-----------------|--------|--------:|--------:|---------:|-------------|",
    ]
    for r in manifest["days"]:
        lines.append(
            f"| {r['date']} | {r['weekday']} | {r['expected_expiry']} | "
            f"{r['status']} | {r['spot_1m']} | {r['option_tokens']} | "
            f"{r['universe_instruments']} | {r['snap_expiry']} |"
        )
    lines.extend(["", "## Notes", ""])
    for n in manifest.get("notes", []):
        lines.append(f"- {n}")
    md_path.write_text("\n".join(lines) + "\n")
    return json_path


async def _fetch_day(day: date, underlying: str) -> bool:
    from day_backtest import run_backtest  # noqa: E402

    print(f"\n{'=' * 72}\nFETCH {day.isoformat()} expected={_expected_expiry(day, underlying)}\n{'=' * 72}")
    try:
        await run_backtest(
            day,
            underlying=underlying,
            refresh_snap=True,
            snap_only=True,
        )
    except Exception as exc:
        print(f"  FETCH FAILED {day}: {exc}")
        return False
    st = _snap_status(day, underlying)
    ok = st["status"] in ("OK", "SPOT_ONLY") and st["spot_1m"] >= 30
    print(f"  Result: {st['status']} spot_1m={st['spot_1m']} expiry={st['snap_expiry']}")
    return ok


async def run_fetch(
    start: date,
    end: date,
    *,
    underlying: str,
    manifest_only: bool,
    force: bool,
) -> None:
    manifest = build_manifest(start, end, underlying=underlying)
    path = write_manifest(manifest)
    print(f"Manifest: {path}")
    print(f"Fetch queue: {manifest['need_fetch']} days")
    if manifest_only:
        return

    queue = manifest["fetch_queue"]
    if force:
        queue = [r["date"] for r in manifest["days"]]

    ok = 0
    fail = 0
    for raw in queue:
        day = date.fromisoformat(raw)
        if await _fetch_day(day, underlying):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.5)
    print(f"\nFetch complete: ok={ok} fail={fail}")
    final = build_manifest(start, end, underlying=underlying)
    write_manifest(final)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Flattrade day snaps for backtest")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch all days in range (not only missing)",
    )
    args = parser.parse_args()
    asyncio.run(
        run_fetch(
            date.fromisoformat(args.start),
            date.fromisoformat(args.end),
            underlying=args.underlying.upper(),
            manifest_only=args.manifest_only,
            force=args.force,
        )
    )


if __name__ == "__main__":
    main()
