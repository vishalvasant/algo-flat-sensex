import { useEffect, useRef, useState } from "react";
import { openPositionStream } from "../api/client";
import type { PositionStreamPayload, WatchlistOpenPosition } from "../types";
import { COCKPIT_REFRESH_EVENT } from "../components/CockpitEngineControls";

const HOLD_EMPTY_MS = 800;
const MAX_LTP_JUMP_PCT = 0.15;

function positionKey(position: WatchlistOpenPosition): string {
  return position.position_id ?? position.tsym;
}

function saneStreamFields(
  prev: WatchlistOpenPosition,
  next: WatchlistOpenPosition,
): Pick<WatchlistOpenPosition, "current_ltp" | "unrealized_pnl" | "pnl_points"> {
  const entry = next.entry_price ?? prev.entry_price ?? 0;
  const qty = next.quantity ?? prev.quantity ?? 0;
  const prevLtp = prev.current_ltp;
  const nextLtp = next.current_ltp;

  if (
    prevLtp != null &&
    nextLtp != null &&
    prevLtp > 0 &&
    Math.abs(nextLtp - prevLtp) / prevLtp > MAX_LTP_JUMP_PCT
  ) {
    return {
      current_ltp: prevLtp,
      unrealized_pnl: prev.unrealized_pnl,
      pnl_points: prev.pnl_points,
    };
  }

  if (
    entry > 0 &&
    nextLtp != null &&
    Math.abs((nextLtp - entry) / entry) > 0.35 &&
    prevLtp != null &&
    Math.abs((prevLtp - entry) / entry) <= 0.2
  ) {
    return {
      current_ltp: prevLtp,
      unrealized_pnl: prev.unrealized_pnl,
      pnl_points: prev.pnl_points,
    };
  }

  const pnl =
    next.unrealized_pnl ??
    (nextLtp != null && entry > 0 ? (nextLtp - entry) * qty : prev.unrealized_pnl);

  return {
    current_ltp: nextLtp ?? prevLtp,
    unrealized_pnl: pnl,
    pnl_points: next.pnl_points ?? (nextLtp != null ? nextLtp - entry : prev.pnl_points),
  };
}

function mergePosition(
  prev: WatchlistOpenPosition,
  next: WatchlistOpenPosition,
): WatchlistOpenPosition {
  const managed =
    next.managed || next.source === "broker+algo" || next.source === "algo" ||
    prev.managed || prev.source === "broker+algo" || prev.source === "algo";
  const stable = saneStreamFields(
    prev,
    {
      ...next,
      // Use locked entry for P&L sanity when broker avg briefly leaks.
      entry_price:
        managed && next.entry_price_source === "broker_display_only"
          ? (prev.entry_price ?? next.entry_price)
          : (next.entry_price ?? prev.entry_price),
      quantity:
        managed && next.entry_price_source === "broker_display_only"
          ? (prev.quantity ?? next.quantity)
          : (next.quantity ?? prev.quantity),
    },
  );
  return {
    ...prev,
    ...next,
    entry_price:
      managed && next.entry_price_source === "broker_display_only"
        ? (prev.entry_price ?? next.entry_price)
        : managed
          ? (next.entry_price ?? prev.entry_price)
          : (prev.entry_price ?? next.entry_price),
    quantity:
      managed && next.entry_price_source === "broker_display_only"
        ? (prev.quantity ?? next.quantity)
        : (next.quantity ?? prev.quantity),
    entry_price_source: managed
      ? (next.entry_price_source === "broker_display_only"
          ? (prev.entry_price_source ?? "order_fill")
          : (next.entry_price_source ?? prev.entry_price_source ?? "order_fill"))
      : next.entry_price_source ?? prev.entry_price_source,
    lots:
      managed && next.entry_price_source === "broker_display_only"
        ? (prev.lots ?? next.lots)
        : (next.lots ?? prev.lots),
    lot_size: next.lot_size ?? prev.lot_size,
    current_ltp: stable.current_ltp,
    unrealized_pnl: stable.unrealized_pnl,
    mfe_points: next.mfe_points ?? prev.mfe_points,
    mfe_inr: next.mfe_inr ?? prev.mfe_inr,
    mfe_pct: next.mfe_pct ?? prev.mfe_pct,
    trail_floor: next.trail_floor ?? prev.trail_floor,
    pnl_points: stable.pnl_points,
    setup_type: next.setup_type ?? prev.setup_type,
    stop_loss: next.stop_loss ?? prev.stop_loss,
    target_price: next.target_price ?? prev.target_price,
  };
}

function mergeList(
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

export function usePositionStream() {
  const cacheRef = useRef<WatchlistOpenPosition[]>([]);
  const emptySinceRef = useRef<number | null>(null);
  const lastCountRef = useRef<number | null>(null);
  const [positions, setPositions] = useState<WatchlistOpenPosition[]>([]);
  const [meta, setMeta] = useState<Pick<
    PositionStreamPayload,
    "feed_mode" | "ws_open" | "ts"
  > | null>(null);

  useEffect(() => {
    const stop = openPositionStream((payload) => {
      setMeta({
        feed_mode: payload.feed_mode,
        ws_open: payload.ws_open,
        ts: payload.ts,
      });
      const incoming = payload.open_positions ?? [];
      if (incoming.length > 0) {
        emptySinceRef.current = null;
        const merged = mergeList(cacheRef.current, incoming);
        cacheRef.current = merged;
        setPositions(merged);
        return;
      }
      if (cacheRef.current.length > 0) {
        const now = Date.now();
        if (emptySinceRef.current == null) emptySinceRef.current = now;
        if (now - emptySinceRef.current < HOLD_EMPTY_MS) {
          setPositions(cacheRef.current);
          return;
        }
      }
      cacheRef.current = [];
      setPositions([]);
    });
    return stop;
  }, []);

  useEffect(() => {
    const count = positions.length;
    if (lastCountRef.current != null && lastCountRef.current !== count) {
      window.dispatchEvent(new CustomEvent(COCKPIT_REFRESH_EVENT));
    }
    lastCountRef.current = count;
  }, [positions.length]);

  return { positions, meta, live: meta?.ws_open === true };
}
