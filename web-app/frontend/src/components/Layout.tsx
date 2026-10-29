import { Outlet, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { fetchHealth, fetchMarketSummary, fetchWatchlist, openWatchlistStream } from "../api/client";
import type { EngineHealth, MarketSummary, Watchlist } from "../types";
import { MarketStatsPanel } from "./cockpit/MarketStatsPanel";
import { GlobalTopHeader } from "./GlobalTopHeader";
import { CockpitEngineControls } from "./CockpitEngineControls";
import { RefSidebarFooter, RefSidebarNav } from "./RefSidebarNav";
import { useIndexQuotes } from "../hooks/useIndexQuotes";
import { COCKPIT_REFRESH_EVENT } from "./CockpitEngineControls";
import { mergeHeaderCommodities } from "../utils/headerCommodities";
import { brokerDisplayName } from "../utils/broker";
import { formatIstClock } from "../utils/format";

export interface DashboardOutletContext {
  activeCommodity: string;
  setActiveCommodity: (v: string) => void;
  watchlist: Watchlist | null;
  summary: MarketSummary | null;
}

export function useDashboardOutlet() {
  return useOutletContext<DashboardOutletContext>();
}

function useIstClock() {
  const [now, setNow] = useState("");
  useEffect(() => {
    const tick = () => setNow(formatIstClock());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { username, logout } = useAuth();
  const ist = useIstClock();
  const [health, setHealth] = useState<EngineHealth | null>(null);
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [watchlist, setWatchlist] = useState<Watchlist | null>(null);
  const [activeCommodity, setActiveCommodity] = useState("SENSEX");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const isDashboard = location.pathname === "/";

  const load = useCallback(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchMarketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 2000);
    const onRefresh = () => load();
    window.addEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    return () => {
      clearInterval(id);
      window.removeEventListener(COCKPIT_REFRESH_EVENT, onRefresh);
    };
  }, [load]);

  useEffect(() => {
    fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    const stop = openWatchlistStream(
      (wl) => setWatchlist(wl),
      () => {
        fetchWatchlist().then(setWatchlist).catch(() => undefined);
      },
    );
    const id = setInterval(() => {
      fetchWatchlist().then(setWatchlist).catch(() => setWatchlist(null));
    }, 5000);
    return () => {
      stop();
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!watchlist?.commodities?.length) return;
    if (!watchlist.commodities.some((c) => c.underlying === activeCommodity)) {
      setActiveCommodity(watchlist.commodities[0].underlying);
    }
  }, [watchlist?.commodities, activeCommodity]);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [sidebarOpen]);

  const commodities = useMemo(() => {
    // Prefer live SSE/watchlist LTPs. market-summary is slower (REST/features cache)
    // and must only fill gaps — never overwrite a live spot_ltp.
    const base = mergeHeaderCommodities(
      watchlist?.commodities ??
        summary?.commodities ?? [
          { underlying: "SENSEX", display_name: "SENSEX", spot_ltp: null, atm_strike: null },
        ],
    );
    if (!summary) return base;
    return base.map((row) => {
      if (row.underlying === "SENSEX" && row.spot_ltp == null && summary.cash_index_spot != null) {
        return { ...row, spot_ltp: summary.cash_index_spot };
      }
      return row;
    });
  }, [watchlist?.commodities, summary?.commodities, summary?.cash_index_spot]);

  const indexQuotes = useIndexQuotes(commodities);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const refreshDashboardData = useCallback(async () => {
    load();
    try {
      const wl = await fetchWatchlist();
      setWatchlist(wl);
    } catch {
      setWatchlist(null);
    }
  }, [load]);

  const brokerProvider =
    health?.broker_credentials?.provider ??
    health?.flattrade_credentials?.provider ??
    "upstox";
  const brokerName = brokerDisplayName(brokerProvider);

  const outletContext: DashboardOutletContext = {
    activeCommodity,
    setActiveCommodity,
    watchlist,
    summary,
  };

  return (
    <div className={`app-shell app-shell-dashboard ref-layout${sidebarOpen ? " ref-sidebar-open" : ""}`}>
      <GlobalTopHeader
        commodities={indexQuotes}
        active={activeCommodity}
        onChange={setActiveCommodity}
        brokerConnected={health?.broker_connected}
        brokerName={brokerName}
        clock={ist}
        marketOpen={watchlist?.market_open ?? summary?.market_open ?? health?.market_open}
        marketSession={summary?.market_session ?? health?.market_session}
        feedMode={watchlist?.feed_mode ?? summary?.feed_mode}
        wsOpen={watchlist?.ws_open ?? summary?.ws_open}
        wsQuoteAgeSec={watchlist?.ws_quote_age_sec ?? summary?.ws_quote_age_sec}
        quoteAgeSec={watchlist?.quote_age_sec ?? summary?.quote_age_sec}
        onMenuToggle={() => setSidebarOpen((open) => !open)}
        engineControls={
          <CockpitEngineControls
            onDataRefresh={refreshDashboardData}
            onLogout={handleLogout}
            brokerName={brokerName}
          />
        }
      />

      <div className="ref-body">
        <button
          type="button"
          className="ref-sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
        <aside className={`sidebar ref-sidebar${sidebarOpen ? " is-open" : ""}`}>
          <RefSidebarNav alertCount={summary?.unread_notifications ?? 0} />
          <div className="sidebar-widgets">
            <MarketStatsPanel watchlist={watchlist} summary={summary} activeCommodity={activeCommodity} />
          </div>
          <RefSidebarFooter
            status={health?.status}
            username={username ?? undefined}
            brokerOn={health?.broker_connected}
            onLogout={handleLogout}
          />
        </aside>

        <div className="main ref-main">
          <main className={isDashboard ? "content content-cockpit" : "content content-page"}>
            <Outlet context={outletContext} />
          </main>
        </div>
      </div>
    </div>
  );
}
