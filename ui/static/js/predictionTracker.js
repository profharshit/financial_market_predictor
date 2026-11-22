/**
 * predictionTracker.js — records every directional composite-signal call and
 * scores it once its target bar has actually happened, purely in the
 * browser (localStorage). No backend/schema change needed to get a working
 * scorecard on the dashboard.
 *
 * Trade-off, stated plainly: this ledger lives in ONE browser. Reloading the
 * dashboard on another machine starts a fresh scorecard. See
 * README_DASHBOARD_INTEGRATION.md for how to move this into the project's
 * own `signals`/`forecasts` tables (schema.sql) if you want it durable and
 * shared across everyone viewing the dashboard.
 */

import { PIP } from "./format.js";

const STORAGE_KEY = "fmp_composite_predictions_v1";
const RESOLUTION_HOURS = 4;   // score each call against the same 4h horizon the API's own model_bias uses
const PUSH_PIPS = 1;          // moves smaller than this count as a "push", not a hit or a miss
const ASSUMED_COST_PIPS = 1;  // mirrors COST_PIPS in train_price_model.py's own backtest

function load() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; } catch { return []; }
}
function save(list) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); } catch { /* storage unavailable */ }
}

/**
 * Record a new call — but only if it's actually a new, distinct directional
 * call (skip HOLD, skip re-recording the same bar or the same still-open call).
 */
export function recordIfNew({ action, confidence, factors, asOf, lastClose }) {
  if (action === "HOLD") return;
  const list = load();
  const prev = list[list.length - 1];
  if (prev && prev.asOf === asOf) return;
  if (prev && prev.pending && prev.action === action) return;

  list.push({
    id: `${asOf}-${action}`,
    asOf, action, confidence, factors,
    entryPrice: lastClose,
    targetTs: new Date(new Date(asOf).getTime() + RESOLUTION_HOURS * 3600e3).toISOString(),
    pending: true, outcome: null, exitPrice: null, pipsResult: null,
  });
  save(list);
}

/** Resolve any pending calls whose target bar has now appeared in `bars` (from GET /bars). */
export function resolvePending(bars) {
  const list = load();
  let changed = false;
  for (const p of list) {
    if (!p.pending) continue;
    const targetMs = new Date(p.targetTs).getTime();
    const target = bars.find(b => Math.abs(new Date(b.t).getTime() - targetMs) < 30 * 60e3);
    if (!target) continue;

    const pips = (target.c - p.entryPrice) / PIP * (p.action === "BUY" ? 1 : -1);
    p.pending = false;
    p.exitPrice = target.c;
    p.pipsResult = pips;
    p.outcome = Math.abs(pips) < PUSH_PIPS ? "push" : pips > 0 ? "win" : "loss";
    changed = true;
  }
  if (changed) save(list);
  return list;
}

export function getHistory(limit = 25) {
  return load().slice(-limit).reverse();
}

export function getStats() {
  const list = load();
  const resolved = list.filter(p => !p.pending);
  const wins = resolved.filter(p => p.outcome === "win").length;
  const losses = resolved.filter(p => p.outcome === "loss").length;
  const pushes = resolved.filter(p => p.outcome === "push").length;
  const decided = wins + losses; // pushes excluded from the accuracy ratio, same idea as store.py's own `called` filter
  const simulatedPips = resolved.reduce((sum, p) => sum + (p.pipsResult || 0) - ASSUMED_COST_PIPS, 0);

  return {
    total: list.length,
    pending: list.filter(p => p.pending).length,
    resolved: resolved.length,
    correct: wins, incorrect: losses, push: pushes,
    accuracy: decided ? wins / decided : null,
    simulatedPips,
  };
}

export function reset() { save([]); }
