import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { fetchHealth, fetchMarketSummary } from "../api/client";
import { IndexCandleChart, type ChartInterval } from "../components/IndexCandleChart";
import { useDashboardOutlet } from "../components/Layout";
import { COCKPIT_REFRESH_EVENT } from "../components/CockpitEngineControls";
import { AtmNearCards } from "../components/cockpit/AtmNearCards";
import { DecisionFlowPanel } from "../components/cockpit/DecisionFlowPanel";
import { MarketIntelligencePanel } from "../components/cockpit/MarketIntelligencePanel";
import { DecisionLogFeed } from "../components/cockpit/DecisionLogFeed";
import { PositionSummaryPanel } from "../components/cockpit/PositionSummaryPanel";
import { RiskManagerPanel } from "../components/cockpit/RiskManagerPanel";
import { StrategyInsightPanel } from "../components/cockpit/StrategyInsightPanel";
import { useStableOpenPositions } from "../hooks/useStableOpenPositions";
import { useResponsiveChartHeight } from "../hooks/useResponsiveChartHeight";
import type { CommoditySnapshot, Watchlist, WatchlistItem, WatchlistOpenPosition } from "../types";

type ChartPickMode = "auto" | "manual";

function positionKey(pos: WatchlistOpenPosition): string {
  return String(pos.instrument_token || pos.position_id || pos.tsym);
}

function optionSideFromPosition(pos: WatchlistOpenPosition): "CE" | "PE" {
  if (pos.side === "CE" || pos.side === "PE") return pos.side;
  if (pos.tsym.includes("CE")) return "CE";
  if (pos.tsym.includes("PE")) return "PE";
  return "CE";
}

function positionMatchesUnderlying(pos: WatchlistOpenPosition, underlying: string): boolean {
  const ul = underlying.toUpperCase();
  const tsym = (pos.tsym || "").toUpperCase();
  if (!tsym) return true;
  // Options are tagged like NIFTY04AUG26C24350 — ignore _FUT ticker rows.
  if (ul.endsWith("_FUT")) return tsym.startsWith(ul.replace(/_FUT$/, ""));
  return tsym.startsWith(ul);
}

