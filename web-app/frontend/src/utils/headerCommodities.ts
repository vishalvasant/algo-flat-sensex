import type { CommoditySnapshot } from "../types";

/** Top-bar cards always shown — merged with live watchlist/SSE data. */
export const HEADER_COMMODITY_DEFAULTS: CommoditySnapshot[] = [
  { underlying: "SENSEX", display_name: "SENSEX", spot_ltp: null, atm_strike: null },
  { underlying: "INDIAVIX", display_name: "INDIA VIX", spot_ltp: null, atm_strike: null },
];

export function mergeHeaderCommodities(apiRows: CommoditySnapshot[]): CommoditySnapshot[] {
  const cleaned = apiRows.filter((row) => {
    const u = (row.underlying || "").toUpperCase();
    const n = (row.display_name || "").toUpperCase();
    if (u.endsWith("_FUT") || row.card_type === "fut") return false;
    return u !== "GIFTNIFTY" && !u.includes("GIFT") && !n.includes("GIFT NIFTY");
  });
  const byKey = new Map(cleaned.map((row) => [row.underlying, row]));
  const merged = HEADER_COMMODITY_DEFAULTS.map((def) => ({
    ...def,
    ...byKey.get(def.underlying),
  }));
  for (const row of cleaned) {
    if (!HEADER_COMMODITY_DEFAULTS.some((d) => d.underlying === row.underlying)) {
      merged.push(row);
    }
  }
  return merged;
}
