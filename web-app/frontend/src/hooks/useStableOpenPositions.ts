import { useEffect, useMemo, useRef } from "react";
import type { MarketSummary, Watchlist, WatchlistOpenPosition } from "../types";
import { usePositionStream } from "./usePositionStream";

const HOLD_EMPTY_MS = 800;

function positionKey(position: WatchlistOpenPosition): string {
  return position.position_id ?? position.tsym;
}

function mergePosition(
  prev: WatchlistOpenPosition,
  next: WatchlistOpenPosition,
): WatchlistOpenPosition {
  const managed =
    next.managed || next.source === "broker+algo" || next.source === "algo" ||
    prev.managed || prev.source === "broker+algo" || prev.source === "algo";
  // Managed positions: never let broker net-avg overwrite order-fill entry / algo qty.
  const entry =
    managed && next.entry_price_source === "broker_display_only"
      ? (prev.entry_price ?? next.entry_price)
      : managed
        ? (next.entry_price ?? prev.entry_price)
        : (prev.entry_price ?? next.entry_price);
  const quantity = managed
    ? (next.entry_price_source === "broker_display_only"
        ? (prev.quantity ?? next.quantity)
        : (next.quantity ?? prev.quantity))
    : (next.quantity ?? prev.quantity);
  return {
    ...prev,
    ...next,
    entry_price: entry,
    quantity,
    entry_price_source: managed
      ? (next.entry_price_source === "broker_display_only"
          ? (prev.entry_price_source ?? "order_fill")
          : (next.entry_price_source ?? prev.entry_price_source ?? "order_fill"))
      : next.entry_price_source ?? prev.entry_price_source,
    lots: managed
      ? (next.entry_price_source === "broker_display_only"
          ? (prev.lots ?? next.lots)
          : (next.lots ?? prev.lots))
      : (next.lots ?? prev.lots),
    lot_size: next.lot_size ?? prev.lot_size,
    current_ltp: next.current_ltp ?? prev.current_ltp,
    unrealized_pnl: next.unrealized_pnl ?? prev.unrealized_pnl,
    mfe_points: next.mfe_points ?? prev.mfe_points,
    mfe_inr: next.mfe_inr ?? prev.mfe_inr,
    mfe_pct: next.mfe_pct ?? prev.mfe_pct,
    trail_floor: next.trail_floor ?? prev.trail_floor,
    pnl_points: next.pnl_points ?? prev.pnl_points,
    setup_type: next.setup_type ?? prev.setup_type,
    stop_loss: next.stop_loss ?? prev.stop_loss,
    target_price: next.target_price ?? prev.target_price,
  };
}

function mergePositionList(
  cached: WatchlistOpenPosition[],
  fresh: WatchlistOpenPosition[],
): WatchlistOpenPosition[] {
  if (!fresh.length) return cached;

  const byKey = new Map(cached.map((row) => [positionKey(row), { ...row }]));
  const ordered: WatchlistOpenPosition[] = [];

  for (const row of fresh) {
    const key = positionKey(row);
    const prev = byKey.get(key);
    const merged = prev ? mergePosition(prev, row) : row;
    byKey.set(key, merged);
    ordered.push(merged);
  }

  return ordered;
}

function collectIncoming(
  streamPositions: WatchlistOpenPosition[],
  watchlist: Watchlist | null,
  summary: MarketSummary | null | undefined,
): WatchlistOpenPosition[] {
  if (streamPositions.length > 0) return streamPositions;

  const fromWatchlist = watchlist?.open_positions ?? [];
  if (fromWatchlist.length > 0) return fromWatchlist;

  const fromSummary = summary?.open_positions ?? [];
  if (fromSummary.length > 0) return fromSummary;

  const single = summary?.open_position;
  if (!single) return [];

  return [
    {
      tsym: single.tsym,
      side: single.side,
      quantity: single.quantity,
      entry_price: single.entry_price,
      current_ltp: single.current_ltp,
      unrealized_pnl: single.unrealized_pnl,
      premium_deployed: single.premium_deployed,
      setup_type: single.setup_type,
    },
  ];
}

export function useStableOpenPositions(
  watchlist: Watchlist | null,
  summary: MarketSummary | null | undefined,
) {
  const { positions: streamPositions, meta: streamMeta, live } = usePositionStream();
  const cacheRef = useRef<WatchlistOpenPosition[]>([]);
  const emptySinceRef = useRef<number | null>(null);

  const incoming = useMemo(
    () => collectIncoming(streamPositions, watchlist, summary),
    [streamPositions, watchlist, summary],
  );

  const positions = useMemo(() => {
    if (incoming.length > 0) {
      emptySinceRef.current = null;
      return mergePositionList(cacheRef.current, incoming);
    }

    if (cacheRef.current.length > 0) {
      const now = Date.now();
      if (emptySinceRef.current == null) emptySinceRef.current = now;
      if (now - emptySinceRef.current < HOLD_EMPTY_MS) {
        return cacheRef.current;
      }
    }

    return [];
  }, [incoming]);

  useEffect(() => {
    if (positions.length > 0) {
      cacheRef.current = positions;
      return;
    }
    if (
      emptySinceRef.current != null &&
      Date.now() - emptySinceRef.current >= HOLD_EMPTY_MS
    ) {
      cacheRef.current = [];
    }
  }, [positions]);

  return { positions, streamMeta, live };
}
