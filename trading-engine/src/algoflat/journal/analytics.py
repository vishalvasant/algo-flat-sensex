"""Performance analytics + strategy priority learning loop (§15)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class StrategyStats:
  trades: int = 0
  wins: int = 0
  losses: int = 0
  pnl: float = 0.0
  recent: list[float] = field(default_factory=list)  # last N trade pnls

  @property
  def win_rate(self) -> float:
    return (self.wins / self.trades) if self.trades else 0.0

  @property
  def expectancy(self) -> float:
    return (self.pnl / self.trades) if self.trades else 0.0


class StrategyLearner:
  """Reduce priority of strategies with sustained underperformance."""

  def __init__(
    self,
    path: Path | None = None,
    *,
    lookback: int = 20,
    demote_after_losses: int = 5,
    demote_multiplier: float = 0.75,
    promote_floor: float = 0.55,
    pause_after_session_losses: int = 4,
    pause_rest_of_session: bool = True,
    min_trades_before_demote: int = 8,
  ) -> None:
    self._path = path
    self._lookback = lookback
    self._demote_after = demote_after_losses
    self._demote_mult = demote_multiplier
    self._promote_floor = promote_floor
    self._pause_after_session = max(1, pause_after_session_losses)
    self._pause_rest_of_session = pause_rest_of_session
    self._min_trades_demote = max(1, min_trades_before_demote)
    self._stats: dict[str, StrategyStats] = {}
    self._multipliers: dict[str, float] = {}
    self._session_stats: dict[str, StrategyStats] = {}
    self._session_paused: set[str] = set()
    self._session_date: date | None = None
    if path and path.exists():
      self._load(path)

  @classmethod
  def from_config(cls, path: Path | None, config: dict) -> StrategyLearner:
    cfg = config.get("strategy_learner") or {}
    return cls(
      path,
      lookback=int(cfg.get("lookback", 20)),
      demote_after_losses=int(cfg.get("demote_after_losses", 5)),
      demote_multiplier=float(cfg.get("demote_multiplier", 0.75)),
      promote_floor=float(cfg.get("promote_floor", 0.55)),
      pause_after_session_losses=int(cfg.get("pause_after_session_losses", 4)),
      pause_rest_of_session=bool(cfg.get("pause_rest_of_session", True)),
      min_trades_before_demote=int(cfg.get("min_trades_before_demote", 8)),
    )

  def begin_session(self, session_date: date | None = None) -> None:
    """Reset per-session pause state (call at market open / backtest day start)."""
    today = session_date or datetime.now(tz=timezone.utc).date()
    if self._session_date == today:
      return
    self._session_date = today
    self._session_stats.clear()
    self._session_paused.clear()

  def is_paused(self, setup_type: str) -> bool:
    return setup_type in self._session_paused

  def record_trade(
    self,
    setup_type: str,
    pnl: Decimal | float,
    *,
    confidence: int | None = None,
    exit_reason: str | None = None,
  ) -> None:
    self.begin_session()
    st = self._stats.setdefault(setup_type, StrategyStats())
    sess = self._session_stats.setdefault(setup_type, StrategyStats())
    px = float(pnl)
    st.trades += 1
    sess.trades += 1
    st.pnl += px
    sess.pnl += px
    if px > 0:
      st.wins += 1
      sess.wins += 1
    else:
      st.losses += 1
      sess.losses += 1
    st.recent.append(px)
    if len(st.recent) > self._lookback:
      st.recent = st.recent[-self._lookback :]
    sess.recent.append(px)
    if len(sess.recent) > self._pause_after_session:
      sess.recent = sess.recent[-self._pause_after_session :]
    self._recompute(setup_type)
    self._recompute_session_pause(setup_type)
    logger.info(
      "strategy_learner_recorded",
      setup=setup_type,
      pnl=px,
      mult=self._multipliers.get(setup_type, 1.0),
      win_rate=round(st.win_rate, 3),
      confidence=confidence,
      exit_reason=exit_reason,
    )
    self._persist()

  def priority_multiplier(self, setup_type: str) -> float:
    return self._multipliers.get(setup_type, 1.0)

  def adjusted_confidence(self, setup_type: str, confidence: int) -> int:
    return int(round(confidence * self.priority_multiplier(setup_type)))

  def snapshot(self) -> dict[str, Any]:
    return {
      "multipliers": dict(self._multipliers),
      "session_paused": sorted(self._session_paused),
      "stats": {
        k: {
          "trades": v.trades,
          "wins": v.wins,
          "losses": v.losses,
          "pnl": round(v.pnl, 2),
          "win_rate": round(v.win_rate, 3),
          "expectancy": round(v.expectancy, 2),
        }
        for k, v in self._stats.items()
      },
      "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }

  def _recompute(self, setup_type: str) -> None:
    st = self._stats[setup_type]
    recent = st.recent[-self._demote_after :]
    if len(recent) >= self._demote_after and all(x <= 0 for x in recent):
      if st.trades >= self._min_trades_demote:
        self._multipliers[setup_type] = self._demote_mult
      return
    if st.trades >= self._min_trades_demote and st.win_rate < self._promote_floor and st.expectancy < 0:
      self._multipliers[setup_type] = min(
        self._multipliers.get(setup_type, 1.0), self._demote_mult
      )
      return
    if st.trades >= 5 and st.expectancy > 0 and st.win_rate >= 0.5:
      self._multipliers[setup_type] = 1.0
      return
    self._multipliers.setdefault(setup_type, 1.0)

  def _recompute_session_pause(self, setup_type: str) -> None:
    if not self._pause_rest_of_session:
      return
    sess = self._session_stats.get(setup_type)
    if sess is None:
      return
    recent = sess.recent[-self._pause_after_session :]
    if len(recent) >= self._pause_after_session and all(x <= 0 for x in recent):
      self._session_paused.add(setup_type)
      logger.warning(
        "strategy_paused_for_session",
        setup=setup_type,
        losses=self._pause_after_session,
      )

  def _persist(self) -> None:
    if self._path is None:
      return
    try:
      self._path.parent.mkdir(parents=True, exist_ok=True)
      self._path.write_text(json.dumps(self.snapshot(), indent=2))
    except Exception:
      logger.exception("strategy_learner_persist_failed")

  def _load(self, path: Path) -> None:
    try:
      data = json.loads(path.read_text())
      self._multipliers = {
        k: float(v) for k, v in (data.get("multipliers") or {}).items()
      }
      for k, v in (data.get("stats") or {}).items():
        self._stats[k] = StrategyStats(
          trades=int(v.get("trades", 0)),
          wins=int(v.get("wins", 0)),
          losses=int(v.get("losses", 0)),
          pnl=float(v.get("pnl", 0)),
        )
    except Exception:
      logger.exception("strategy_learner_load_failed")
