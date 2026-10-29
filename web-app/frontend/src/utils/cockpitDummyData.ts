import type { ClosedBlotterTrade, DecisionLogEvent, WatchlistOpenPosition } from "../types";

/** Placeholder cockpit data when no live events yet (dashboard preview only). */
export const COCKPIT_PREVIEW_MODE = false;

export const AI_PANEL_INDICES = ["SENSEX"] as const;
export type AiPanelIndex = (typeof AI_PANEL_INDICES)[number];

export const AI_PANEL_LABELS: Record<AiPanelIndex, string> = {
  NIFTY: "NIFTY 50",
};

export interface IndexStrategyInsight {
  name: string;
  bias: "BULLISH" | "BEARISH" | "NEUTRAL";
  confidence: number;
  bullishConfidence: number;
  bearishConfidence: number;
  leadingSide: "BULLISH" | "BEARISH" | "NEUTRAL";
  reasons: string[];
  entryLow: number;
  entryHigh: number;
  targets: [number, number];
  stopLoss: number;
}

export const strategyByIndex: Record<AiPanelIndex, IndexStrategyInsight> = {
  NIFTY: {
    name: "VWAP RECLAIM",
    bias: "BULLISH",
    confidence: 78,
    bullishConfidence: 78,
    bearishConfidence: 42,
    leadingSide: "BULLISH",
    reasons: [
      "Price reclaimed VWAP with volume confirmation",
      "PCR below 1.0 — bullish positioning",
      "5m structure turned bullish above EMA21",
      "OI buildup at ATM calls supports upside",
    ],
    entryLow: 23850,
    entryHigh: 23870,
    targets: [24100, 24250],
    stopLoss: 23720,
  },
};

export function indexStep(underlying: string): number {
  if (underlying === "BANKNIFTY") return 100;
  return 50;
}

export function levelsFromSpot(spot: number, underlying: string) {
  const step = indexStep(underlying);
  const entryLow = Math.round((spot - step * 3) / step) * step;
  const entryHigh = Math.round((spot - step) / step) * step;
  const t1 = Math.round((spot + step * 4) / step) * step;
  const t2 = Math.round((spot + step * 7) / step) * step;
  const sl = Math.round((spot - step * 6) / step) * step;
  return { entryLow, entryHigh, targets: [t1, t2] as [number, number], stopLoss: sl };
}

export const COCKPIT_DUMMY = {
  indices: {
    BANKNIFTY: { spot: 52371.8, change: 234.85, changePct: 0.45 },
    INDIAVIX: { spot: 12.48, change: -0.18, changePct: -1.42 },
  },
  breadth: {
    advancing: 127,
    declining: 89,
    neutral: 34,
    pcr: 0.92,
    vwap: 23768.45,
    oiPcr: 1.08,
    vix: 12.48,
    vixChangePct: -1.42,
  },
  strategy: strategyByIndex.NIFTY,
  position: {
    tsym: "NIFTY 24 JUL 23,900 CE",
    side: "BUY",
    optionType: "CE",
    lots: 2,
    quantity: 100,
    entryPrice: 142.5,
    ltp: 158.9,
    indexEntry: 23872.5,
    indexLtp: 23905.3,
    unrealizedPnl: 3280,
    pnlPct: 1.37,
    setupType: "VWAP RECLAIM",
    stopLoss: 118.0,
    targetPrice: 175.0,
    entryTs: new Date().toISOString(),
  },
  risk: {
    riskPct: 28,
    label: "LOW RISK",
    dailyPnl: 6927,
    maxLoss: 10000,
    usedMargin: 238760,
    available: 261240,
  },
  footer: {
    totalPnl: 3280,
    margin: 238760,
    mtm: 3280,
    tradeCount: 1,
  },
  journal: [
    {
      id: "dummy-j1",
      tsym: "NIFTY 24 JUL 23,850 PE",
      side: "SELL",
      entryPrice: 98.2,
      exitPrice: 72.5,
      pnl: 1925,
      exitReason: "Target Hit",
      time: "14:22",
    },
    {
      id: "dummy-j2",
      tsym: "NIFTY 24 JUL 23,950 CE",
      side: "BUY",
      entryPrice: 115.0,
      exitPrice: 98.4,
      pnl: -1245,
      exitReason: "SL Hit",
      time: "11:05",
    },
  ] as const,
  decisions: [
    { time: "10:27:18", title: "VWAP Reclaim detected", summary: "NIFTY reclaimed session VWAP on 5m close", severity: "success" },
    { time: "10:27:22", title: "Entry signal generated", summary: "23,900 CE added to candidate queue", severity: "success" },
    { time: "10:26:55", title: "Volume spike", summary: "1.8× average volume on NIFTY futures", severity: "info" },
    { time: "10:25:10", title: "Risk check passed", summary: "Margin and daily loss limits within bounds", severity: "success" },
  ] as const,
  chainExpiries: ["24 JUL 25 (W)", "31 JUL 25 (W)", "07 AUG 25 (M)", "28 AUG 25 (M)"],
};

