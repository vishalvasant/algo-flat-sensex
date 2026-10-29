import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Filter,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import { fetchDecisionLogs } from "../api/client";
import type { DecisionLogEvent } from "../types";
import { AppPageShell } from "../components/AppPageShell";
import { formatHumanDecision } from "../utils/decisionLogFormat";

const PAGE_SIZE = 25;

const FILTERS = [
  { id: "all", label: "All" },
  { id: "strategy_decision", label: "Decisions" },
  { id: "entry_confirm_wait", label: "Confirming" },
  { id: "entry_skipped", label: "Blocked" },
  { id: "manual_sync", label: "Sync" },
] as const;

function formatTs(ts: string) {
  try {
    const d = new Date(ts);
    const date = d.toLocaleDateString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour12: false,
      day: "2-digit",
      month: "short",
    });
    const time = d.toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    return { date, time, full: `${date} · ${time}` };
  } catch {
    return { date: "—", time: ts, full: ts };
  }
}

function eventTypeLabel(eventType: string): string {
  if (eventType === "strategy_decision") return "Decision";
  if (eventType === "entry_confirm_wait") return "Confirming";
  if (eventType === "entry_skipped") return "Blocked";
  if (eventType === "manual_sync") return "Sync";
  return eventType.replace(/_/g, " ");
}

function feedSeverityClass(ev: DecisionLogEvent): string {
  if (ev.event_type === "entry_skipped") return "sev-warning";
  if (ev.event_type === "entry_confirm_wait") return "sev-warning";
  if (ev.metadata?.execution_status === "blocked") return "sev-warning";
  if (ev.metadata?.execution_status === "confirm_wait") return "sev-warning";
  if (ev.metadata?.trade_allowed === true) return "sev-success";
  if (ev.severity === "critical") return "sev-critical";
  if (ev.severity === "warning") return "sev-warning";
  if (ev.severity === "success") return "sev-success";
  return "";
}

