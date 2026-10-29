"""Entry confirmation — require N consecutive scans before execute."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from algoflat.models.events import CandidateSignal

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class PendingEntry:
  setup_type: str
  side: str
  instrument_token: str
  tsym: str
  confidence: int
  streak: int = 1
  first_seen: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
  last_seen: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
  signal: CandidateSignal | None = None


class EntryConfirmationTracker:
  """Hold signals until they repeat for required_scans consecutive scans."""

  def __init__(
    self,
    *,
    required_scans: int = 2,
    max_wait_seconds: float = 60.0,
    enabled: bool = True,
    exempt_setups: frozenset[str] | None = None,
    open_window_exempt_setups: frozenset[str] | None = None,
    open_window_minutes: int = 15,
    high_confidence_bypass: int | None = 90,
    setup_overrides: dict[str, dict] | None = None,
    fast_confirm_enabled: bool = False,
    fast_confirm_apply_setups: frozenset[str] | None = None,
  ) -> None:
    self._required = max(1, required_scans)
    self._max_wait = max_wait_seconds
    self._enabled = enabled
    self._exempt = exempt_setups or frozenset()
    self._open_exempt = open_window_exempt_setups or frozenset()
    self._open_window_minutes = open_window_minutes
    self._high_conf_bypass = int(high_confidence_bypass) if high_confidence_bypass else None
    self._setup_overrides = setup_overrides or {}
    self._fast_confirm = fast_confirm_enabled
    self._fast_confirm_setups = fast_confirm_apply_setups or frozenset(
      {"expiry_scalping", "gap_and_go", "opening_range_breakout", "momentum_continuation"}
    )
    self._pending: dict[str, PendingEntry] = {}

  def required_scans_for(self, setup_type: str) -> int:
    override = self._setup_overrides.get(setup_type) or {}
    return max(1, int(override.get("required_scans", self._required)))

  def has_pending(self) -> bool:
    return bool(self._pending)

  @classmethod
  def from_config(cls, cfg: dict) -> EntryConfirmationTracker:
    return cls(
      enabled=bool(cfg.get("enabled", True)),
      required_scans=int(cfg.get("required_scans", 2)),
      max_wait_seconds=float(cfg.get("max_wait_seconds", 60)),
      exempt_setups=frozenset(cfg.get("exempt_setups") or ["trend_reversal_flip"]),
      open_window_exempt_setups=frozenset(
        cfg.get("open_window_exempt_setups") or ["gap_and_go", "opening_range_breakout"]
      ),
      open_window_minutes=int(cfg.get("open_window_minutes", 15)),
      high_confidence_bypass=cfg.get("high_confidence_bypass", 90),
      setup_overrides=cfg.get("setup_overrides") or {},
      fast_confirm_enabled=bool(cfg.get("fast_confirm_on_quote", False)),
      fast_confirm_apply_setups=frozenset(
        cfg.get("fast_confirm_apply_setups")
        or ["expiry_scalping", "gap_and_go", "opening_range_breakout", "momentum_continuation"]
      ),
    )

  def _key(self, signal: CandidateSignal) -> str:
    return f"{signal.setup_type}:{signal.instrument_token}:{signal.side}"

  def consider(
    self,
    signal: CandidateSignal | None,
    *,
    now: datetime | None = None,
    in_open_gap_window: bool = False,
  ) -> tuple[CandidateSignal | None, str | None]:
    """Return (signal_to_execute, wait_reason)."""
    now = now or datetime.now(tz=timezone.utc)
    self._prune(now)

    if not self._enabled:
      return signal, None

    if signal is None:
      self._pending.clear()
      return None, None

    if signal.setup_type in self._exempt:
      self._pending.clear()
      return signal, None

    conf = int(signal.confidence or 0)
    if self._high_conf_bypass is not None and conf >= self._high_conf_bypass:
      self._pending.clear()
      return signal, None

    if in_open_gap_window and signal.setup_type in self._open_exempt:
      self._pending.clear()
      return signal, None

    key = self._key(signal)
    required = self.required_scans_for(signal.setup_type)
    pending = self._pending.get(key)
    if pending is None:
      self._pending = {
        key: PendingEntry(
          setup_type=signal.setup_type,
          side=signal.side,
          instrument_token=signal.instrument_token,
          tsym=signal.tsym,
          confidence=int(signal.confidence or 0),
          first_seen=now,
          last_seen=now,
          signal=signal,
        )
      }
      if required <= 1:
        self._pending.pop(key, None)
        return signal, None
      return None, f"entry_confirm_wait scan=1/{required}"

    pending.streak += 1
    pending.last_seen = now
    pending.confidence = int(signal.confidence or pending.confidence)
    pending.signal = signal

    if pending.streak >= required:
      self._pending.pop(key, None)
      return signal, None

    return None, f"entry_confirm_wait scan={pending.streak}/{required}"

  def try_fast_confirm(
    self,
    instrument_token: str,
    *,
    premium_tracker,
    now: datetime | None = None,
  ) -> tuple[CandidateSignal | None, str | None]:
    """Quote-driven confirmation after candidate detected (§26A — not a full scan)."""
    if not self._fast_confirm or not self._pending:
      return None, None
    now = now or datetime.now(tz=timezone.utc)
    for key, pending in list(self._pending.items()):
      if pending.instrument_token != instrument_token:
        continue
      if pending.setup_type not in self._fast_confirm_setups:
        continue
      if pending.signal is None or premium_tracker is None:
        continue
      responsive = premium_tracker.is_responsive(
        instrument_token,
        pending.side,
        min_spot_pct=0.02,
        min_premium_pct=0.25,
        lookback_samples=3,
      )
      if responsive is not True:
        continue
      required = self.required_scans_for(pending.setup_type)
      if pending.streak < max(1, required - 1):
        continue
      self._pending.pop(key, None)
      sig = pending.signal
      meta = dict(sig.scanner_metadata or {})
      meta["confirmation_timing"] = {
        "candidate_first_seen": pending.first_seen.isoformat(),
        "fast_confirm_completed": now.isoformat(),
        "confirm_path": "fast_quote_premium",
        "scans_before_fast_confirm": pending.streak,
      }
      return sig.model_copy(update={"scanner_metadata": meta}), "fast_confirm_premium"
    return None, None

  def _prune(self, now: datetime) -> None:
    stale = [
      k
      for k, p in self._pending.items()
      if (now - p.first_seen).total_seconds() > self._max_wait
    ]
    for k in stale:
      self._pending.pop(k, None)

  def clear(self) -> None:
    self._pending.clear()


def in_open_gap_entry_window(
  now: datetime,
  *,
  entry_start: str = "09:30",
  open_window_minutes: int = 15,
) -> bool:
  """True during first N minutes after entry_start (IST) — fast gap/ORB entries."""
  ist = now.astimezone(IST)
  start_h, start_m = map(int, entry_start.split(":"))
  start = ist.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
  if ist < start:
    return False
  elapsed = (ist - start).total_seconds() / 60.0
  return elapsed < open_window_minutes
