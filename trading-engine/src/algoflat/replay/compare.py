"""Compare journal replay decisions to live candidate_signals."""
from __future__ import annotations

from typing import Any


def _trade_key(tsym: str, side: str) -> str:
    return f"{tsym}|{side.upper()}"


def compare_signal_decisions(
    live_rows: list[dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    *,
    window_sec: float = 10.0,
) -> dict[str, Any]:
    """Match live vs replay signals by setup/side/tsym within time window."""
    matched: list[dict] = []
    live_only: list[dict] = []
    validator_mismatch: list[dict] = []
    used_replay: set[int] = set()

    for ls in live_rows:
        best_i: int | None = None
        best_dt = window_sec + 1
        for i, rd in enumerate(replay_rows):
            if i in used_replay:
                continue
            if ls.get("setup_type") != rd.get("setup_type"):
                continue
            if ls.get("side") != rd.get("side"):
                continue
            if ls.get("tsym") and rd.get("tsym") and ls["tsym"] != rd["tsym"]:
                continue
            dt = abs(float(ls["ts_epoch"]) - float(rd["ts_epoch"]))
            if dt <= window_sec and dt < best_dt:
                best_dt = dt
                best_i = i
        if best_i is None:
            live_only.append(ls)
            continue
        used_replay.add(best_i)
        rd = replay_rows[best_i]
        live_passed = bool(ls.get("validator_passed"))
        replay_passed = bool(rd.get("validator_passed"))
        row = {
            "setup_type": ls.get("setup_type"),
            "side": ls.get("side"),
            "tsym": ls.get("tsym"),
            "live_ts": ls.get("ts_ist"),
            "replay_ts": rd.get("ts_ist"),
            "delta_sec": round(abs(float(ls["ts_epoch"]) - float(rd["ts_epoch"])), 1),
            "live_validator_passed": live_passed,
            "replay_validator_passed": replay_passed,
            "live_rejections": ls.get("rejection_reasons") or [],
            "replay_rejections": rd.get("rejection_reasons") or [],
            "live_order_filled": ls.get("order_filled"),
            "replay_would_enter": rd.get("entered"),
        }
        matched.append(row)
        if live_passed != replay_passed:
            validator_mismatch.append(row)

    replay_only = [rd for i, rd in enumerate(replay_rows) if i not in used_replay]
    agree = sum(
        1 for m in matched if m["live_validator_passed"] == m["replay_validator_passed"]
    )
    val_agree_pct = (agree / len(matched) * 100) if matched else 0.0

    return {
        "matched": matched,
        "live_only": live_only,
        "replay_only": replay_only,
        "validator_mismatch": validator_mismatch,
        "matched_count": len(matched),
        "live_only_count": len(live_only),
        "replay_only_count": len(replay_only),
        "validator_agreement_pct": round(val_agree_pct, 1),
    }


def compare_closed_trades(
    live_rows: list[dict[str, Any]],
    replay_rows: list[dict[str, Any]],
    *,
    tolerance_sec: float = 120.0,
) -> dict[str, Any]:
    """Match live vs replay closed trades by tsym+side within entry time window."""
    matched: list[dict[str, Any]] = []
    live_only: list[dict[str, Any]] = []
    used_replay: set[int] = set()

    for lt in live_rows:
        best_i: int | None = None
        best_dt = tolerance_sec + 1
        for i, rt in enumerate(replay_rows):
            if i in used_replay:
                continue
            if _trade_key(lt.get("tsym", ""), lt.get("side", "")) != _trade_key(
                rt.get("tsym", ""), rt.get("side", "")
            ):
                continue
            dt = abs(float(lt["entry_epoch"]) - float(rt["entry_epoch"]))
            if dt <= tolerance_sec and dt < best_dt:
                best_dt = dt
                best_i = i
        if best_i is None:
            live_only.append(lt)
            continue
        used_replay.add(best_i)
        rt = replay_rows[best_i]
        live_pnl = float(lt.get("pnl") or 0)
        replay_pnl = float(rt.get("pnl") or 0)
        matched.append(
            {
                "tsym": lt.get("tsym"),
                "side": lt.get("side"),
                "setup_type": lt.get("setup_type"),
                "live_entry": lt.get("entry_ist"),
                "replay_entry": rt.get("entry_ist"),
                "entry_delta_sec": round(
                    abs(float(lt["entry_epoch"]) - float(rt["entry_epoch"])), 1
                ),
                "live_pnl": live_pnl,
                "replay_pnl": replay_pnl,
                "pnl_same_sign": (live_pnl >= 0) == (replay_pnl >= 0),
                "live_exit_reason": lt.get("exit_reason"),
                "replay_exit_reason": rt.get("exit_reason"),
                "live_lots": lt.get("lots"),
                "replay_lots": rt.get("lots"),
            }
        )

    replay_only = [rt for i, rt in enumerate(replay_rows) if i not in used_replay]
    live_count = len(live_rows)
    match_rate = (len(matched) / live_count * 100) if live_count else 0.0
    pnl_agree = sum(1 for m in matched if m["pnl_same_sign"])
    pnl_agree_pct = (pnl_agree / len(matched) * 100) if matched else 0.0

    return {
        "matched": matched,
        "live_only": live_only,
        "replay_only": replay_only,
        "matched_count": len(matched),
        "live_count": live_count,
        "replay_count": len(replay_rows),
        "live_only_count": len(live_only),
        "replay_only_count": len(replay_only),
        "match_rate_pct": round(match_rate, 1),
        "pnl_direction_agreement_pct": round(pnl_agree_pct, 1),
        "tolerance_sec": tolerance_sec,
    }
