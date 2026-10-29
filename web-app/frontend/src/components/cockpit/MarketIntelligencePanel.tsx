import { Activity } from "lucide-react";
import type { MarketSummary } from "../../types";

interface MarketIntelligencePanelProps {
  summary: MarketSummary | null;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toFixed(1)}%`;
}

function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function MarketIntelligencePanel({ summary }: MarketIntelligencePanelProps) {
  const mi = summary?.market_intelligence;
  const flow = summary?.decision_flow;
  const ml = summary?.ml_shadow ?? flow?.ml_shadow;
  const enabled = mi?.enabled !== false;

  if (!enabled) {
    return (
      <section className="cockpit-panel mi-panel">
        <header className="cockpit-panel-head">
          <div className="decision-flow-title">
            <Activity size={14} aria-hidden />
            <h3>Market Intelligence</h3>
          </div>
        </header>
        <p className="muted">MI layer disabled</p>
      </section>
    );
  }

  const vix = mi?.vix as Record<string, unknown> | undefined;
  const cePe = mi?.ce_pe as Record<string, unknown> | undefined;
  const breadth = mi?.breadth as Record<string, unknown> | undefined;
  const sectors = mi?.sectors as Record<string, unknown> | undefined;
  const early = mi?.early_bird as Record<string, unknown> | undefined;
  const reversal = mi?.reversal as Record<string, unknown> | undefined;
  const dq = mi?.data_quality as Record<string, unknown> | undefined;

  return (
    <section className="cockpit-panel mi-panel">
      <header className="cockpit-panel-head mi-panel-head">
        <div className="decision-flow-title">
          <Activity size={14} aria-hidden />
          <h3>Market Intelligence</h3>
        </div>
        {flow?.no_trade_code ? (
          <span className="decision-flow-badge tone-block">{flow.no_trade_code.replace(/_/g, " ")}</span>
        ) : (
          <span className="decision-flow-badge tone-ok">{String(mi?.version ?? "mi_v1")}</span>
        )}
      </header>

      <div className="mi-grid">
        <div className="mi-cell">
          <span className="mi-k">VIX</span>
          <strong className="mono">{fmtNum(vix?.vix_level as number)}</strong>
          <span className="mi-sub muted">{String(vix?.volatility_regime ?? "—")}</span>
        </div>
        <div className="mi-cell">
          <span className="mi-k">Breadth</span>
          <strong className="mono">{fmtPct((breadth?.advance_pct as number) ?? flow?.advance_pct)}</strong>
          <span className="mi-sub muted">{String(breadth?.source ?? "sector proxy")}</span>
        </div>
        <div className="mi-cell">
          <span className="mi-k">Sectors</span>
          <strong className="mono">{fmtPct((sectors?.sector_breadth_pct as number) ?? flow?.sector_breadth_pct)}</strong>
          <span className="mi-sub muted">
            +{String(sectors?.sectors_positive_count ?? "—")} / -{String(sectors?.sectors_negative_count ?? "—")}
          </span>
        </div>
        <div className="mi-cell">
          <span className="mi-k">CE / PE</span>
          <strong className="mono">
            {fmtNum((cePe?.ce_strength as number) ?? flow?.ce_strength, 0)} /{" "}
            {fmtNum((cePe?.pe_strength as number) ?? flow?.pe_strength, 0)}
          </strong>
          <span className="mi-sub muted">{String(cePe?.verdict ?? flow?.ce_pe_verdict ?? "—").replace(/_/g, " ")}</span>
        </div>
      </div>

      <div className="mi-flags">
        {early?.early_bird_active ? <span className="mi-flag tone-ok">Early-bird context</span> : null}
        {reversal?.reversal_risk_elevated ? (
          <span className="mi-flag tone-warn">Reversal risk elevated</span>
        ) : null}
        {dq?.state ? <span className="mi-flag tone-pending">{String(dq.state).replace(/_/g, " ")}</span> : null}
      </div>

      {ml?.model_available ? (
        <p className="mi-ml muted">
          ML shadow · P(profit) {fmtPct((ml.probability_profit as number) * 100)} ·{" "}
          {String(ml.HYPOTHETICAL_ML_DECISION ?? ml.would_trade_ml ? "TRADE" : "NO_TRADE")} · gate{" "}
          {ml.gate_enabled ? "on" : "off"}
        </p>
      ) : (
        <p className="mi-ml muted">ML shadow · {String(ml?.mode ?? "disabled")}</p>
      )}
    </section>
  );
}
