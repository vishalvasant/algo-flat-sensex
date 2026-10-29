import { useEffect, useMemo, useRef, useState } from "react";
import type { MarketSummary, Watchlist, WatchlistItem } from "../types";
import { COCKPIT_DUMMY, dummyOiChange, dummyOiChangePct, dummyVolume } from "../utils/cockpitDummyData";
import { describeFeedMode } from "../utils/feedMode";
import { useCommodityChain } from "../hooks/useCommodityChain";

function formatPrice(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function hasPrice(value: number | null | undefined): boolean {
  return value !== null && value !== undefined && !Number.isNaN(Number(value));
}

function formatOi(value: number | null | undefined) {
  if (value === null || value === undefined) return "–";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString("en-IN");
}

function formatIv(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return `${Number(value).toFixed(1)}%`;
}

function formatDelta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(3);
}

function formatTheta(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
  return Number(value).toFixed(2);
}

function formatOiChg(value: number) {
  const sign = value >= 0 ? "+" : "";
  if (Math.abs(value) >= 1000) return `${sign}${(value / 1000).toFixed(1)}K`;
  return `${sign}${value}`;
}

function formatVolume(value: number | null | undefined) {
  if (value === null || value === undefined) return "–";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString("en-IN");
}

function formatOiChgPct(value: number) {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

function OiChgCell({ strike, isCe }: { strike: number; isCe: boolean }) {
  const chg = dummyOiChange(strike, isCe);
  const pct = dummyOiChangePct(strike, isCe);
  const up = chg >= 0;
  return (
    <td className="mono num oi-chg-cell">
      <span className={up ? "positive" : "negative"}>{formatOiChg(chg)}</span>
      <span className={`oi-chg-pill ${up ? "up" : "down"}`}>{formatOiChgPct(pct)}</span>
    </td>
  );
}

function formatTs(ts: string | null | undefined) {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

interface StrikeRow {
  strike: number;
  isAtm: boolean;
  lotSize: number;
  ce?: WatchlistItem;
  pe?: WatchlistItem;
}

interface WatchlistPanelProps {
  watchlist: Watchlist | null;
  summary?: MarketSummary | null;
  activeCommodity: string;
}

export function WatchlistPanel({ watchlist, summary, activeCommodity }: WatchlistPanelProps) {
  const [activeExpiry, setActiveExpiry] = useState(0);
  const [chainView, setChainView] = useState<"oi" | "volume" | "greeks">("oi");
  const { selected, items, isRefreshing } = useCommodityChain(watchlist, summary, activeCommodity);
  const spot = selected?.spot_ltp ?? selected?.trading_spot_ltp ?? null;
  const atm = selected?.atm_strike ?? null;
  const expiry = selected?.expiry_symbol ?? null;
  const feedMode = watchlist?.feed_mode ?? summary?.feed_mode ?? "offline";
  const wsOpen = watchlist?.ws_open ?? summary?.ws_open;
  const feed = describeFeedMode(feedMode, wsOpen, watchlist?.ws_quote_age_sec ?? summary?.ws_quote_age_sec, watchlist?.quote_age_sec ?? summary?.quote_age_sec);
  const feedLabel = feed.label;
  const atmSteps = selected?.atm_strike_steps ?? watchlist?.atm_strike_steps ?? 5;
  const step = selected?.strike_step ?? watchlist?.strike_step ?? 50;
  const displayName = selected?.display_name ?? selected?.underlying ?? activeCommodity;

  const strikeRows = useMemo(() => {
    const byStrike = new Map<number, StrikeRow>();
    for (const item of items) {
      let row = byStrike.get(item.strike);
      if (!row) {
        row = {
          strike: item.strike,
          isAtm: item.is_atm,
          lotSize: item.lot_size ?? 65,
          ce: undefined,
          pe: undefined,
        };
        byStrike.set(item.strike, row);
      }
      row.lotSize = item.lot_size ?? row.lotSize;
      row.isAtm = row.isAtm || item.is_atm;
      if (item.option_type === "CE") row.ce = item;
      if (item.option_type === "PE") row.pe = item;
    }
    return [...byStrike.values()].sort((a, b) => b.strike - a.strike);
  }, [items]);

  const prevLtp = useRef<Record<string, number>>({});
  const [ltpFlash, setLtpFlash] = useState<Record<string, { dir: "up" | "down"; at: number }>>({});

  useEffect(() => {
    prevLtp.current = {};
    setLtpFlash({});
  }, [activeCommodity]);

  useEffect(() => {
    const next: Record<string, { dir: "up" | "down"; at: number }> = {};
    const now = Date.now();
    for (const item of items) {
      if (item.ltp == null || Number.isNaN(Number(item.ltp))) continue;
      const price = Number(item.ltp);
      const prev = prevLtp.current[item.token];
      if (prev != null && price !== prev) {
        next[item.token] = { dir: price > prev ? "up" : "down", at: now };
      }
      prevLtp.current[item.token] = price;
    }
    if (Object.keys(next).length === 0) return;
    setLtpFlash((cur) => ({ ...cur, ...next }));
    const timer = window.setTimeout(() => {
      setLtpFlash((cur) => {
        const cut = Date.now() - 650;
        const kept: Record<string, { dir: "up" | "down"; at: number }> = {};
        for (const [k, v] of Object.entries(cur)) {
          if (v.at >= cut) kept[k] = v;
        }
        return kept;
      });
    }, 700);
    return () => window.clearTimeout(timer);
  }, [items]);

  const ltpCellClass = (token: string | undefined, ltp: number | null | undefined) => {
    const base = hasPrice(ltp) ? "ltp" : "muted empty-ltp";
    if (!token) return base;
    const flash = ltpFlash[token];
    if (!flash) return base;
    return `${base} ltp-tick ltp-tick-${flash.dir}`;
  };

  return (
    <div className="card watchlist-card pro">
      <div className="chain-ref-toolbar">
        <div className="chain-expiry-tabs">
          {COCKPIT_DUMMY.chainExpiries.map((label, i) => (
            <button
              key={label}
              type="button"
              className={i === activeExpiry ? "chain-expiry active" : "chain-expiry"}
              onClick={() => setActiveExpiry(i)}
            >
              {label}
            </button>
          ))}
        </div>
        {spot != null ? (
          <span className="chain-spot-pill mono">
            Spot <strong className="accent">{spot.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</strong>
          </span>
        ) : null}
        <div className="chain-view-toggles">
          {(["oi", "volume", "greeks"] as const).map((v) => (
            <button
              key={v}
              type="button"
              className={chainView === v ? "chain-toggle active" : "chain-toggle"}
              onClick={() => setChainView(v)}
            >
              {v === "oi" ? "OI Change" : v === "volume" ? "Volume" : "Greeks"}
            </button>
          ))}
        </div>
      </div>

      <div className="watchlist-header watchlist-header--slim">
        <div>
          <h3>
            Option Chain ({displayName})
            <span className="watchlist-feed-tag">{feedLabel}</span>
            {feedMode === "websocket" || feedMode === "rest" ? (
              <span className="live-dot" aria-label="live" />
            ) : null}
          </h3>
          <p className="watchlist-sub">
            Weekly {expiry ?? "—"} · ATM ±{atmSteps} · step {step} · {strikeRows.length} strikes
          </p>
        </div>
        <div className="watchlist-meta watchlist-meta--compact">
          <div>
            <span className="meta-label">ATM</span>
            <span className="meta-value">{atm != null ? atm.toLocaleString("en-IN") : "—"}</span>
          </div>
          <div>
            <span className="meta-label">Tick</span>
            <span className="meta-value sm">{formatTs(watchlist?.last_quote_ts ?? null)}</span>
          </div>
        </div>
      </div>

      {strikeRows.length === 0 ? (
        <div className="empty-state pro">
          <p>Loading option chain…</p>
          <span>ATM ±{atmSteps} steps only · live WebSocket during market hours</span>
        </div>
      ) : (
        <div className={`watchlist-scroll${isRefreshing ? " watchlist-scroll--stale" : ""}`}>
          <table className="watchlist-table pro chain-compact chain-greeks">
            <thead>
              <tr className="chain-group-row">
                <th colSpan={6} className="ce-group">
                  CALLS (CE)
                </th>
                <th className="strike-group">Strike</th>
                <th colSpan={6} className="pe-group">
                  PUTS (PE)
                </th>
              </tr>
              <tr>
                <th className="num">OI</th>
                <th className="num">{chainView === "volume" ? "Vol" : "OI Chg"}</th>
                <th className="num">{chainView === "greeks" ? "IV" : chainView === "volume" ? "OI Chg" : "Vol"}</th>
                <th className="num">{chainView === "greeks" ? "Δ" : "IV"}</th>
                <th className="num">{chainView === "greeks" ? "θ" : "Δ"}</th>
                <th className="num">LTP</th>
                <th className="num strike-head">Strike</th>
                <th className="num">LTP</th>
                <th className="num">{chainView === "greeks" ? "Δ" : "Δ"}</th>
                <th className="num">{chainView === "greeks" ? "θ" : "IV"}</th>
                <th className="num">{chainView === "volume" ? "OI Chg" : chainView === "greeks" ? "IV" : "Vol"}</th>
                <th className="num">{chainView === "volume" ? "Vol" : "OI Chg"}</th>
                <th className="num">OI</th>
              </tr>
            </thead>
            <tbody>
              {strikeRows.map((row) => (
                  <tr
                    key={row.strike}
                    className={row.isAtm ? "watchlist-row tradable atm-row" : "watchlist-row"}
                  >
                    <td className="mono num muted">{formatOi(row.ce?.oi)}</td>
                    {chainView === "volume" ? (
                      <td className="mono num accent">{formatVolume(row.ce?.volume)}</td>
                    ) : chainView === "greeks" ? (
                      <td className="mono num muted">{formatIv(row.ce?.iv)}</td>
                    ) : (
                      <OiChgCell strike={row.strike} isCe />
                    )}
                    <td className="mono num muted">
                      {chainView === "greeks"
                        ? formatDelta(row.ce?.delta)
                        : chainView === "volume"
                          ? formatOiChg(dummyOiChange(row.strike, true))
                          : formatVolume(row.ce?.volume)}
                    </td>
                    <td className="mono num muted">
                      {chainView === "greeks" ? formatTheta(row.ce?.theta) : formatIv(row.ce?.iv)}
                    </td>
                    <td className="mono num muted">
                      {chainView === "greeks" ? formatIv(row.ce?.iv) : formatDelta(row.ce?.delta)}
                    </td>
                    <td className={`mono num ${ltpCellClass(row.ce?.token, row.ce?.ltp)}`}>
                      {formatPrice(row.ce?.ltp)}
                    </td>
                    <td className="mono num strike-cell">
                      <strong>{row.strike.toLocaleString("en-IN")}</strong>
                      {row.isAtm ? <span className="atm-pill">ATM</span> : null}
                    </td>
                    <td className={`mono num ${ltpCellClass(row.pe?.token, row.pe?.ltp)}`}>
                      {formatPrice(row.pe?.ltp)}
                    </td>
                    <td className="mono num muted">
                      {formatDelta(row.pe?.delta)}
                    </td>
                    <td className="mono num muted">
                      {chainView === "greeks" ? formatTheta(row.pe?.theta) : formatIv(row.pe?.iv)}
                    </td>
                    <td className="mono num muted">
                      {chainView === "greeks"
                        ? formatIv(row.pe?.iv)
                        : chainView === "volume"
                          ? formatOiChg(dummyOiChange(row.strike, false))
                          : formatOi(dummyVolume(row.strike + 1))}
                    </td>
                    {chainView === "volume" ? (
                      <td className="mono num accent">{formatVolume(row.pe?.volume)}</td>
                    ) : chainView === "greeks" ? (
                      <td className="mono num muted">{formatIv(row.pe?.iv)}</td>
                    ) : (
                      <OiChgCell strike={row.strike} isCe={false} />
                    )}
                    <td className="mono num muted">{formatOi(row.pe?.oi)}</td>
                  </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
