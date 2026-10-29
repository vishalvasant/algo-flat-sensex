import { GitBranch } from "lucide-react";
import type { DecisionFlowStageStatus, MarketSummary } from "../../types";
import { describeFeedMode } from "../../utils/feedMode";
import { formatBasisPoints, formatIndexPrice } from "../../utils/format";

interface DecisionFlowPanelProps {
  summary: MarketSummary | null;
}

function statusLabel(status: DecisionFlowStageStatus): string {
  switch (status) {
    case "ok":
      return "Ready";
    case "warn":
      return "Caution";
    case "block":
      return "Blocked";
    case "pending":
      return "Waiting";
    default:
      return "Idle";
  }
}

function formatBasis(basis: number | null | undefined, limit: number | undefined) {
  return formatBasisPoints(basis, limit);
}

export function DecisionFlowPanel({ summary }: DecisionFlowPanelProps) {
  const flow = summary?.decision_flow;
  const stages = flow?.stages ?? [];
  const feed = describeFeedMode(
    summary?.feed_mode,
    summary?.ws_open,
    summary?.ws_quote_age_sec,
    summary?.quote_age_sec,
  );
  const alignment = summary?.spot_fut?.spot_fut_alignment;

  return (
    <section className="cockpit-panel decision-flow-panel">
      <header className="cockpit-panel-head decision-flow-head">
        <div className="decision-flow-title">
          <GitBranch size={14} aria-hidden />
          <h3>Decision Flow</h3>
        </div>
        {flow?.basis_ok === false ? (
          <span className="decision-flow-badge tone-block">Basis wide</span>
        ) : flow?.momentum_aligned === false ? (
          <span className="decision-flow-badge tone-warn">Momentum mixed</span>
        ) : (
          <span className={`decision-flow-badge tone-${feed.tone === "live" ? "ok" : feed.tone === "backup" ? "warn" : "block"}`}>
            {feed.label}
          </span>
        )}
      </header>

      <div className="decision-flow-metrics">
        <div className="decision-flow-metric">
          <span className="decision-flow-k">SENSEX</span>
          <strong className="mono">
            {formatIndexPrice(summary?.cash_index_spot ?? summary?.spot_ltp)}
          </strong>
        </div>
        <div className="decision-flow-metric">
          <span className="decision-flow-k">Basis</span>
          <strong className={`mono tone-${flow?.basis_ok === false ? "block" : "ok"}`}>
            {formatBasis(summary?.basis_points, flow?.basis_limit_points)}
          </strong>
        </div>
        <div className="decision-flow-metric">
          <span className="decision-flow-k">Align</span>
          <strong className="mono">{alignment ? String(alignment).replace(/_/g, " ") : "—"}</strong>
        </div>
      </div>

      <p className="decision-flow-feed-hint muted">{feed.detail}</p>

      <ol className="decision-flow-stages" aria-label="Trade decision pipeline">
        {stages.length === 0 ? (
          <li className="decision-flow-stage tone-pending">
            <span className="decision-flow-stage-label">Pipeline</span>
            <span className="decision-flow-stage-detail muted">Waiting for first scan…</span>
          </li>
        ) : (
          stages.map((stage, index) => (
            <li
              key={stage.id}
              className={`decision-flow-stage tone-${stage.status}${index < stages.length - 1 ? " has-connector" : ""}`}
            >
              <div className="decision-flow-stage-marker" aria-hidden />
              <div className="decision-flow-stage-body">
                <div className="decision-flow-stage-top">
                  <span className="decision-flow-stage-label">{stage.label}</span>
                  <span className={`decision-flow-stage-status tone-${stage.status}`}>
                    {statusLabel(stage.status)}
                  </span>
                </div>
                <p className="decision-flow-stage-detail">{stage.detail}</p>
              </div>
            </li>
          ))
        )}
      </ol>
    </section>
  );
}