function todayIstIso(hour: number, minute: number): string {
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .formatToParts(now)
    .reduce<Record<string, string>>((acc, p) => {
      if (p.type !== "literal") acc[p.type] = p.value;
      return acc;
    }, {});
  const y = parts.year;
  const m = parts.month;
  const d = parts.day;
  const hh = String(hour).padStart(2, "0");
  const mm = String(minute).padStart(2, "0");
  return `${y}-${m}-${d}T${hh}:${mm}:00+05:30`;
}

export function dummyOpenPosition(): WatchlistOpenPosition {
  const p = COCKPIT_DUMMY.position;
  return {
    tsym: p.tsym,
    side: p.side,
    quantity: p.quantity,
    lots: p.lots,
    entry_price: p.entryPrice,
    entry_ts: p.entryTs,
    current_ltp: p.ltp,
    unrealized_pnl: p.unrealizedPnl,
    premium_deployed: p.entryPrice * p.quantity,
    setup_type: p.setupType,
    stop_loss: p.stopLoss,
    target_price: p.targetPrice,
  };
}

export function dummyClosedTrades(): ClosedBlotterTrade[] {
  const times = [todayIstIso(14, 22), todayIstIso(11, 5)];
  return COCKPIT_DUMMY.journal.map((j, i) => ({
    id: j.id,
    tsym: j.tsym,
    side: j.side,
    entry_ts: times[i] ?? todayIstIso(10, 0),
    exit_ts: times[i] ?? todayIstIso(10, 0),
    entry_price: j.entryPrice,
    exit_price: j.exitPrice,
    quantity: 50,
    lot_size: 25,
    lots: 2,
    pnl: j.pnl,
    exit_reason: j.exitReason,
    setup_type: "DEMO",
  }));
}

export function dummyDecisionEvents(): DecisionLogEvent[] {
  const now = Date.now();
  return COCKPIT_DUMMY.decisions.map((d, i) => ({
    id: `dummy-dec-${i}`,
    ts: new Date(now - i * 45_000).toISOString(),
    event_type: "scan",
    severity: d.severity,
    message: d.title,
    metadata: { summary: d.summary },
  }));
}

/** Deterministic dummy OI change from strike (visual only). */
export function dummyOiChange(strike: number, isCe: boolean): number {
  const sign = isCe ? 1 : -1;
  return sign * (((strike / 50) % 17) - 8) * 1250;
}

export function dummyVolume(strike: number): number {
  return Math.round(8000 + (strike % 11) * 3400);
}

/** Deterministic OI change % for pill display (visual only). */
export function dummyOiChangePct(strike: number, isCe: boolean): number {
  const base = ((strike / 50) % 13) - 6;
  return Number((base * (isCe ? 1.1 : -0.9)).toFixed(1));
}
