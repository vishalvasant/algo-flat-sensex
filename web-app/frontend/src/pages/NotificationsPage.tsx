import { useCallback, useEffect, useState } from "react";
import { Bell, CheckCheck, RefreshCw } from "lucide-react";
import { fetchNotifications, markNotificationRead } from "../api/client";
import type { Notification } from "../types";
import { AppPageShell } from "../components/AppPageShell";

function formatTs(ts: string) {
  try {
    const d = new Date(ts);
    const date = d.toLocaleDateString("en-IN", {
      timeZone: "Asia/Kolkata",
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

function severityClass(severity: string): string {
  if (severity === "critical" || severity === "error") return "sev-critical";
  if (severity === "warning") return "sev-warning";
  if (severity === "success") return "sev-success";
  return "";
}

function severityLabel(severity: string): string {
  if (severity === "critical" || severity === "error") return "Critical";
  if (severity === "warning") return "Warning";
  if (severity === "success") return "Success";
  return "Info";
}

export function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [markingAll, setMarkingAll] = useState(false);

  const load = useCallback((opts?: { silent?: boolean }) => {
    if (!opts?.silent) setBusy(true);
    fetchNotifications(50)
      .then((rows) => {
        setItems(rows);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => {
        if (!opts?.silent) setBusy(false);
      });
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(() => load({ silent: true }), 8000);
    return () => clearInterval(id);
  }, [load]);

  const markRead = async (id: string) => {
    try {
      await markNotificationRead(id);
      setItems((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)));
    } catch (e) {
      setError(String(e));
    }
  };

  const markAllRead = async () => {
    const unreadIds = items.filter((n) => !n.read).map((n) => n.id);
    if (!unreadIds.length) return;
    setMarkingAll(true);
    try {
      await Promise.all(unreadIds.map((id) => markNotificationRead(id)));
      setItems((prev) => prev.map((n) => ({ ...n, read: true })));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setMarkingAll(false);
    }
  };

  const unread = items.filter((n) => !n.read).length;

  return (
    <AppPageShell
      title="Alerts"
      icon={Bell}
      description={
        unread > 0
          ? `Entries, exits, and system events · ${unread} unread`
          : "Entries, exits, and system events"
      }
    >
      <div className="logs-page-full notifications-page">
        <section className="cockpit-panel logs-stats-panel">
          <header className="cockpit-panel-head logs-stats-head">
            <Bell size={14} strokeWidth={2} />
            <h3>Overview</h3>
            <button
              type="button"
              className="btn btn-ghost btn-sm logs-refresh-btn"
              onClick={() => load()}
              disabled={busy}
            >
              <RefreshCw size={13} className={busy ? "spin" : undefined} />
              {busy ? "Loading…" : "Refresh"}
            </button>
          </header>

          <div className="cockpit-command-metrics logs-command-metrics">
            <div className="cmd-metric">
              <span>Unread</span>
              <strong>{unread}</strong>
            </div>
            <div className="cmd-metric">
              <span>Loaded</span>
              <strong>{items.length}</strong>
            </div>
            <div className="cmd-metric">
              <span>Auto-refresh</span>
              <strong>8s</strong>
            </div>
          </div>
        </section>

        {error ? <div className="error-banner">{error}</div> : null}

        <section className="cockpit-panel logs-feed-panel notifications-feed-panel orderbook-table-panel">
          <header className="cockpit-panel-head">
            <Bell size={14} strokeWidth={2} />
            <h3>Alert log</h3>
            <span className="logs-range-pill mono muted">
              {unread ? `${unread} unread` : "All read"}
            </span>
            {unread > 0 ? (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={markAllRead}
                disabled={markingAll}
                title="Mark all alerts as read"
              >
                <CheckCheck size={13} />
                {markingAll ? "Marking…" : "Mark all read"}
              </button>
            ) : null}
          </header>

          {items.length === 0 ? (
            <p className="blotter-empty decision-log-empty">
              {busy ? "Loading alerts…" : "No alerts yet"}
            </p>
          ) : (
            <div className="page-table-scroll logs-table-scroll">
              <table className="logs-table alerts-table cockpit-table pro">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Severity</th>
                    <th>Title</th>
                    <th>Message</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((n) => {
                    const ts = formatTs(n.ts);
                    return (
                      <tr
                        key={n.id}
                        className={`logs-table-row alerts-row ${n.read ? "read" : "unread"} ${severityClass(n.severity)}`.trim()}
                        onClick={() => {
                          if (!n.read) void markRead(n.id);
                        }}
                        onKeyDown={(e) => {
                          if (!n.read && (e.key === "Enter" || e.key === " ")) {
                            e.preventDefault();
                            void markRead(n.id);
                          }
                        }}
                        role={n.read ? undefined : "button"}
                        tabIndex={n.read ? undefined : 0}
                        title={n.read ? undefined : "Click to mark as read"}
                      >
                        <td className="mono logs-col-time" title={ts.full}>
                          <span className="logs-time-date">{ts.date}</span>
                          <span className="logs-time-clock">{ts.time}</span>
                        </td>
                        <td>
                          <span
                            className={`logs-type-pill ${severityClass(n.severity)}`.trim()}
                          >
                            {severityLabel(n.severity)}
                          </span>
                        </td>
                        <td className="logs-col-event">{n.title}</td>
                        <td className="logs-col-summary">{n.message || "—"}</td>
                        <td>
                          <span className={`alert-read-pill ${n.read ? "read" : "unread"}`}>
                            {n.read ? "Read" : "Unread"}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppPageShell>
  );
}