export function DecisionLogsPage() {
  const [events, setEvents] = useState<DecisionLogEvent[]>([]);
  const [intervalSec, setIntervalSec] = useState(10);
  const [marketOpen, setMarketOpen] = useState(false);
  const [marketSession, setMarketSession] = useState("CLOSED");
  const [istTime, setIstTime] = useState<string | null>(null);
  const [engineStatus, setEngineStatus] = useState("unknown");
  const [lastScanAt, setLastScanAt] = useState<string | null>(null);
  const [todayCount, setTodayCount] = useState(0);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<string>("all");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [tick, setTick] = useState(0);
  const [newEventPulse, setNewEventPulse] = useState(false);

  const topEventIdRef = useRef<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const offset = (page - 1) * PAGE_SIZE;

  const rangeLabel = useMemo(() => {
    if (total === 0) return "0 records";
    const from = offset + 1;
    const to = Math.min(offset + events.length, total);
    return `${from}–${to} of ${total}`;
  }, [events.length, offset, total]);

  const refreshedAgo = useMemo(() => {
    if (!lastRefreshedAt) return null;
    const sec = Math.max(0, Math.floor((Date.now() - lastRefreshedAt.getTime()) / 1000));
    if (sec < 5) return "just now";
    if (sec < 60) return `${sec}s ago`;
    return `${Math.floor(sec / 60)}m ago`;
  }, [lastRefreshedAt, tick]);

  const load = useCallback(
    (opts?: { silent?: boolean }) => {
      const silent = opts?.silent ?? false;
      if (!silent) setBusy(true);
      const type = filter === "all" ? undefined : filter;
      fetchDecisionLogs({ limit: PAGE_SIZE, offset, eventType: type })
        .then((res) => {
          const topId = res.events[0]?.id ?? null;
          if (
            page === 1 &&
            topEventIdRef.current &&
            topId &&
            topId !== topEventIdRef.current
          ) {
            setNewEventPulse(true);
            window.setTimeout(() => setNewEventPulse(false), 2000);
          }
          topEventIdRef.current = topId;

          setEvents(res.events);
          setIntervalSec(res.scan_interval_seconds);
          setMarketOpen(res.market_open ?? false);
          setMarketSession(res.market_session ?? "CLOSED");
          setIstTime(res.ist_time ?? null);
          setEngineStatus(res.engine_status ?? "unknown");
          setLastScanAt(res.last_scan_at ?? null);
          setTodayCount(res.decisions_today);
          setTotal(res.total);
          setLastRefreshedAt(new Date());
          setError(null);
        })
        .catch((e) => setError(String(e)))
        .finally(() => {
          if (!silent) setBusy(false);
        });
    },
    [filter, offset, page],
  );

  useEffect(() => {
    load();
  }, [load]);

  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    const ms = Math.max(5000, intervalSec * 1000);
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => {
      loadRef.current({ silent: true });
    }, ms);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [intervalSec]);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const goPrev = () => setPage((p) => Math.max(1, p - 1));
  const goNext = () => setPage((p) => Math.min(totalPages, p + 1));

  const setFilterAndReset = (id: string) => {
    setFilter(id);
    setPage(1);
    topEventIdRef.current = null;
  };

  return (
    <AppPageShell
      title="Decision Logs"
      icon={ScrollText}
      description="Human-readable audit of scans, skips, and syncs"
    >
      <div className="logs-page-full">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Filter size={14} strokeWidth={2} />
            <h3>Overview</h3>
            <span className="logs-live-pill mono muted">
              {refreshedAgo ? `Updated ${refreshedAgo}` : "—"}
              {newEventPulse ? " · new" : ""}
            </span>
            <button
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={() => load()}
              disabled={busy}
              type="button"
            >
              <RefreshCw size={13} className={busy ? "spin" : ""} />
              {busy ? "Loading…" : "Refresh"}
            </button>
          </header>

          <div className="cockpit-command-metrics logs-command-metrics">
            <div className="cmd-metric">
              <span>Scan interval</span>
              <strong>{intervalSec}s</strong>
            </div>
            <div className="cmd-metric">
              <span>Engine</span>
              <strong>{engineStatus}</strong>
            </div>
            <div className="cmd-metric">
              <span>IST now</span>
              <strong>{istTime ? formatTs(istTime).full : "—"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Session</span>
              <strong>{marketSession}</strong>
            </div>
            <div className="cmd-metric">
              <span>Market</span>
              <strong>{marketOpen ? "open" : "closed"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Last scan</span>
              <strong>{lastScanAt ? formatTs(lastScanAt).full : "—"}</strong>
            </div>
            <div className="cmd-metric">
              <span>Decisions today</span>
              <strong>{todayCount}</strong>
            </div>
            <div className="cmd-metric">
              <span>Matching filter</span>
              <strong>{total}</strong>
            </div>
            <div className="cmd-metric">
              <span>Page</span>
              <strong>
                {page} / {totalPages}
              </strong>
            </div>
          </div>

          <div className="logs-filter-row">
            <span className="logs-filter-label">Filter</span>
            <div className="chart-interval-tabs logs-filter-tabs" role="tablist" aria-label="Log type">
              {FILTERS.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  role="tab"
                  aria-selected={filter === f.id}
                  className={`chart-interval ${filter === f.id ? "active" : ""}`}
                  onClick={() => setFilterAndReset(f.id)}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        <section
          className={`cockpit-panel logs-feed-panel orderbook-table-panel${newEventPulse ? " logs-feed-pulse" : ""}`}
        >
          <header className="cockpit-panel-head">
            <ScrollText size={14} strokeWidth={2} />
            <h3>Event log</h3>
            <span className="logs-range-pill mono muted">{rangeLabel}</span>
            <span className="logs-range-pill mono muted">auto {intervalSec}s</span>
          </header>

          {events.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy
                ? "Loading events…"
                : "No logs yet. They appear once the market is open and the scanner runs."}
            </p>
          ) : (
            <div className="page-table-scroll logs-table-scroll">
              <table className="logs-table cockpit-table pro">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Type</th>
                    <th>Event</th>
                    <th>Summary</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((ev) => {
                    const human = formatHumanDecision(ev);
                    const ts = formatTs(ev.ts);
                    const type = eventTypeLabel(ev.event_type);
                    return (
                      <tr
                        key={ev.id}
                        className={`logs-table-row ${feedSeverityClass(ev)}`.trim()}
                      >
                        <td className="mono logs-col-time" title={ts.full}>
                          <span className="logs-time-date">{ts.date}</span>
                          <span className="logs-time-clock">{ts.time}</span>
                        </td>
                        <td>
                          <span
                            className={`logs-type-pill type-${ev.event_type} ${feedSeverityClass(ev)}`.trim()}
                          >
                            {type}
                          </span>
                        </td>
                        <td className="logs-col-event">{human.title}</td>
                        <td className="logs-col-summary">{human.summary || "—"}</td>
                        <td className="logs-col-detail muted">{human.detail || "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <nav className="logs-pagination logs-pagination--inline" aria-label="Decision log pages">
            <span className="logs-pagination-range muted">{rangeLabel}</span>
            <div className="logs-pagination-actions">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={goPrev}
                disabled={page <= 1}
              >
                <ChevronLeft size={14} />
                Prev
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={goNext}
                disabled={page >= totalPages}
              >
                Next
                <ChevronRight size={14} />
              </button>
            </div>
          </nav>
        </section>
      </div>
    </AppPageShell>
  );
}
