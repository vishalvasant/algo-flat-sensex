from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.config import AppConfig
from algoflat.contract_selector.expiry import (
  chain_anchor_symbol,
  format_expiry_tag,
  nearest_weekly_expiry_tag,
  next_weekly_expiry_dates,
  parse_expiry_tag,
  weekly_expiry_candidates,
)
from algoflat.contract_selector.scripmaster import load_weekly_band_from_scripmaster
from algoflat.models.events import Instrument

logger = structlog.get_logger(__name__)


@dataclass
class ContractUniverse:
    spot: Decimal
    atm_strike: Decimal
    expiry_symbol: str | None = None
    instruments: list[Instrument] = field(default_factory=list)
    atm_ce: Instrument | None = None
    atm_pe: Instrument | None = None
    subscription_keys: list[str] = field(default_factory=list)


class ContractSelector:
    def __init__(self, config: AppConfig, broker: BrokerAdapter) -> None:
        self._config = config
        self._broker = broker

    def atm_strike_for_spot(
        self,
        spot: Decimal,
        strike_step: Decimal | float | None = None,
    ) -> Decimal:
        step = Decimal(
            str(strike_step if strike_step is not None else self._config.symbols["strike_step"])
        )
        return (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

    def retarget_atm(
        self,
        universe: ContractUniverse,
        spot: Decimal,
        strike_step: Decimal | float | None = None,
    ) -> ContractUniverse:
        """Re-point ATM CE/PE from an already-loaded band when spot moves.

        Avoids a full broker chain refresh every scan — required so live
        multi-position behaviour matches day backtest (new ATM as spot drifts).
        """
        atm = self.atm_strike_for_spot(spot, strike_step=strike_step)
        if not universe.instruments:
            return ContractUniverse(
                spot=spot,
                atm_strike=atm,
                expiry_symbol=universe.expiry_symbol,
                instruments=[],
                atm_ce=None,
                atm_pe=None,
                subscription_keys=universe.subscription_keys,
            )
        atm_ce = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "CE"),
            None,
        )
        atm_pe = next(
            (i for i in universe.instruments if i.strike == atm and i.option_type == "PE"),
            None,
        )
        # Fallback nearest in-band if exact ATM missing
        if atm_ce is None:
            ces = [i for i in universe.instruments if i.option_type == "CE"]
            atm_ce = min(ces, key=lambda i: abs(i.strike - atm), default=None)
        if atm_pe is None:
            pes = [i for i in universe.instruments if i.option_type == "PE"]
            atm_pe = min(pes, key=lambda i: abs(i.strike - atm), default=None)
        return ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=universe.expiry_symbol,
            instruments=universe.instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=universe.subscription_keys,
        )

    def strike_in_band(self, strike: Decimal, atm: Decimal) -> bool:
        band = Decimal(str(self._config.symbols["strike_band_points"]))
        step = Decimal(str(self._config.symbols["strike_step"]))
        if abs(strike - atm) > band:
            return False
        # Keep only exact strike-step multiples from ATM (no stray strikes).
        diff = abs(strike - atm)
        return (diff % step) == 0

    @staticmethod
    def _expected_strike_count(band_steps: int) -> int:
        return band_steps * 2 + 1

    def _finalize_universe(
        self,
        *,
        spot: Decimal,
        atm: Decimal,
        expiry_tag: str,
        instruments: list[Instrument],
    ) -> ContractUniverse:
        atm_ce = next((i for i in instruments if i.strike == atm and i.option_type == "CE"), None)
        atm_pe = next((i for i in instruments if i.strike == atm and i.option_type == "PE"), None)
        spot_exchange = self._config.symbols["exchange_spot"]
        spot_token = self._config.symbols["spot_token"]
        keys = [BrokerAdapter.format_instrument(spot_exchange, spot_token)]
        keys.extend(BrokerAdapter.format_instrument(i.exchange, i.token) for i in instruments)
        return ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=expiry_tag,
            instruments=instruments,
            atm_ce=atm_ce,
            atm_pe=atm_pe,
            subscription_keys=keys,
        )

    def _supplement_from_scripmaster(
        self,
        universe: ContractUniverse,
        *,
        atm: Decimal,
        underlying: str,
        exchange: str,
        expiry_tag: str,
        band_steps: int,
    ) -> ContractUniverse:
        """Fill ATM±band when broker option chain returns fewer strikes than expected."""
        unique_strikes = {i.strike for i in universe.instruments}
        expected = self._expected_strike_count(band_steps)
        if len(unique_strikes) >= expected:
            return universe

        band_points = Decimal(str(self._config.symbols["strike_band_points"]))
        try:
            supplemental = load_weekly_band_from_scripmaster(
                underlying=underlying,
                expiry_tag=expiry_tag,
                atm=atm,
                band_points=band_points,
                exchange=exchange,
            )
        except Exception:
            logger.exception("scripmaster_supplement_failed", underlying=underlying, expiry=expiry_tag)
            return universe

        if not supplemental:
            return universe

        by_token = {i.token: i for i in universe.instruments}
        for inst in supplemental:
            by_token.setdefault(inst.token, inst)

        merged = [
            i
            for i in by_token.values()
            if self.strike_in_band(i.strike, atm)
        ]
        merged.sort(key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1))
        if len({i.strike for i in merged}) <= len(unique_strikes):
            return universe

        logger.info(
            "contract_universe_supplemented",
            underlying=underlying,
            expiry=expiry_tag,
            broker_strikes=len(unique_strikes),
            merged_strikes=len({i.strike for i in merged}),
            instrument_count=len(merged),
        )
        return self._finalize_universe(
            spot=universe.spot,
            atm=atm,
            expiry_tag=expiry_tag,
            instruments=merged,
        )

    async def build_universe(self, spot: Decimal, *, as_of: date | None = None) -> ContractUniverse:
        atm = self.atm_strike_for_spot(spot)
        exchange = self._config.symbols["exchange_options"]
        underlying = self._config.symbols["underlying"]
        band_steps = int(self._config.symbols.get("atm_strike_steps", 5))
        band = band_steps

        search_rows = await self._broker.search_scrip(exchange, underlying)
        session_day = as_of
        include_today = None
        if session_day is not None:
            # Historical replay: include session-day expiry (Tue weekly) like live morning.
            include_today = True
            cal = next_weekly_expiry_dates(
                underlying=underlying,
                as_of=session_day,
                count=3,
                include_today_if_expiry=True,
            )
            primary = format_expiry_tag(cal[0]) if cal else None
        else:
            primary = None
        candidates = weekly_expiry_candidates(
            search_rows,
            underlying=underlying,
            as_of=session_day,
            include_today=include_today,
            limit=4,
        )
        # Broker search often returns futures only. Prefer listed OPTIDX expiries from the
        # contract master so a holiday-shifted weekly (Tue→Mon) is used instead of a
        # calendar Tuesday that has no chain, which would skip to the next monthly.
        try:
            from algoflat.contract_selector.scripmaster import option_expiry_tags_from_scripmaster

            master_tags = option_expiry_tags_from_scripmaster(
                underlying=underlying,
                as_of=session_day,
                include_today=True if include_today is None else include_today,
                limit=4,
                exchange=exchange,
            )
        except Exception:
            logger.exception("scripmaster_expiry_tags_failed", underlying=underlying)
            master_tags = []
        if master_tags:
            candidates = master_tags + [c for c in candidates if c not in master_tags]
        if session_day is not None:
            # Historical: nearest listed weekly for that session (handles holiday shifts).
            candidates = (master_tags[:1] if master_tags else ([primary] if primary else []))
        elif primary:
            candidates = [primary] + [c for c in candidates if c != primary]
        if not candidates:
            # Last-resort single calendar guess
            fallback = nearest_weekly_expiry_tag(
                search_rows,
                underlying=underlying,
                as_of=session_day,
                include_today=include_today,
            )
            candidates = [fallback] if fallback else []

        if not candidates:
            logger.warning("weekly_expiry_not_found", underlying=underlying)
            return ContractUniverse(spot=spot, atm_strike=atm)

        logger.info("weekly_expiry_candidates", candidates=candidates)

        for expiry_tag in candidates:
            universe = await self._build_for_expiry(
                spot=spot,
                atm=atm,
                exchange=exchange,
                underlying=underlying,
                band=band,
                expiry_tag=expiry_tag,
            )
            if universe.instruments:
                return universe
            logger.warning(
                "weekly_expiry_chain_empty",
                expiry=expiry_tag,
                anchor=chain_anchor_symbol(underlying, expiry_tag, int(atm), "CE"),
            )

        # After-hours / empty Pi API: load from Flattrade public scripmaster CDN.
        for expiry_tag in candidates:
            try:
                instruments = load_weekly_band_from_scripmaster(
                    underlying=underlying,
                    expiry_tag=expiry_tag,
                    atm=atm,
                    band_points=Decimal(str(self._config.symbols["strike_band_points"])),
                    exchange=exchange,
                )
            except Exception:
                logger.exception("scripmaster_fallback_failed", expiry=expiry_tag)
                continue
            if not instruments:
                continue
            atm_ce = next((i for i in instruments if i.is_atm and i.option_type == "CE"), None)
            atm_pe = next((i for i in instruments if i.is_atm and i.option_type == "PE"), None)
            spot_exchange = self._config.symbols["exchange_spot"]
            spot_token = self._config.symbols["spot_token"]
            keys = [BrokerAdapter.format_instrument(spot_exchange, spot_token)]
            keys.extend(BrokerAdapter.format_instrument(i.exchange, i.token) for i in instruments)
            universe = ContractUniverse(
                spot=spot,
                atm_strike=atm,
                expiry_symbol=expiry_tag,
                instruments=instruments,
                atm_ce=atm_ce,
                atm_pe=atm_pe,
                subscription_keys=keys,
            )
            logger.info(
                "contract_universe_from_scripmaster",
                expiry=expiry_tag,
                instrument_count=len(instruments),
                has_atm_ce=atm_ce is not None,
                has_atm_pe=atm_pe is not None,
            )
            return universe

        logger.warning(
            "weekly_expiry_all_chains_empty",
            candidates=candidates,
            hint="Broker option chain empty; scripmaster also returned no band matches",
        )
        return ContractUniverse(spot=spot, atm_strike=atm, expiry_symbol=candidates[0])

    async def _build_for_expiry(
        self,
        *,
        spot: Decimal,
        atm: Decimal,
        exchange: str,
        underlying: str,
        band: int,
        expiry_tag: str,
    ) -> ContractUniverse:
        anchor = chain_anchor_symbol(underlying, expiry_tag, int(atm), "CE")
        # Flattrade count=N returns ~2N strikes; request +1 for ATM±band_steps.
        chain = await self._broker.get_option_chain(
            exchange=exchange,
            tradingsymbol=anchor,
            strikeprice=float(atm),
            count=band + 1,
        )

        expiry_date = parse_expiry_tag(expiry_tag)
        instruments: list[Instrument] = []
        atm_ce: Instrument | None = None
        atm_pe: Instrument | None = None

        for row in chain:
            strike = Decimal(str(row.get("strprc", 0)))
            if not self.strike_in_band(strike, atm):
                continue
            optt = str(row.get("optt", "")).upper()
            if optt in ("CE", "C", "CALL"):
                option_type = "CE"
            elif optt in ("PE", "P", "PUT"):
                option_type = "PE"
            else:
                continue
            inst = Instrument(
                exchange=exchange,
                token=str(row["token"]),
                tsym=str(row["tsym"]),
                underlying=underlying,
                expiry_date=datetime.combine(expiry_date, datetime.min.time()).replace(tzinfo=timezone.utc)
                if expiry_date
                else None,
                strike=strike,
                option_type=option_type,
                lot_size=int(row.get("ls", 1)),
                tick_size=Decimal(str(row["ti"])) if row.get("ti") else None,
                is_atm=strike == atm,
                in_band=True,
            )
            instruments.append(inst)
            if strike == atm and option_type == "CE":
                atm_ce = inst
            if strike == atm and option_type == "PE":
                atm_pe = inst

        universe = self._finalize_universe(
            spot=spot,
            atm=atm,
            expiry_tag=expiry_tag,
            instruments=instruments,
        )
        if instruments:
            logger.info(
                "contract_universe_built",
                atm_strike=str(atm),
                expiry=expiry_tag,
                anchor=anchor,
                instrument_count=len(instruments),
                has_atm_ce=atm_ce is not None,
                has_atm_pe=atm_pe is not None,
            )
            universe = self._supplement_from_scripmaster(
                universe,
                atm=atm,
                underlying=underlying,
                exchange=exchange,
                expiry_tag=expiry_tag,
                band_steps=band,
            )
        return universe

    async def persist_instruments(self, pool: Any, universe: ContractUniverse) -> None:
        underlying = self._config.symbols["underlying"]
        expiry_date = None
        if universe.expiry_symbol:
            expiry_date = parse_expiry_tag(universe.expiry_symbol)

        # Mark prior expiries inactive so watchlist/DB reflect current weekly only.
        if expiry_date is not None:
            await pool.execute(
                """
                UPDATE instruments
                SET in_band = FALSE, is_atm = FALSE
                WHERE underlying = $1
                  AND (expiry_date IS DISTINCT FROM $2 OR expiry_date IS NULL)
                """,
                underlying,
                expiry_date,
            )
            # Reset current expiry band before re-upserting the active strike window.
            await pool.execute(
                """
                UPDATE instruments
                SET in_band = FALSE, is_atm = FALSE
                WHERE underlying = $1 AND expiry_date = $2
                """,
                underlying,
                expiry_date,
            )

        for inst in universe.instruments:
            await pool.execute(
                """
                INSERT INTO instruments (
                    exchange, token, tsym, underlying, expiry_date,
                    strike, option_type, lot_size, tick_size, is_atm, in_band
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (exchange, token) DO UPDATE SET
                    tsym = EXCLUDED.tsym,
                    expiry_date = EXCLUDED.expiry_date,
                    strike = EXCLUDED.strike,
                    option_type = EXCLUDED.option_type,
                    lot_size = EXCLUDED.lot_size,
                    is_atm = EXCLUDED.is_atm,
                    in_band = EXCLUDED.in_band
                """,
                inst.exchange,
                inst.token,
                inst.tsym,
                inst.underlying,
                inst.expiry_date.date() if inst.expiry_date else date.today(),
                inst.strike,
                inst.option_type,
                inst.lot_size,
                inst.tick_size,
                inst.is_atm,
                inst.in_band,
            )
