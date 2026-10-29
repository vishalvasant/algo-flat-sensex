import { useMemo } from "react";
import { BarChart3, TrendingDown, TrendingUp } from "lucide-react";
import type { MarketSummary, Watchlist } from "../../types";
import { useCommodityChain } from "../../hooks/useCommodityChain";

function formatPrice(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function formatPct(n: number) {
  return `${n.toFixed(0)}%`;
}

type PcrTone = "bullish" | "bearish" | "neutral";

function pcrTone(value: number): PcrTone {
  if (value < 0.9) return "bullish";
  if (value > 1.1) return "bearish";
  return "neutral";
}

function breadthSentiment(adv: number, dec: number): {
  label: string;
  tone: "bullish" | "bearish" | "neutral";
} {
  const net = adv - dec;
  const total = adv + dec;
  if (total <= 0) return { label: "Neutral", tone: "neutral" };
  const ratio = net / total;
  if (ratio >= 0.12) return { label: "Bullish", tone: "bullish" };
  if (ratio <= -0.12) return { label: "Bearish", tone: "bearish" };
  return { label: "Mixed", tone: "neutral" };
}

interface MarketStatsPanelProps {
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
  activeCommodity: string;
}

export function MarketStatsPanel({
  watchlist,
  summary,
  activeCommodity,
}: MarketStatsPanelProps) {
  const { items } = useCommodityChain(watchlist, summary, activeCommodity);

  const { liveOiPcr, liveVolPcr } = useMemo(() => {
    let ceOi = 0;
    let peOi = 0;
    let ceVol = 0;
    let peVol = 0;
    for (const item of items) {
      const oi = item.oi ?? 0;
      const vol = item.volume ?? 0;
      if (item.option_type === "CE") {
        ceOi += oi;
        ceVol += vol;
      }
      if (item.option_type === "PE") {
        peOi += oi;
        peVol += vol;
      }
    }
    return {
      liveOiPcr: ceOi > 0 ? peOi / ceOi : null,
      liveVolPcr: ceVol > 0 ? peVol / ceVol : null,
    };
  }, [items]);

  const breadth = summary?.market_intelligence?.breadth;
  const breadthLive = breadth?.available === true;
  const advancing = breadthLive ? Number(breadth.advancing ?? 0) : null;
  const declining = breadthLive ? Number(breadth.declining ?? 0) : null;
  const neutral = breadthLive ? Number(breadth.unchanged ?? 0) : null;
  const totalBreadth =
    advancing != null && declining != null && neutral != null
      ? advancing + declining + neutral
      : 0;
  const advPct = totalBreadth > 0 && advancing != null ? (advancing / totalBreadth) * 100 : 0;
  const decPct = totalBreadth > 0 && declining != null ? (declining / totalBreadth) * 100 : 0;
  const neuPct = totalBreadth > 0 && neutral != null ? (neutral / totalBreadth) * 100 : 0;
  const netBreadth = advancing != null && declining != null ? advancing - declining : null;
  const sentiment =
    advancing != null && declining != null
      ? breadthSentiment(advancing, declining)
      : { label: "Waiting", tone: "neutral" as const };

  const vixRow = (summary?.commodities ?? watchlist?.commodities ?? []).find(
    (row) => row.underlying === "INDIAVIX",
  );
  const vixLive = summary?.market_intelligence?.vix;
  const vix = vixRow?.spot_ltp ?? (typeof vixLive?.vix_level === "number" ? vixLive.vix_level : null);
  const vixChange =
    vixRow?.change_pct ??
    (typeof vixLive?.vix_change_pct === "number" ? vixLive.vix_change_pct : null);
  const vixUp = (vixChange ?? 0) > 0;

  const volPcr = liveVolPcr;
  const oiPcr = liveOiPcr;
  const vwap = summary?.session_vwap ?? null;
  const spotVsVwap = summary?.spot_vs_vwap?.toLowerCase();

  return (
    <section className="cockpit-panel market-stats-panel market-breadth-panel">
      <header className="cockpit-panel-head market-breadth-head">
        <div className="market-breadth-title">
          <BarChart3 size={14} aria-hidden />
          <h3>Market Breadth</h3>
        </div>
        <span className={`breadth-sentiment-badge tone-${sentiment.tone}`}>
          {sentiment.label}
        </span>
      </header>

      <div className="breadth-summary">
        <p className="breadth-net mono">
          {netBreadth == null ? "—" : `${netBreadth >= 0 ? "+" : ""}${netBreadth}`}
          <span className="breadth-net-label">net sectors</span>
        </p>
        <p className="breadth-participation muted">
          {totalBreadth > 0 ? `${totalBreadth.toLocaleString("en-IN")} sectors` : "sector quotes pending"}
        </p>
      </div>

      <div
        className="breadth-bar"
        role="img"
        aria-label={`Advancing ${formatPct(advPct)}, declining ${formatPct(decPct)}, neutral ${formatPct(neuPct)}`}
      >
        <div
          className="breadth-bar-seg adv"
          style={{ width: `${advPct}%` }}
          title={`Advancing ${advancing}`}
        />
        <div
          className="breadth-bar-seg dec"
          style={{ width: `${decPct}%` }}
          title={`Declining ${declining}`}
        />
        <div
          className="breadth-bar-seg neu"
          style={{ width: `${neuPct}%` }}
          title={`Neutral ${neutral}`}
        />
      </div>

      <ul className="breadth-chips">
        <li className="breadth-chip adv">
          <span className="breadth-chip-value mono">{advancing ?? "—"}</span>
          <span className="breadth-chip-meta">
            <i className="breadth-dot" aria-hidden />
            Adv · {formatPct(advPct)}
          </span>
        </li>
        <li className="breadth-chip dec">
          <span className="breadth-chip-value mono">{declining ?? "—"}</span>
          <span className="breadth-chip-meta">
            <i className="breadth-dot" aria-hidden />
            Dec · {formatPct(decPct)}
          </span>
        </li>
        <li className="breadth-chip neu">
          <span className="breadth-chip-value mono">{neutral ?? "—"}</span>
          <span className="breadth-chip-meta">
            <i className="breadth-dot" aria-hidden />
            Neu · {formatPct(neuPct)}
          </span>
        </li>
      </ul>

      <dl className="breadth-metrics">
        <div className="breadth-metric">
          <dt>
            Vol PCR
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className={`mono ${volPcr == null ? "" : `tone-${pcrTone(volPcr)}`}`}>
            {volPcr == null ? "—" : volPcr.toFixed(2)}
          </dd>
        </div>
        <div className="breadth-metric">
          <dt>
            OI PCR
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className={`mono ${oiPcr == null ? "" : `tone-${pcrTone(oiPcr)}`}`}>
            {oiPcr == null ? "—" : oiPcr.toFixed(2)}
          </dd>
        </div>
        <div className="breadth-metric breadth-metric--wide">
          <dt>
            VWAP
            <span className="breadth-metric-sub">{activeCommodity}</span>
          </dt>
          <dd className="mono accent">{formatPrice(vwap)}</dd>
          {spotVsVwap ? (
            <dd className={`breadth-metric-hint tone-${spotVsVwap === "above" ? "bullish" : spotVsVwap === "below" ? "bearish" : "neutral"}`}>
              Spot {spotVsVwap} VWAP
            </dd>
          ) : null}
        </div>
        <div className="breadth-metric">
          <dt>India VIX</dt>
          <dd className={`mono breadth-vix ${vix == null ? "" : vixUp ? "tone-bearish" : "tone-bullish"}`}>
            {vix == null ? null : vixUp ? <TrendingUp size={12} aria-hidden /> : <TrendingDown size={12} aria-hidden />}
            <span>{vix == null ? "—" : vix.toFixed(2)}</span>
            {vixChange != null ? (
              <small>
                ({vixChange > 0 ? "+" : ""}
                {vixChange.toFixed(2)}%)
              </small>
            ) : null}
          </dd>
        </div>
      </dl>
    </section>
  );
}
