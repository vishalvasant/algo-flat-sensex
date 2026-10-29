from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog

from algoflat.config import AppConfig
from algoflat.models.events import OptionState, QuoteUpdate

logger = structlog.get_logger(__name__)

GREEK_FIELDS = ("iv", "delta", "gamma", "theta", "vega")


class OptionDataLayer:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._states: dict[str, OptionState] = {}
        self._vwap_num: dict[str, Decimal] = {}
        self._vwap_den: dict[str, Decimal] = {}
        self._field_flags = {
            "ltp": True,
            "bid": True,
            "ask": True,
            "oi": True,
            "volume": True,
            **{field: False for field in GREEK_FIELDS},
        }
        mark_cfg = (config.runtime or {}).get("option_mark") or {}
        self._mark_enabled = bool(mark_cfg.get("enabled", True))
        self._prefer_mid = bool(mark_cfg.get("prefer_mid", True))
        self._ema_alpha = float(mark_cfg.get("ema_alpha", 0.35))
        self._ltp_ema: dict[str, Decimal] = {}

    @property
    def field_flags(self) -> dict[str, bool]:
        return dict(self._field_flags)

    def get(self, token: str) -> OptionState | None:
        return self._states.get(token)

    def all_states(self) -> list[OptionState]:
        return list(self._states.values())

    def mark_ltp(self, token: str) -> Decimal | None:
        """Noise-resistant premium for entry/exit decisions (mid or EMA, else LTP)."""
        state = self._states.get(token)
        if state is None:
            return None
        if not self._mark_enabled:
            return state.ltp
        if (
            self._prefer_mid
            and state.bid is not None
            and state.ask is not None
            and state.ask > state.bid
        ):
            return ((state.bid + state.ask) / Decimal("2")).quantize(Decimal("0.05"))
        if token in self._ltp_ema:
            return self._ltp_ema[token]
        return state.ltp

    def state_with_mark(self, token: str) -> OptionState | None:
        """Copy of OptionState with ltp replaced by stable mark (for scans/sizing)."""
        state = self.get(token)
        if state is None:
            return None
        mark = self.mark_ltp(token)
        if mark is None or mark == state.ltp:
            return state
        return state.model_copy(update={"ltp": mark})

    def update_from_quote(self, quote: QuoteUpdate) -> OptionState:
        state = self._states.get(quote.instrument_token) or OptionState(
            instrument_token=quote.instrument_token,
            tsym=quote.tsym or quote.instrument_token,
            field_flags=self.field_flags,
        )
        if quote.ltp is not None:
            state.ltp = quote.ltp
            # Session option VWAP — equal-weight tick VWAP (volume deltas unreliable).
            tok = quote.instrument_token
            self._vwap_num[tok] = self._vwap_num.get(tok, Decimal("0")) + quote.ltp
            self._vwap_den[tok] = self._vwap_den.get(tok, Decimal("0")) + Decimal("1")
            # EMA for decision marks when mid is unavailable.
            prev = self._ltp_ema.get(tok)
            alpha = Decimal(str(self._ema_alpha))
            if prev is None:
                self._ltp_ema[tok] = quote.ltp
            else:
                self._ltp_ema[tok] = (alpha * quote.ltp + (Decimal("1") - alpha) * prev).quantize(
                    Decimal("0.05")
                )
        if quote.bid is not None:
            state.bid = quote.bid
        if quote.ask is not None:
            state.ask = quote.ask
        if quote.volume is not None:
            state.volume = quote.volume
        if quote.oi is not None:
            state.oi = quote.oi
        if quote.broker_delta is not None:
            state.broker_delta = quote.broker_delta
        if quote.broker_gamma is not None:
            state.broker_gamma = quote.broker_gamma
        if quote.broker_theta is not None:
            state.broker_theta = quote.broker_theta
        if quote.broker_iv is not None:
            state.broker_iv = quote.broker_iv
        if state.bid and state.ask and state.ask > 0:
            state.spread_pct = ((state.ask - state.bid) / state.ask) * Decimal("100")
        state.last_update_ts = quote.ts
        state.quote_source = str(quote.source or "websocket")
        state.field_flags = self.field_flags
        self._states[quote.instrument_token] = state
        return state

    def option_vwap(self, token: str) -> float | None:
        den = self._vwap_den.get(token)
        num = self._vwap_num.get(token)
        if den is None or num is None or den <= 0:
            return None
        return float(num / den)

    def probe_greek_availability(self) -> dict[str, bool]:
        expected = self._config.data_availability.get("greek_fields_expected", False)
        if not expected:
            logger.info("greek_fields_degraded_mode", fields=GREEK_FIELDS)
        return {field: False for field in GREEK_FIELDS}
