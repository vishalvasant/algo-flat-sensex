import { BarChart3 } from "lucide-react";
import { AppPageShell } from "../components/AppPageShell";
import { useDashboardOutlet } from "../components/Layout";
import { WatchlistPanel } from "../components/WatchlistPanel";

export function OptionChainPage() {
  const { activeCommodity, watchlist, summary } = useDashboardOutlet();

  return (
    <AppPageShell
      title="Options Chain"
      icon={BarChart3}
      description="Full ATM band · WebSocket live during market hours · REST fallback"
    >
      <div className="logs-page-full option-chain-page">
        <WatchlistPanel
          watchlist={watchlist}
          summary={summary}
          activeCommodity={activeCommodity}
        />
      </div>
    </AppPageShell>
  );
}
