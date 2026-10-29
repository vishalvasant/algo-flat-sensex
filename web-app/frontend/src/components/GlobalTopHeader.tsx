import { Menu } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { CommoditySnapshot } from "../types";
import { useAuth } from "../auth/AuthContext";
import { describeFeedMode } from "../utils/feedMode";

export interface IndexQuote extends CommoditySnapshot {
  change?: number | null;
  changePct?: number | null;
}

interface GlobalTopHeaderProps {
  commodities: IndexQuote[];
  active: string;
  onChange: (underlying: string) => void;
  brokerConnected?: boolean;
  brokerName?: string;
  clock?: string;
  marketOpen?: boolean;
  marketSession?: string | null;
  feedMode?: string | null;
  wsOpen?: boolean | null;
  wsQuoteAgeSec?: number | null;
  quoteAgeSec?: number | null;
  onMenuToggle?: () => void;
  engineControls?: ReactNode;
}

const TRADEABLE = new Set(["SENSEX"]);

function formatPrice(spot: number | null | undefined) {
  if (spot == null) return "—";
  return Number(spot).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatChange(
  change: number | null | undefined,
  pct: number | null | undefined,
) {
  if (change == null || pct == null) {
    return { pts: "—", pct: "", up: true, flat: true };
  }
  const up = change >= 0;
  const sign = up ? "+" : "−";
  return {
    pts: `${sign}${Math.abs(change).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`,
    pct: `${sign}${Math.abs(pct).toFixed(2)}%`,
    up,
    flat: change === 0,
  };
}

function shortName(c: IndexQuote): string {
  const name = c.display_name ?? c.underlying;
  if (c.underlying === "INDIAVIX" || name.toUpperCase().includes("VIX")) return "VIX";
  if (c.underlying.endsWith("_FUT") || c.card_type === "fut") return c.underlying.replace("_FUT", " FUT");
  if (c.underlying === "SENSEX") return "SENSEX";
  if (c.underlying === "NIFTY") return "NIFTY 50";
  return name;
}

function feedLabel(
  feedMode?: string | null,
  marketOpen?: boolean,
  wsOpen?: boolean | null,
  wsQuoteAgeSec?: number | null,
  quoteAgeSec?: number | null,
): string {
  if (!marketOpen) return "REST";
  return describeFeedMode(feedMode, wsOpen, wsQuoteAgeSec, quoteAgeSec).label;
}

export function GlobalTopHeader({
  commodities,
  active,
  onChange,
  brokerConnected,
  brokerName = "Upstox",
  clock,
  marketOpen = false,
  marketSession,
  feedMode,
  wsOpen,
  wsQuoteAgeSec,
  quoteAgeSec,
  onMenuToggle,
  engineControls,
}: GlobalTopHeaderProps) {
  const { username } = useAuth();
  const initials = (username ?? "AF").slice(0, 2).toUpperCase();
  const prevPrices = useRef<Record<string, number>>({});
  const [priceFlash, setPriceFlash] = useState<Record<string, "up" | "down">>({});

  useEffect(() => {
    const nextFlash: Record<string, "up" | "down"> = {};
    for (const c of commodities) {
      const spot = c.spot_ltp;
      if (spot == null) continue;
      const prev = prevPrices.current[c.underlying];
      if (prev != null && prev !== spot) {
        nextFlash[c.underlying] = spot > prev ? "up" : "down";
      }
      prevPrices.current[c.underlying] = spot;
    }
    if (Object.keys(nextFlash).length === 0) return;
    setPriceFlash((prev) => ({ ...prev, ...nextFlash }));
    const id = window.setTimeout(() => {
      setPriceFlash((prev) => {
        const cleaned = { ...prev };
        for (const key of Object.keys(nextFlash)) delete cleaned[key];
        return cleaned;
      });
    }, 650);
    return () => window.clearTimeout(id);
  }, [commodities]);

  const sessionLabel = (marketSession || (marketOpen ? "OPEN" : "CLOSED")).toUpperCase();
  const feedView = describeFeedMode(feedMode, wsOpen, wsQuoteAgeSec, quoteAgeSec);
  const feed = feedLabel(feedMode, marketOpen, wsOpen, wsQuoteAgeSec, quoteAgeSec);

  return (
    <header className="global-top-header">
      <div className="global-top-brand">
        {onMenuToggle ? (
          <button
            type="button"
            className="global-menu-btn"
            onClick={onMenuToggle}
            aria-label="Toggle navigation"
          >
            <Menu size={18} />
          </button>
        ) : null}
        <div className="global-top-brand-text">
          <span className="global-top-logo">Algo-Flat Sensex</span>
          <span className={`global-session-chip ${marketOpen ? "open" : "closed"}`}>
            <i className="global-session-dot" />
            {sessionLabel}
          </span>
        </div>
      </div>

      <div className="global-top-center">
        <div className="global-top-indices" role="list">
          {commodities.map((c) => {
            const isActive = c.underlying === active;
            const tradeable = TRADEABLE.has(c.underlying);
            const isFut = c.card_type === "fut" || c.underlying.endsWith("_FUT");
            const chg = formatChange(c.change, c.changePct);
            const flash = priceFlash[c.underlying];
            const title = isFut && c.fut_tsym
              ? c.fut_tsym
              : tradeable
                ? `Trade ${shortName(c)}`
                : "Display only";

            return (
              <button
                key={c.underlying}
                type="button"
                role="listitem"
                className={[
                  "global-index-card",
                  isActive ? "active" : "",
                  tradeable ? "" : "display-only",
                  isFut ? "fut-card" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => tradeable && onChange(c.underlying)}
                disabled={!tradeable}
                title={title}
                aria-pressed={tradeable ? isActive : undefined}
              >
                <span className="global-index-top">
                  <span className="global-index-name">{shortName(c)}</span>
                  {c.expiry_label ? (
                    <span className="global-index-expiry mono">{c.expiry_label}</span>
                  ) : null}
                </span>
                <span
                  className={[
                    "global-index-price mono",
                    flash === "up" ? "price-tick-up" : "",
                    flash === "down" ? "price-tick-down" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {formatPrice(c.spot_ltp)}
                </span>
                <span
                  className={[
                    "global-index-chg mono",
                    chg.flat ? "flat" : chg.up ? "up" : "down",
                  ].join(" ")}
                >
                  <span className="chg-pts">{chg.pts}</span>
                  {chg.pct ? <span className="chg-pct">{chg.pct}</span> : null}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="global-top-meta">
        <div className="global-status-cluster" title={`${brokerName} · ${feedView.detail}`}>
          <span className="global-status-row">
            <i className={`broker-dot ${brokerConnected ? "on" : "off"}`} />
            <span className="meta-v">{brokerName}</span>
            <span className="global-meta-sep">·</span>
            <span className={`meta-feed ${feedView.tone === "live" ? "live" : ""}`}>{feed}</span>
          </span>
          {clock ? (
            <span className="global-meta-clock mono" title="Asia/Kolkata">
              {clock}
              <span className="clock-tz"> IST</span>
            </span>
          ) : null}
        </div>

        {engineControls}

        <span className="global-avatar" title={username ?? "User"}>
          {initials}
        </span>
      </div>
    </header>
  );
}
