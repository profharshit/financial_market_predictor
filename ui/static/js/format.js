/**
 * format.js — shared formatting + small pure helpers.
 * No DOM access, no fetches: safe to reuse from any module or a unit test.
 */

export const PIP = 0.0001;

export const f5 = x => Number(x).toFixed(5);
export const f4 = x => Number(x).toFixed(4);
export const f1 = x => Number(x).toFixed(1);

export const when = iso => new Date(iso).toLocaleString("en-GB", {
  timeZone: "UTC", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});

export const whenShort = iso => when(iso).replace(",", "");

export const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export const NAMES = { xgb_quantile: "XGBoost", vol_only: "Volatility-only", ridge: "Linear" };

/** FX weekend: closed roughly Fri 22:00 UTC through Sun 21:00 UTC. */
export function marketClosed(d) {
  const w = d.getUTCDay(), h = d.getUTCHours();
  return w === 6 || (w === 5 && h >= 22) || (w === 0 && h < 21);
}

export function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

/** Small helper: true UTC-hour distance between two ISO timestamps. */
export function hoursBetween(isoA, isoB) {
  return Math.abs(new Date(isoA).getTime() - new Date(isoB).getTime()) / 3.6e6;
}
