"""Trade alert copy — clear entry/exit Telegram templates."""

from __future__ import annotations

from decimal import Decimal

from algoflat.notifications.policy import is_important_alert
from algoflat.notifications.telegram import _format_alert
from algoflat.notifications.trade_alerts import (
    _option_label,
    format_trade_entry,
    format_trade_exit,
)
from algoflat.risk.engine import DailyRiskSnapshot


def _snap() -> DailyRiskSnapshot:
    return DailyRiskSnapshot(
        trade_date=__import__("datetime").date.today(),
        starting_capital=Decimal("20000"),
        available_capital=Decimal("8000"),
        deployed_capital=Decimal("12000"),
        realized_pnl=Decimal("500"),
        trade_count=2,
        consecutive_losses=0,
        kill_switch=False,
        entries_blocked=False,
    )


def test_option_label_parses_tsym():
    assert _option_label("NIFTY04AUG26C24350") == "CE 24350"
    assert _option_label("NIFTY04AUG26P24300") == "PE 24300"


def test_entry_alert_is_clear():
    alert = format_trade_entry(
        tsym="NIFTY04AUG26C24350",
        fill=Decimal("73.20"),
        quantity=195,
        setup_type="delta_momentum",
        snap=_snap(),
    )
    assert alert.title == "Entry · CE 24350"
    assert "Bought @" in alert.message
    assert "delta_momentum" in alert.message
    assert "Day P&L" in alert.message
    assert "BUY" not in alert.title


def test_exit_win_alert_is_clear():
    alert = format_trade_exit(
        tsym="NIFTY04AUG26C24350",
        fill=Decimal("93.95"),
        trade_pnl=Decimal("975"),
        quantity=195,
        exit_reason="momentum_trail",
        snap=_snap(),
    )
    assert alert.title == "Exit · WIN +₹975.00"
    assert "Sold @" in alert.message
    assert "momentum trail" in alert.message
    assert alert.severity == "info"


def test_exit_loss_alert():
    alert = format_trade_exit(
        tsym="NIFTY04AUG26P24300",
        fill=Decimal("150"),
        trade_pnl=Decimal("-400"),
        quantity=65,
        exit_reason="adverse_momentum",
        snap=_snap(),
    )
    assert "LOSS" in alert.title
    assert alert.severity == "warning"


def test_telegram_trade_format():
    text = _format_alert(
        title="Entry · CE 24350",
        message="PAPER · delta_momentum\nBought @ ₹73.20 × 195 qty",
        severity="info",
        type_="trade",
    )
    assert "🟢" in text
    assert "Entry · CE 24350" in text
    assert "BUY filled" not in text


def test_policy_matches_new_titles():
    assert is_important_alert("trade", "Entry · CE 24350")
    assert is_important_alert("trade", "Exit · WIN +₹100.00")
