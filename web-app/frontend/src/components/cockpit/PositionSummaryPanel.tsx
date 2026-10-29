import { Briefcase } from "lucide-react";
import { useRef, useState } from "react";
import { exitPosition } from "../../api/client";
import type { MarketSummary, WatchlistOpenPosition } from "../../types";
import { StatusBadge } from "../StatusBadge";
import { buildRiskDashboard, entryStatusLabel } from "../../utils/riskDashboard";

function formatInr(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function formatPrice(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function optionTypeFor(position: WatchlistOpenPosition) {
  if (position.side && position.side !== "BUY" && position.side !== "SELL") {
    return position.side;
  }
  if (position.tsym.includes("CE")) return "CE";
  if (position.tsym.includes("PE")) return "PE";
  return position.side ?? "—";
}

function useStablePnlTone(pnl: number) {
  const toneRef = useRef<"positive" | "negative" | "neutral">("neutral");
  if (pnl > 1) toneRef.current = "positive";
  else if (pnl < -1) toneRef.current = "negative";
  return toneRef.current;
}

function lastSkipReason(summary: MarketSummary | null | undefined) {
  const rejection = summary?.recent_rejections?.[0];
  if (!rejection) return null;
  const reason = rejection.reasons?.[0];
  if (!reason) return null;
  return reason.replace(/_/g, " ");
}

interface PositionSummaryPanelProps {
  openPositions: WatchlistOpenPosition[];
  summary?: MarketSummary | null;
  onRefresh?: () => void;
  live?: boolean;
}

export function PositionSummaryPanel({
  openPositions,
  summary = null,
  onRefresh,
  live = false,
}: PositionSummaryPanelProps) {
  const [exiting, setExiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primary = openPositions[0] ?? null;
  const liveCount = openPositions.length;
  const dash = buildRiskDashboard(summary);
  const hasPosition = primary != null;

  const handleExit = async () => {
    if (!primary?.position_id) {
      setError("Missing position id — refresh and try again");
      return;
    }
    const label = `${primary.tsym} @ ₹${formatPrice(primary.current_ltp ?? primary.entry_price)}`;
    if (!window.confirm(`Exit ${label} at current LTP?`)) return;

    setExiting(true);
    setError(null);
    try {
      await exitPosition(primary.position_id);
      onRefresh?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExiting(false);
    }
  };

  const pnl = primary?.unrealized_pnl ?? 0;
  const entry = primary?.entry_price ?? 0;
  const ltp = primary?.current_ltp ?? entry;
  const mfeInr = primary?.mfe_inr ?? 0;
  const trailFloor = primary?.trail_floor ?? null;
  const deployed = primary?.premium_deployed ?? entry * (primary?.quantity ?? 1);
  const pnlPct = deployed > 0 ? (pnl / deployed) * 100 : 0;
  const optionType = primary ? optionTypeFor(primary) : "—";
  const pnlTone = useStablePnlTone(pnl);
  const skipReason = lastSkipReason(summary);
  const entryStatus = dash ? entryStatusLabel(dash.entryStatus) : null;
  const tradeCount = summary?.trade_count ?? 0;
  const sessionPnl = dash?.realizedPnl ?? summary?.today_pnl ?? 0;

  return (
    <section
      className={`cockpit-panel position-summary-panel${hasPosition ? "" : " position-summary-flat"}`}
    >
      <header className="cockpit-panel-head">
        <Briefcase size={14} />
        <h3>Position Summary</h3>
        <StatusBadge
          severity={hasPosition ? "success" : "neutral"}
          label={
            hasPosition
              ? live
                ? liveCount > 1
                  ? `${liveCount} LIVE`
                  : "LIVE"
                : liveCount > 1
                  ? `${liveCount} OPEN`
                  : "OPEN"
              : "FLAT"
          }
        />
      </header>

      {error && hasPosition ? <p className="position-error">{error}</p> : null}

      {hasPosition && primary ? (
        <>
          <p className="position-symbol mono">{primary.tsym}</p>
          <p className="position-side">
            {optionType} · {primary.lots ?? "—"} lots / {primary.quantity ?? "—"} qty
          </p>
          <p className={`position-pnl ${pnlTone}`}>
            {formatInr(pnl)}
            <span className="position-pnl-pct">
              ({pnlPct >= 0 ? "+" : ""}
              {pnlPct.toFixed(2)}%)
            </span>
          </p>
          <dl className="position-details">
            <div>
              <dt>Entry</dt>
              <dd className="mono tabular-nums">{formatPrice(entry)}</dd>
            </div>
            <div>
              <dt>LTP</dt>
              <dd className="mono accent tabular-nums">{formatPrice(ltp)}</dd>
            </div>
            <div>
              <dt>Max profit</dt>
              <dd className="mono tabular-nums positive">{formatInr(mfeInr)}</dd>
            </div>
            <div>
              <dt>Trail SL</dt>
              <dd className="mono tabular-nums">
                {trailFloor != null ? formatPrice(trailFloor) : "—"}
              </dd>
            </div>
            <div>
              <dt>Setup</dt>
              <dd>{primary.setup_type ?? "—"}</dd>
            </div>
          </dl>
          {liveCount > 1 ? (
            <p className="position-more muted">+{liveCount - 1} more in Open Positions below</p>
          ) : (
            <p className="position-more muted position-more--spacer" aria-hidden>
              &nbsp;
            </p>
          )}
        </>
      ) : (
        <>
          <p className="position-flat-msg">No open positions</p>
          {entryStatus ? (
            <p className="position-flat-status">{entryStatus}</p>
          ) : null}
          {tradeCount > 0 ? (
            <p className="position-flat-hint muted">
              Session: {tradeCount} closed trade{tradeCount === 1 ? "" : "s"} · realized{" "}
              <span className={sessionPnl >= 0 ? "positive" : "negative"}>
                {formatInr(sessionPnl)}
              </span>
            </p>
          ) : (
            <p className="position-flat-hint muted">
              Live trades appear here when the engine opens a position.
            </p>
          )}
          {skipReason ? (
            <p className="position-flat-skip muted">Latest skip: {skipReason}</p>
          ) : null}
          <p className="position-more muted position-more--spacer" aria-hidden>
            &nbsp;
          </p>
        </>
      )}

      {hasPosition ? (
        <div className="position-actions">
          <button type="button" className="btn-modify" disabled title="Coming soon">
            Modify SL
          </button>
          <button type="button" className="btn-partial" disabled title="Coming soon">
            Book Partial
          </button>
          <button
            type="button"
            className="btn-exit"
            onClick={handleExit}
            disabled={!primary?.position_id || exiting}
          >
            {exiting ? "Exiting…" : "Exit Trade"}
          </button>
        </div>
      ) : null}
    </section>
  );
}
