"""Market intelligence orchestrator — New-Engine phases 1–10."""
from __future__ import annotations

from typing import Any

from algoflat.config import AppConfig
from algoflat.market_intelligence.breadth import build_breadth_features
from algoflat.market_intelligence.ce_pe_scoring import build_ce_pe_comparison
from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.data_quality import assess_data_quality
from algoflat.market_intelligence.divergence import build_divergence_features
from algoflat.market_intelligence.early_bird import build_early_bird_context
from algoflat.market_intelligence.fut_features import build_fut_features
from algoflat.market_intelligence.global_context import build_global_features
from algoflat.market_intelligence.premium_features import build_premium_features
from algoflat.market_intelligence.reversal_context import build_reversal_context
from algoflat.market_intelligence.sectors import build_sector_features
from algoflat.market_intelligence.spot_features import build_spot_features
from algoflat.market_intelligence.vix_features import build_vix_features


class MarketIntelligenceEngine:
  def __init__(self, config: AppConfig) -> None:
    self._cfg = config.market_intelligence if hasattr(config, "market_intelligence") else {}
    self._sector_symbols = [
      str(s.get("symbol", "")).upper()
      for s in (config.symbols.get("sector_indices") or [])
      if s.get("symbol")
    ]

  def build(self, ctx: MarketIntelligenceContext) -> dict[str, Any]:
    if not self._cfg.get("enabled", True):
      return {"enabled": False}

    phases = self._cfg.get("phases") or {}
    snap: dict[str, Any] = {
      "version": self._cfg.get("version", "mi_v1.0.0"),
      "enabled": True,
    }

    if phases.get("normalize", True):
      snap["spot"] = build_spot_features(ctx)
    else:
      snap["spot"] = {"available": False}

    if phases.get("fut_features", True):
      snap["fut"] = build_fut_features(ctx)
    else:
      snap["fut"] = {"available": False}

    if phases.get("vix", True):
      snap["vix"] = build_vix_features(ctx, cfg=self._cfg.get("vix") or {})
    else:
      snap["vix"] = {"available": False}

    if phases.get("premium_confirm", True):
      snap["premium"] = build_premium_features(ctx, cfg=self._cfg.get("premium_confirm") or {})
    else:
      snap["premium"] = {"available": False}

    if phases.get("ce_pe_scoring", True):
      snap["ce_pe"] = build_ce_pe_comparison(
        ctx,
        premium=snap.get("premium") or {},
        fut=snap.get("fut") or {},
        cfg=self._cfg.get("ce_pe_scoring") or {},
      )
    else:
      snap["ce_pe"] = {}

    if phases.get("sectors", True) and self._sector_symbols:
      snap["sectors"] = build_sector_features(ctx, sector_symbols=self._sector_symbols)
    else:
      snap["sectors"] = {"available": False, "sectors": []}

    if phases.get("breadth", True):
      snap["breadth"] = build_breadth_features(
        ctx,
        sectors=snap.get("sectors") or {},
        cfg=self._cfg.get("breadth") or {},
      )
    else:
      snap["breadth"] = {"available": False}

    if phases.get("global", True):
      snap["global"] = build_global_features(
        ctx,
        cfg=self._cfg.get("global") or {},
        vix_features=snap.get("vix") or {},
      )
    else:
      snap["global"] = {"available": False}

    if phases.get("breadth", True) or phases.get("sectors", True):
      snap["divergence"] = build_divergence_features(
        spot=snap.get("spot") or {},
        breadth=snap.get("breadth") or {},
        sectors=snap.get("sectors") or {},
        cfg=self._cfg.get("divergence") or {},
      )

    if phases.get("extension_ev", True):
      snap["early_bird"] = build_early_bird_context(
        ctx,
        spot=snap.get("spot") or {},
        fut=snap.get("fut") or {},
        breadth=snap.get("breadth") or {},
        sectors=snap.get("sectors") or {},
        cfg=self._cfg.get("early_bird") or {},
      )
      snap["reversal"] = build_reversal_context(
        ctx,
        spot=snap.get("spot") or {},
        fut=snap.get("fut") or {},
        breadth=snap.get("breadth") or {},
        sectors=snap.get("sectors") or {},
        divergence=snap.get("divergence") or {},
        cfg=self._cfg.get("reversal") or {},
      )

    snap["data_quality"] = assess_data_quality(snap)
    return snap

  def apply_to_features_extra(self, extra: dict[str, Any], mi: dict[str, Any]) -> dict[str, Any]:
    """Flatten key MI outputs into feature extra for routers/validators."""
    out = dict(extra)
    out["market_intelligence"] = mi
    ce_pe = mi.get("ce_pe") or {}
    out["ce_strength"] = ce_pe.get("ce_strength")
    out["pe_strength"] = ce_pe.get("pe_strength")
    out["ce_pe_verdict"] = ce_pe.get("verdict")
    out["mi_no_trade_recommended"] = ce_pe.get("no_trade_recommended")
    out["global_sentiment_score"] = (mi.get("global") or {}).get("global_sentiment_score")
    out["vix_level"] = (mi.get("vix") or {}).get("vix_level")
    out["sector_breadth_pct"] = (mi.get("sectors") or {}).get("sector_breadth_pct")
    out["nifty_breadth_divergence"] = (mi.get("divergence") or {}).get("nifty_breadth_divergence")
    out["early_bird_active"] = (mi.get("early_bird") or {}).get("early_bird_active")
    out["reversal_risk_elevated"] = (mi.get("reversal") or {}).get("reversal_risk_elevated")
    if mi.get("fut"):
      out["spot_fut"] = {**(out.get("spot_fut") or {}), **mi["fut"]}
    return out
