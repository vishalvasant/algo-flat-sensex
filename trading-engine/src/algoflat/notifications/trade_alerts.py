"""Telegram / in-app copy for trade entry and exit alerts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from algoflat.broker.base import BrokerAdapter
from algoflat.broker.routed import RoutedBrokerAdapter
from algoflat.risk.engine import DailyRiskSnapshot, RiskEngine
from algoflat.runtime.trading_mode import get_execution_mode

_OPTION_TAIL = re.compile(r"([CP])(\d+)\s*$")


@dataclass(frozen=True)
class TradeAlert:
    """Title is the Telegram headline; message is the body (no duplicate labels)."""

    title: str
    message: str
    severity: str = "info"


def _inr(amount: Decimal) -> str:
    return f"₹{amount:,.2f}"


def _pnl_short(pnl: Decimal) -> str:
    if pnl > 0:
        return f"+{_inr(pnl)}"
    if pnl < 0:
        return f"−{_inr(abs(pnl))}"
    return _inr(Decimal("0"))


def _option_label(tsym: str) -> str:
    """NIFTY04AUG26C24350 → CE 24350."""
    m = _OPTION_TAIL.search(tsym.upper().replace(" ", ""))
    if not m:
        return tsym
    side = "CE" if m.group(1) == "C" else "PE"
    return f"{side} {int(m.group(2))}"


def _mode_tag() -> str:
    return get_execution_mode().upper()


def _account_footer(snap: DailyRiskSnapshot) -> str:
    day_pnl = snap.day_pnl_total if snap.day_pnl_total is not None else snap.realized_pnl
    return f"Day P&L {_pnl_short(day_pnl)} · Balance {_inr(snap.equity)}"


def _live_broker(broker: BrokerAdapter | None):
    if broker is None:
        return None
    if isinstance(broker, RoutedBrokerAdapter):
        return broker.live_broker
    return None


def format_trade_entry(
    *,
    tsym: str,
    fill: Decimal,
    quantity: int,
    setup_type: str | None,
    snap: DailyRiskSnapshot,
) -> TradeAlert:
    leg = _option_label(tsym)
    premium = (fill or Decimal("0")) * quantity
    setup = setup_type or "—"
    title = f"Entry · {leg}"
    message = "\n".join(
        [
            f"{_mode_tag()} · {setup}",
            f"Bought @ {_inr(fill)} × {quantity} qty ({_inr(premium)})",
            _account_footer(snap),
        ]
    )
    return TradeAlert(title=title, message=message, severity="info")


def format_trade_exit(
    *,
    tsym: str,
    fill: Decimal,
    trade_pnl: Decimal,
    quantity: int,
    exit_reason: str | None,
    snap: DailyRiskSnapshot,
) -> TradeAlert:
    leg = _option_label(tsym)
    won = trade_pnl >= 0
    status = "WIN" if won else "LOSS"
    title = f"Exit · {status} {_pnl_short(trade_pnl)}"
    reason = (exit_reason or "closed").replace("_", " ")
    message = "\n".join(
        [
            f"{_mode_tag()} · {leg} · {reason}",
            f"Sold @ {_inr(fill)} × {quantity} qty",
            _account_footer(snap),
        ]
    )
    return TradeAlert(
        title=title,
        message=message,
        severity="info" if won else "warning",
    )


async def build_trade_entry_message(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    quantity: int,
    setup_type: str | None = None,
    broker: BrokerAdapter | None = None,
) -> str:
    snap = await risk.snapshot_for_execution(live_broker=_live_broker(broker))
    return format_trade_entry(
        tsym=tsym,
        fill=fill,
        quantity=quantity,
        setup_type=setup_type,
        snap=snap,
    ).message


async def build_trade_entry_alert(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    quantity: int,
    setup_type: str | None = None,
    broker: BrokerAdapter | None = None,
) -> TradeAlert:
    snap = await risk.snapshot_for_execution(live_broker=_live_broker(broker))
    return format_trade_entry(
        tsym=tsym,
        fill=fill,
        quantity=quantity,
        setup_type=setup_type,
        snap=snap,
    )


async def build_trade_exit_message(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    trade_pnl: Decimal,
    quantity: int,
    exit_reason: str | None = None,
    broker: BrokerAdapter | None = None,
) -> str:
    snap = await risk.snapshot_for_execution(live_broker=_live_broker(broker))
    return format_trade_exit(
        tsym=tsym,
        fill=fill,
        trade_pnl=trade_pnl,
        quantity=quantity,
        exit_reason=exit_reason,
        snap=snap,
    ).message


async def build_trade_exit_alert(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    trade_pnl: Decimal,
    quantity: int,
    exit_reason: str | None = None,
    broker: BrokerAdapter | None = None,
) -> TradeAlert:
    snap = await risk.snapshot_for_execution(live_broker=_live_broker(broker))
    return format_trade_exit(
        tsym=tsym,
        fill=fill,
        trade_pnl=trade_pnl,
        quantity=quantity,
        exit_reason=exit_reason,
        snap=snap,
    )
