"""Re-run RuleValidator on journal-stored candidate signals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from algoflat.config import AppConfig, get_config
from algoflat.models.events import CandidateSignal, OptionState
from algoflat.replay.loader import DayJournal, JournalSignal
from algoflat.validator.engine import RuleValidator

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class RevalidationRow:
    signal_id: str
    ts_ist: str
    setup_type: str
    side: str
    tsym: str
    live_passed: bool | None
    live_reasons: list[str]
    replay_passed: bool
    replay_reasons: list[str]
    agrees: bool
    live_validator_version: str | None
    replay_validator_version: str


@dataclass
class RevalidationReport:
    day: str
    underlying: str
    total: int
    comparable: int
    agrees: int
    disagreements: list[RevalidationRow] = field(default_factory=list)
    new_passes: list[RevalidationRow] = field(default_factory=list)
    new_fails: list[RevalidationRow] = field(default_factory=list)
  # Limitations noted in report
    limitations: list[str] = field(default_factory=list)

    @property
    def agreement_pct(self) -> float:
        if self.comparable == 0:
            return 0.0
        return self.agrees / self.comparable * 100.0


def _signal_to_candidate(row: JournalSignal) -> CandidateSignal:
    return CandidateSignal(
        id=row.id,
        ts=row.ts.astimezone(timezone.utc),
        setup_type=row.setup_type,
        side=row.side,
        instrument_token=row.instrument_token,
        tsym=row.tsym,
        strategy_version=row.strategy_version,
        feature_snapshot=row.feature_snapshot,
        scanner_metadata=dict(row.scanner_metadata),
        confidence=row.confidence,
    )


def _option_from_metadata(row: JournalSignal) -> OptionState | None:
    meta = row.scanner_metadata or {}
    ltp_raw = meta.get("option_ltp")
    if ltp_raw is None:
        return None
    try:
        ltp = Decimal(str(ltp_raw))
    except Exception:
        return None
    bid = meta.get("bid")
    ask = meta.get("ask")
    spread = meta.get("spread_pct")
    return OptionState(
        instrument_token=row.instrument_token,
        tsym=row.tsym,
        ltp=ltp,
        bid=Decimal(str(bid)) if bid is not None else ltp - Decimal("0.5"),
        ask=Decimal(str(ask)) if ask is not None else ltp + Decimal("0.5"),
        spread_pct=Decimal(str(spread)) if spread is not None else Decimal("1"),
        volume=meta.get("volume"),
        oi=meta.get("oi"),
    )


def revalidate_journal(
    journal: DayJournal,
    *,
    config: AppConfig | None = None,
    is_expiry_day: bool = False,
    option_m1: dict[str, list] | None = None,
    kill_switch: bool = False,
) -> RevalidationReport:
    """Re-run validator on stored signals; compare to journal validation_results."""
    cfg = config or get_config()
    validator = RuleValidator(cfg)

    limitations = [
        "premium_tracker not replayed — premium_responsive checks may drift",
        "session_loss_guard not replayed — loss-streak blocks may drift",
        "position/cooldown state not replayed — has_open/cooldown assumed false",
        "option_m1 defaults to None unless --with-option-snaps",
    ]

    rows: list[RevalidationRow] = []
    agrees = 0
    comparable = 0
    new_passes: list[RevalidationRow] = []
    new_fails: list[RevalidationRow] = []

    for sig in journal.signals:
        if sig.validator_passed is None:
            continue
        comparable += 1
        candidate = _signal_to_candidate(sig)
        option = _option_from_metadata(sig)
        when = sig.ts.astimezone(timezone.utc)
        feat_extra = sig.feature_snapshot.extra or {}
        regime_primary = feat_extra.get("regime_primary")

        o_m1 = None
        if option_m1 and sig.instrument_token in option_m1:
            o_m1 = option_m1[sig.instrument_token]

        result = validator.validate(
            candidate,
            option,
            has_open_for_token=False,
            in_cooldown=False,
            kill_switch=kill_switch,
            is_expiry_day=is_expiry_day,
            now=when,
            premium_tracker=None,
            regime_primary=regime_primary,
            option_m1=o_m1,
            spot_m1=None,
        )

        live_passed = bool(sig.validator_passed)
        replay_passed = result.passed
        agree = live_passed == replay_passed
        if agree:
            agrees += 1

        row = RevalidationRow(
            signal_id=str(sig.id),
            ts_ist=sig.ts.strftime("%Y-%m-%d %H:%M:%S"),
            setup_type=sig.setup_type,
            side=sig.side,
            tsym=sig.tsym,
            live_passed=live_passed,
            live_reasons=list(sig.rejection_reasons),
            replay_passed=replay_passed,
            replay_reasons=list(result.rejection_reasons),
            agrees=agree,
            live_validator_version=sig.validator_version,
            replay_validator_version=result.validator_version,
        )
        rows.append(row)
        if not agree:
            if replay_passed and not live_passed:
                new_passes.append(row)
            elif not replay_passed and live_passed:
                new_fails.append(row)

    disagreements = [r for r in rows if not r.agrees]

    return RevalidationReport(
        day=journal.day.isoformat(),
        underlying=journal.underlying,
        total=len(journal.signals),
        comparable=comparable,
        agrees=agrees,
        disagreements=disagreements,
        new_passes=new_passes,
        new_fails=new_fails,
        limitations=limitations,
    )
