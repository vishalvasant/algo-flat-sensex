import { useState } from "react";
import type { CSSProperties } from "react";
import { RotateCcw, Shield } from "lucide-react";
import { resetPaperAccount } from "../../api/client";
import type { MarketSummary } from "../../types";
import { StatusBadge } from "../StatusBadge";
import { COCKPIT_REFRESH_EVENT } from "../CockpitEngineControls";
import { buildRiskDashboard, riskDriverCaption } from "../../utils/riskDashboard";

function formatInr(value: number | undefined, digits = 0) {
  return `₹${(value ?? 0).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;
}

interface RiskManagerPanelProps {
  summary: MarketSummary | null;
  onRefresh?: () => void;
}

export function RiskManagerPanel({ summary, onRefresh }: RiskManagerPanelProps) {
  const [resetBusy, setResetBusy] = useState(false);
  const hasData = summary != null;
  const isLive = summary?.trading_mode === "live";
  const dash = buildRiskDashboard(summary);

  const usedMargin = summary?.used_margin ?? summary?.deployed_capital ?? 0;
  const available = summary?.available_capital ?? 0;
  const tradeCount = summary?.trade_count ?? 0;

  const riskPct = dash?.riskPct ?? 0;
  const riskLabel = dash?.riskLabel ?? "LOW RISK";
  const riskSeverity = dash?.riskSeverity ?? "success";
  const riskCaption = dash ? riskDriverCaption(dash.riskDriver, dash.riskPct) : "";

  const handleResetPaper = async () => {
    if (isLive) {
      alert("Switch to PAPER mode before resetting the paper ledger.");
      return;
    }
    if (
      !window.confirm(
        "Reset paper account? All paper trades and positions will be deleted and capital restored to the configured starting balance.",
      )
    ) {
      return;
    }
    setResetBusy(true);
    try {
      const result = await resetPaperAccount();
      onRefresh?.();
      window.dispatchEvent(new CustomEvent(COCKPIT_REFRESH_EVENT));
      const warn =
        result.warnings?.length
          ? `\n\nNote: ${result.warnings.join("; ")}`
          : "";
      alert(
        `Paper account reset to ₹${result.starting_capital.toLocaleString("en-IN")}${warn}`,
      );
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setResetBusy(false);
    }
  };

  return (
    <section className="cockpit-panel risk-panel">
      <header className="cockpit-panel-head">
        <Shield size={14} />
        <h3>Risk Manager</h3>
        <StatusBadge severity={riskSeverity} label={riskLabel} />
      </header>

      {summary?.entries_blocked && !dash?.hasOpenPosition ? (
        <p className="risk-block-banner" role="status">
          Entries blocked
          {summary.block_reason ? ` — ${summary.block_reason.replace(/_/g, " ")}` : ""}
        </p>
      ) : null}

      {summary?.is_expiry_day ? (
        <p className="expiry-caution-banner" role="status">
          Weekly expiry — conf ≥82, max 50% deploy (confidence-scaled), theta-aware scoring. Exits same as normal day.
        </p>
      ) : null}

      <figure className="risk-gauge-semicircle" style={{ "--pct": riskPct } as CSSProperties}>
        <div className="risk-arc">
          <div className="risk-arc-fill" />
          <div className="risk-arc-hole">
            <span className="risk-arc-value">{hasData ? `${riskPct}%` : "—"}</span>
            <span className="risk-arc-label">{hasData ? riskLabel : "LOADING"}</span>
          </div>
        </div>
      </figure>
      {hasData && riskCaption ? (
        <p className="risk-driver-caption muted">{riskCaption}</p>
      ) : null}

      <dl className="stat-rows">
        <div className="stat-row">
          <dt>
            Daily P&amp;L
            {isLive ? <span className="stat-sub muted"> · broker</span> : null}
          </dt>
          <dd
            className={`mono ${
              dash == null ? "" : dash.totalPnl >= 0 ? "positive" : "negative"
            }`}
          >
            {dash == null ? "—" : formatInr(dash.totalPnl, 2)}
          </dd>
        </div>
        <div className="stat-row stat-row--sub">
          <dt>Realized</dt>
          <dd
            className={`mono stat-sub-value ${
              dash == null ? "" : dash.realizedPnl >= 0 ? "positive" : "negative"
            }`}
          >
            {dash == null ? "—" : formatInr(dash.realizedPnl, 2)}
          </dd>
        </div>
        <div className="stat-row stat-row--sub">
          <dt>Open MTM</dt>
          <dd
            className={`mono stat-sub-value ${
              dash == null ? "" : dash.unrealizedPnl >= 0 ? "positive" : "negative"
            }`}
          >
            {dash == null ? "—" : formatInr(dash.unrealizedPnl, 2)}
          </dd>
        </div>
        <div className="stat-row">
          <dt>Used margin</dt>
          <dd className="mono">{hasData ? formatInr(usedMargin) : "—"}</dd>
        </div>
        <div className="stat-row">
          <dt>Available</dt>
          <dd className="mono accent">{hasData ? formatInr(available) : "—"}</dd>
        </div>
        {hasData && tradeCount > 0 ? (
          <div className="stat-row">
            <dt>Trades today</dt>
            <dd className="mono">{tradeCount}</dd>
          </div>
        ) : null}
      </dl>

      {!isLive ? (
        <button
          type="button"
          className="btn btn-sm btn-ghost risk-reset-btn"
          onClick={handleResetPaper}
          disabled={resetBusy}
          title="Wipe paper trades and restore starting capital"
        >
          <RotateCcw size={13} />
          {resetBusy ? "Resetting…" : "Reset paper account"}
        </button>
      ) : null}
    </section>
  );
}