function parseStrikeFromTsym(tsym: string): number | null {
  const m = tsym.match(/(\d{4,6})(?:CE|PE)$/i);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

function findChainItem(
  pos: WatchlistOpenPosition,
  commodities: CommoditySnapshot[],
  watchlist: Watchlist | null,
  underlying: string,
): WatchlistItem | null {
  const token = String(pos.instrument_token || "").trim();
  const tsym = pos.tsym;
  const pools: WatchlistItem[] = [];
  const commodity = commodities.find((c) => c.underlying === underlying);
  if (commodity?.items?.length) pools.push(...commodity.items);
  if (watchlist?.items?.length) pools.push(...watchlist.items);

  if (token) {
    const byToken = pools.find((item) => item.token === token);
    if (byToken) return byToken;
  }
  if (tsym) {
    const byTsym = pools.find((item) => item.tsym === tsym);
    if (byTsym) return byTsym;
  }
  return null;
}

function itemFromPosition(pos: WatchlistOpenPosition): WatchlistItem | null {
  const token = String(pos.instrument_token || "").trim();
  if (!token && !pos.tsym) return null;
  const side = optionSideFromPosition(pos);
  return {
    token: token || pos.tsym,
    tsym: pos.tsym,
    strike: parseStrikeFromTsym(pos.tsym) ?? 0,
    option_type: side,
    is_atm: false,
    tradable: true,
    lot_size: pos.lot_size,
    ltp: pos.current_ltp ?? null,
    bid: null,
    ask: null,
    volume: null,
    oi: null,
    last_update_ts: pos.last_tick_ts ?? null,
  };
}

function resolveOptionForPosition(
  pos: WatchlistOpenPosition,
  commodities: CommoditySnapshot[],
  watchlist: Watchlist | null,
  underlying: string,
): WatchlistItem | null {
  return (
    findChainItem(pos, commodities, watchlist, underlying) ?? itemFromPosition(pos)
  );
}

export function DashboardPage() {
  const { activeCommodity, watchlist, summary } = useDashboardOutlet();
  const [chartInterval, setChartInterval] = useState<ChartInterval>("5m");
  const [selectedOption, setSelectedOption] = useState<WatchlistItem | null>(null);
  const [pickMode, setPickMode] = useState<ChartPickMode>("auto");
  const [error, setError] = useState<string | null>(null);
  const seenPositionKeysRef = useRef<Set<string>>(new Set());
  const positionsReadyRef = useRef(false);

  const commodities =
    watchlist?.commodities ?? summary?.commodities ?? [
      { underlying: "SENSEX", display_name: "SENSEX", spot_ltp: null, atm_strike: null },
    ];

  const selectedCommodity =
    commodities.find((c) => c.underlying === activeCommodity) ?? commodities[0];
  const futSpot =
    selectedCommodity?.trading_spot_ltp ?? selectedCommodity?.spot_ltp ?? null;

  const { positions: openPositions, live: positionStreamLive } = useStableOpenPositions(
    watchlist,
    summary,
  );
  const chartHeight = useResponsiveChartHeight();

  useEffect(() => {
    setSelectedOption(null);
    setPickMode("auto");
    seenPositionKeysRef.current = new Set();
    positionsReadyRef.current = false;
  }, [activeCommodity]);

  // Auto-open chart for open positions; force-switch on new buys; allow manual override.
  useEffect(() => {
    const relevant = openPositions.filter((p) =>
      positionMatchesUnderlying(p, activeCommodity),
    );
    const currentKeys = new Set(relevant.map(positionKey));
    const prevKeys = seenPositionKeysRef.current;
    const newBuys = relevant.filter((p) => !prevKeys.has(positionKey(p)));
    seenPositionKeysRef.current = currentKeys;

    const applyItem = (item: WatchlistItem | null) => {
      setSelectedOption((prev) => {
        if (!item) return prev == null ? prev : null;
        if (prev?.token === item.token && prev?.tsym === item.tsym) return prev;
        return item;
      });
    };

    if (!relevant.length) {
      if (pickMode === "auto") applyItem(null);
      positionsReadyRef.current = true;
      return;
    }

    if (positionsReadyRef.current && newBuys[0]) {
      // Fresh fill / adopt — always jump to that contract chart.
      const item = resolveOptionForPosition(
        newBuys[0],
        commodities,
        watchlist,
        activeCommodity,
      );
      if (item) {
        setPickMode("auto");
        applyItem(item);
      }
    } else if (pickMode === "auto") {
      const item = resolveOptionForPosition(
        relevant[0],
        commodities,
        watchlist,
        activeCommodity,
      );
      if (item) applyItem(item);
    }

    positionsReadyRef.current = true;
  }, [openPositions, activeCommodity, pickMode, commodities, watchlist]);

  const handleSelectOption = useCallback((item: WatchlistItem | null) => {
    // Explicit card tap — user override. Null = back to the SENSEX index chart.
    setPickMode("manual");
    setSelectedOption(item);
  }, []);

  // Keep selected option LTP fresh from the live watchlist stream.
  const liveSelectedOption = useMemo(() => {
    if (!selectedOption?.token) return null;
    const token = selectedOption.token;
    const commodity = commodities.find((c) => c.underlying === activeCommodity);
    const fromCommodity = commodity?.items?.find((item) => item.token === token);
    if (fromCommodity) return fromCommodity;
    const fromWatchlist = watchlist?.items?.find((item) => item.token === token);
    if (fromWatchlist) return fromWatchlist;
    // Fall back to open-position LTP when the strike scrolled out of ATM±2 cards.
    const fromPos = openPositions.find(
      (p) =>
        String(p.instrument_token || "") === token ||
        p.tsym === selectedOption.tsym,
    );
    if (fromPos) {
      return {
        ...selectedOption,
        ltp: fromPos.current_ltp ?? selectedOption.ltp,
      };
    }
    return selectedOption;
  }, [selectedOption, watchlist, commodities, activeCommodity, openPositions]);

  const chartLiveSpot = liveSelectedOption?.ltp ?? futSpot;
  const chartDisplayName = liveSelectedOption
    ? liveSelectedOption.tsym
    : (selectedCommodity?.display_name ?? "SENSEX");

  const load = useCallback(() => {
    fetchHealth()
      .then((h) => setError(h.error ?? null))
      .catch((e) => setError(String(e)));
    fetchMarketSummary().catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    const intervalMs = positionStreamLive
      ? openPositions.length
        ? 8000
        : 5000
      : openPositions.length
        ? 2000
        : 5000;
    const healthId = setInterval(load, intervalMs);
    const onRefresh = () => load();
    window.addEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    return () => {
      clearInterval(healthId);
      window.removeEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    };
  }, [load, openPositions.length, positionStreamLive]);

  const feedMode = summary?.feed_mode ?? watchlist?.feed_mode ?? "offline";

  return (
    <div className="terminal-cockpit terminal-cockpit--no-bottom">
      {error ? (
        <div className="error-banner">
          <AlertTriangle size={16} />
          {error}
        </div>
      ) : null}

      <section className="cockpit-main cockpit-main-ref">
        <main className="cockpit-col cockpit-col-center">
          <IndexCandleChart
            underlying={activeCommodity}
            displayName={chartDisplayName}
            liveSpot={chartLiveSpot}
            watchlist={watchlist}
            feedMode={watchlist?.feed_mode ?? feedMode}
            height={chartHeight}
            compact
            interval={chartInterval}
            onIntervalChange={setChartInterval}
            levels={liveSelectedOption ? null : (summary?.levels ?? null)}
            instrumentToken={liveSelectedOption?.token ?? null}
            instrumentTsym={liveSelectedOption?.tsym ?? null}
            instrumentExchange={liveSelectedOption?.exchange ?? "NFO"}
          />
          <AtmNearCards
            watchlist={watchlist}
            summary={summary}
            activeCommodity={activeCommodity}
            selectedToken={liveSelectedOption?.token ?? null}
            onSelectOption={handleSelectOption}
          />
        </main>

        <aside className="cockpit-col cockpit-col-right">
          <DecisionFlowPanel summary={summary} />
          <MarketIntelligencePanel summary={summary} />
          <StrategyInsightPanel
            summary={summary}
            watchlist={watchlist}
            activeUnderlying={activeCommodity}
            commodities={commodities}
          />
          <PositionSummaryPanel
            openPositions={openPositions}
            summary={summary}
            onRefresh={load}
            live={positionStreamLive}
          />
          <RiskManagerPanel summary={summary} onRefresh={load} />
          <DecisionLogFeed />
        </aside>
      </section>
    </div>
  );
}
