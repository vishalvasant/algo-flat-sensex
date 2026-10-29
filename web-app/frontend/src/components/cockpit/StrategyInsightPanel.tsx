import { Brain, ChevronLeft, ChevronRight, Target } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CommoditySnapshot, MarketSummary, Watchlist } from "../../types";
import {
  AI_PANEL_INDICES,
  AI_PANEL_LABELS,
  type AiPanelIndex,
  type IndexStrategyInsight,
  levelsFromSpot,
  strategyByIndex,
} from "../../utils/cockpitDummyData";

interface StrategyInsightPanelProps {
  summary: MarketSummary | null;
  watchlist?: Watchlist | null;
  activeUnderlying: string;
  commodities?: CommoditySnapshot[];
}

function formatLevel(value: number) {
  return value.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function normalizeBias(raw: string | undefined): IndexStrategyInsight["bias"] {
  const v = (raw ?? "NEUTRAL").toUpperCase();
  if (v === "BULLISH" || v === "BEARISH") return v;
  return "NEUTRAL";
}

function clampConf(value: number | null | undefined, fallback: number) {
  if (value == null || Number.isNaN(Number(value))) return fallback;
  return Math.min(100, Math.max(0, Math.round(Number(value))));
}

function leadingSide(
  bullish: number,
  bearish: number,
): IndexStrategyInsight["leadingSide"] {
  if (bullish > bearish + 4) return "BULLISH";
  if (bearish > bullish + 4) return "BEARISH";
  return "NEUTRAL";
}

function buildInsight(
  underlying: AiPanelIndex,
  summary: MarketSummary | null,
  watchlist: Watchlist | null | undefined,
  spot: number | null,
): IndexStrategyInsight {
  const base = strategyByIndex[underlying];
  const scanUl = (
    watchlist?.active_underlying ??
    summary?.active_underlying ??
    summary?.underlying ??
    "SENSEX"
  ).toUpperCase();
  const isLiveScan = underlying === scanUl && (summary != null || watchlist != null);

  if (!isLiveScan) {
    if (spot != null && spot > 0) {
      const levels = levelsFromSpot(spot, underlying);
      return { ...base, ...levels };
    }
    return base;
  }

  const strategy = (summary?.strategy ?? "router").replace(/_/g, " ").toUpperCase();
  const bias = normalizeBias(watchlist?.bias_5m ?? summary?.bias_5m);
  const bullishConfidence = clampConf(
    watchlist?.bullish_confidence ?? summary?.bullish_confidence,
    base.bullishConfidence,
  );
  const bearishConfidence = clampConf(
    watchlist?.bearish_confidence ?? summary?.bearish_confidence,
    base.bearishConfidence,
  );
  const routerConf = summary?.router_confidence ?? summary?.confidence;
  const flow = summary?.decision_flow;
  const confidence =
    routerConf != null
      ? clampConf(routerConf, Math.max(bullishConfidence, bearishConfidence))
      : Math.max(bullishConfidence, bearishConfidence);

  const reasons: string[] = [];
  reasons.push(`Bullish (CE) MTF ${bullishConfidence}% · Bearish (PE) MTF ${bearishConfidence}%`);
  const lead = leadingSide(bullishConfidence, bearishConfidence);
  if (lead !== "NEUTRAL") {
    reasons.push(`${lead} side leads by ${Math.abs(bullishConfidence - bearishConfidence)} pts`);
  }
  if (watchlist?.bias_confidence_mismatch || summary?.bias_confidence_mismatch) {
    const gap = watchlist?.mtf_confidence_gap ?? summary?.mtf_confidence_gap ?? 0;
    reasons.push(`Bias vs MTF mismatch — counter-side leads by ${gap} pts`);
  }
  if (summary?.basis_points != null && flow?.basis_ok === false) {
    reasons.push(
      `FUT–cash basis ${summary.basis_points >= 0 ? "+" : ""}${summary.basis_points.toFixed(1)} pts exceeds ±${flow.basis_limit_points ?? 30} limit`,
    );
  }
  if (summary?.fut_vs_vwap && flow?.vwap_source !== "nse_index") {
    reasons.push(`FUT ${summary.fut_vs_vwap.toLowerCase()} session VWAP`);
  }
  if (summary?.spot_vs_vwap) {
    reasons.push(`Price ${summary.spot_vs_vwap.toLowerCase()} session VWAP`);
  }
  if (summary?.bias_5m || watchlist?.bias_5m) {
    reasons.push(`5m structure is ${(watchlist?.bias_5m ?? summary?.bias_5m ?? "").toLowerCase()}`);
  }
  if (summary?.regime) {
    reasons.push(`Regime: ${summary.regime.replace(/_/g, " ")}`);
  }
  if (summary?.is_expiry_day) {
    reasons.push("Weekly expiry — conf ≥82, max 50% deploy, theta-aware (normal exits)");
  }
  if (summary?.block_reason) {
    reasons.push(`Block: ${summary.block_reason.replace(/_/g, " ")}`);
  }
  if (summary?.kill_switch) {
    reasons.push("Kill switch is ON");
  }
  if (summary?.recent_rejections?.length) {
    const last = summary.recent_rejections[0];
    reasons.push(`Last skip: ${last.tsym} — ${last.reasons.join(", ")}`);
  }
  const lv = summary?.levels;
  if (lv) {
    if (lv.at_resistance && lv.nearest_resistance != null) {
      reasons.push(
        `At resistance ${formatLevel(lv.nearest_resistance)} (${lv.confluence_resistance ?? 0} confluence)`,
      );
    }
    if (lv.at_support && lv.nearest_support != null) {
      reasons.push(
        `At support ${formatLevel(lv.nearest_support)} (${lv.confluence_support ?? 0} confluence)`,
      );
    }
    if (lv.rejection_setup && lv.rejection_level) {
      reasons.push(`Level rejection: ${lv.rejection_level} → ${lv.rejection_setup.toUpperCase()}`);
    }
  }
  if (reasons.length <= 2) {
    reasons.push(...base.reasons.slice(0, 2));
  }

  const liveSpot = spot ?? summary?.spot_ltp ?? watchlist?.spot_ltp ?? null;
  const levels =
    liveSpot != null && liveSpot > 0
      ? levelsFromSpot(liveSpot, underlying)
      : {
          entryLow: base.entryLow,
          entryHigh: base.entryHigh,
          targets: base.targets,
          stopLoss: base.stopLoss,
        };

  return {
    name: strategy,
    bias,
    confidence,
    bullishConfidence,
    bearishConfidence,
    leadingSide: lead,
    reasons: reasons.slice(0, 5),
    ...levels,
  };
}

function SideConfidenceBars({ insight }: { insight: IndexStrategyInsight }) {
  return (
    <div className="side-confidence" aria-label="Bullish and bearish confidence">
      <div
        className={`side-conf-row bullish${insight.leadingSide === "BULLISH" ? " is-leading" : ""}`}
      >
        <span className="side-conf-label">BULLISH</span>
        <div className="side-conf-track" aria-hidden>
          <div
            className="side-conf-fill"
            style={{ width: `${insight.bullishConfidence}%` }}
          />
        </div>
        <span className="side-conf-pct mono">{insight.bullishConfidence}%</span>
      </div>
      <div
        className={`side-conf-row bearish${insight.leadingSide === "BEARISH" ? " is-leading" : ""}`}
      >
        <span className="side-conf-label">BEARISH</span>
        <div className="side-conf-track" aria-hidden>
          <div
            className="side-conf-fill"
            style={{ width: `${insight.bearishConfidence}%` }}
          />
        </div>
        <span className="side-conf-pct mono">{insight.bearishConfidence}%</span>
      </div>
      <p className="side-conf-router muted">
        Router {insight.confidence}%
        {insight.leadingSide !== "NEUTRAL" ? ` · ${insight.leadingSide} leads` : " · balanced"}
      </p>
    </div>
  );
}

function StrategySlide({ insight }: { insight: IndexStrategyInsight }) {
  const biasClass = `strategy-v bias-${insight.bias.toLowerCase()}`;

  return (
    <div className="strategy-slide-inner">
      <div className="strategy-meta-grid">
        <div className="strategy-meta-cell">
          <span className="strategy-k">Strategy</span>
          <strong className="strategy-v">{insight.name}</strong>
        </div>
        <div className="strategy-meta-cell align-end">
          <span className="strategy-k">5m Bias</span>
          <strong className={biasClass}>{insight.bias}</strong>
        </div>
      </div>

      <section className="strategy-hero strategy-hero--dual">
        <p className="strategy-name">
          <Target size={15} strokeWidth={2.25} />
          <span>Side Confidence</span>
        </p>
        <SideConfidenceBars insight={insight} />
      </section>

      <ul className="strategy-reasons">
        {insight.reasons.map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>

      <div className="strategy-levels" aria-label="Trade levels">
        <div className="strategy-level-col entry">
          <span className="strategy-level-k">Entry</span>
          <span className="strategy-level-v mono">
            <span className="strategy-level-line">{formatLevel(insight.entryLow)}</span>
            <span className="strategy-level-line muted">to {formatLevel(insight.entryHigh)}</span>
          </span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">T1</span>
          <span className="strategy-level-v mono">{formatLevel(insight.targets[0])}</span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">T2</span>
          <span className="strategy-level-v mono">{formatLevel(insight.targets[1])}</span>
        </div>
        <div className="strategy-level-col">
          <span className="strategy-level-k">SL</span>
          <span className="strategy-level-v mono sl">{formatLevel(insight.stopLoss)}</span>
        </div>
      </div>
    </div>
  );
}

export function StrategyInsightPanel({
  summary,
  watchlist = null,
  activeUnderlying,
  commodities = [],
}: StrategyInsightPanelProps) {
  const indices = AI_PANEL_INDICES;
  const carouselEnabled = indices.length > 1;
  const [slideIdx, setSlideIdx] = useState(0);
  const [paused, setPaused] = useState(false);
  const touchStart = useRef<number | null>(null);

  const spotByUnderlying = useMemo(() => {
    const map: Record<string, number | null> = {};
    for (const c of commodities) {
      map[c.underlying] = c.spot_ltp ?? null;
    }
    return map;
  }, [commodities]);

  const slides = useMemo(
    () =>
      indices.map((ul) => ({
        underlying: ul,
        label: AI_PANEL_LABELS[ul],
        insight: buildInsight(ul, summary, watchlist, spotByUnderlying[ul] ?? null),
      })),
    [indices, summary, watchlist, spotByUnderlying],
  );

  const goTo = useCallback(
    (idx: number) => {
      const next = ((idx % indices.length) + indices.length) % indices.length;
      setSlideIdx(next);
    },
    [indices],
  );

  useEffect(() => {
    const idx = indices.indexOf(activeUnderlying as AiPanelIndex);
    if (idx >= 0) setSlideIdx(idx);
  }, [activeUnderlying, indices]);

  useEffect(() => {
    if (!carouselEnabled || paused) return;
    const id = window.setInterval(() => {
      setSlideIdx((i) => (i + 1) % indices.length);
    }, 9000);
    return () => window.clearInterval(id);
  }, [carouselEnabled, paused, indices]);

  const onTouchStart = (x: number) => {
    touchStart.current = x;
  };

  const onTouchEnd = (x: number) => {
    if (touchStart.current == null) return;
    const delta = x - touchStart.current;
    touchStart.current = null;
    if (Math.abs(delta) < 40) return;
    goTo(slideIdx + (delta < 0 ? 1 : -1));
  };

  const current = slides[slideIdx];
  const liveFeed =
    watchlist?.feed_mode === "websocket" || summary?.feed_mode === "websocket";

  return (
    <section
      className="cockpit-panel strategy-panel ref-style"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <header className="cockpit-panel-head strategy-panel-head">
        <div className="strategy-panel-title">
          <Brain size={14} />
          <h3>Trade Panel</h3>
          {liveFeed ? <span className="strategy-live-dot" title="Live side confidence" /> : null}
        </div>
        <div className="strategy-panel-head-right">
          <span className="strategy-index-pill">{current.label}</span>
          {carouselEnabled ? (
            <div
              className="strategy-carousel-dots"
              role="tablist"
              aria-label="Index strategy slides"
            >
              {slides.map((slide, i) => (
                <button
                  key={slide.underlying}
                  type="button"
                  role="tab"
                  aria-selected={i === slideIdx}
                  aria-label={slide.label}
                  className={i === slideIdx ? "strategy-dot active" : "strategy-dot"}
                  onClick={() => goTo(i)}
                />
              ))}
            </div>
          ) : null}
        </div>
      </header>

      <div
        className="strategy-carousel-body"
        onTouchStart={(e) => onTouchStart(e.touches[0]?.clientX ?? 0)}
        onTouchEnd={(e) => onTouchEnd(e.changedTouches[0]?.clientX ?? 0)}
      >
        {carouselEnabled ? (
          <button
            type="button"
            className="strategy-carousel-nav prev"
            onClick={() => goTo(slideIdx - 1)}
            aria-label="Previous index"
          >
            <ChevronLeft size={14} />
          </button>
        ) : null}

        <div className="strategy-carousel-track">
          {slides.map((slide, i) => (
            <article
              key={slide.underlying}
              className={i === slideIdx ? "strategy-slide active" : "strategy-slide"}
              aria-hidden={i !== slideIdx}
            >
              <StrategySlide insight={slide.insight} />
            </article>
          ))}
        </div>

        {carouselEnabled ? (
          <button
            type="button"
            className="strategy-carousel-nav next"
            onClick={() => goTo(slideIdx + 1)}
            aria-label="Next index"
          >
            <ChevronRight size={14} />
          </button>
        ) : null}
      </div>
    </section>
  );
}
