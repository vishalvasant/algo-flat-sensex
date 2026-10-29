export interface FeedModeView {
  mode: string;
  label: string;
  tone: "live" | "backup" | "offline";
  detail: string;
}

export function describeFeedMode(
  feedMode?: string | null,
  wsOpen?: boolean | null,
  wsQuoteAgeSec?: number | null,
  quoteAgeSec?: number | null,
): FeedModeView {
  const mode = (feedMode ?? "offline").toLowerCase();
  const restAge =
    quoteAgeSec != null && Number.isFinite(quoteAgeSec)
      ? `${Math.round(quoteAgeSec)}s ago`
      : null;
  if (mode === "websocket" || mode === "ws") {
    const stale = wsQuoteAgeSec != null && wsQuoteAgeSec > 12;
    return {
      mode: "websocket",
      label: wsOpen ? (stale ? "WS STALE" : "WS LIVE") : "WS CONNECTING",
      tone: wsOpen && !stale ? "live" : "backup",
      detail: wsOpen
        ? stale
          ? `WebSocket connected — last tick ${Math.round(wsQuoteAgeSec!)}s ago; REST backup active`
          : "Upstox WebSocket stream active"
        : "WebSocket connecting — REST backup may apply",
    };
  }
  if (mode === "rest") {
    const ageBit = restAge ? ` · last tick ${restAge}` : "";
    if (wsOpen) {
      return {
        mode: "rest",
        label: "REST LIVE",
        tone: "backup",
        detail: `Quotes via REST (~1s)${ageBit}. WebSocket linked — waiting for live ticks.`,
      };
    }
    return {
      mode: "rest",
      label: "REST LIVE",
      tone: "backup",
      detail: `Upstox REST quote poll (~1s)${ageBit} — WebSocket not connected`,
    };
  }
  return {
    mode: "offline",
    label: "OFFLINE",
    tone: "offline",
    detail: "Market feed not connected",
  };
}
