from __future__ import annotations

import asyncio

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.market_data.engine import MarketDataEngine
from algoflat.models.events import QuoteUpdate
from algoflat.option_data.layer import OptionDataLayer
from algoflat.time_util import now_ist

logger = structlog.get_logger(__name__)


def quote_from_rest(exchange: str, token: str, raw: dict) -> QuoteUpdate | None:
  if not isinstance(raw, dict) or raw.get("stat") not in (None, "Ok"):
    return None
  ltp = BrokerAdapter.parse_decimal(raw.get("lp"))
  if ltp is None:
    ltp = BrokerAdapter.parse_decimal(raw.get("c"))
  return QuoteUpdate(
    ts=now_ist(),
    exchange=exchange,
    instrument_token=token,
    tsym=raw.get("tsym"),
    ltp=ltp,
    bid=BrokerAdapter.parse_decimal(raw.get("bp1")),
    ask=BrokerAdapter.parse_decimal(raw.get("sp1")),
    volume=int(raw["v"]) if raw.get("v") not in (None, "") else None,
    oi=int(raw["oi"]) if raw.get("oi") not in (None, "") else None,
    source="rest",
  )


class RestQuotePoller:
  def __init__(
    self,
    broker: BrokerAdapter,
    market_data: MarketDataEngine,
    option_data: OptionDataLayer,
    spot_exchange: str,
    spot_token: str,
  ) -> None:
    self._broker = broker
    self._market_data = market_data
    self._option_data = option_data
    self._spot_exchange = spot_exchange
    self._spot_token = spot_token

  def set_spot(self, exchange: str, spot_token: str) -> None:
    self._spot_exchange = exchange
    self._spot_token = spot_token

  async def poll_universe(self, universe: ContractUniverse | None) -> int:
    updated = 0
    try:
      spot_raw = await self._broker.get_quotes(self._spot_exchange, self._spot_token)
    except ConnectionError:
      logger.warning(
        "rest_spot_quote_failed",
        exchange=self._spot_exchange,
        token=self._spot_token,
      )
      spot_raw = None
    if spot_raw:
      spot_quote = quote_from_rest(self._spot_exchange, self._spot_token, spot_raw)
      if spot_quote:
        await self._market_data.on_quote(spot_quote)
        updated += 1

    if universe is None:
      return updated

    async def _one(inst) -> QuoteUpdate | None:
      raw = await self._broker.get_quotes(inst.exchange, inst.token)
      return quote_from_rest(inst.exchange, inst.token, raw)

    # Parallel REST quotes so the option chain refreshes quickly when WS is down.
    quotes = await asyncio.gather(
      *[_one(inst) for inst in universe.instruments],
      return_exceptions=True,
    )
    for quote in quotes:
      if isinstance(quote, Exception) or quote is None:
        continue
      await self._market_data.on_quote(quote)
      self._option_data.update_from_quote(quote)
      updated += 1

    logger.debug("rest_quote_poll_complete", updated=updated)
    return updated
