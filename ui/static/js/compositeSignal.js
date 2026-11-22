/**
 * compositeSignal.js — the dashboard's own "bot call": BUY / SELL / HOLD with
 * a confidence score, recomputed on every refresh from the latest bar.
 *
 * This is a presentation-layer heuristic, NOT the same thing as the API's
 * `signal.edge_verified` / `lorentzian.skill_established` flags. Those are
 * statistically backtested in train_price_model.py; this is a transparent,
 * explainable rule that fuses three inputs the API already exposes so the
 * dashboard has something concrete to show and score itself against:
 *
 *   1. Lorentzian ANN bias + confidence      (lorentzian_signal.py)
 *   2. Adaptive trailing-stop trend          (trailing_stop.py)
 *   3. Proximity to a recent Support/Resistance level  (supportResistance.js)
 *
 * Keep the weights and thresholds here, in one place, so the "strategy" is
 * easy to point to and explain in a presentation.
 */

export const WEIGHTS = Object.freeze({ lorentzian: 0.45, stopTrend: 0.20, srProximity: 0.35 });
export const BUY_THRESHOLD = 0.35;
export const SELL_THRESHOLD = -0.35;
export const SR_PROXIMITY_PCT = 0.0015; // ~15 pips at 1.10 counts as "near" a level

function lorentzianVote(lorentzian) {
  if (!lorentzian) return 0;
  const sign = lorentzian.bias === "up" ? 1 : lorentzian.bias === "down" ? -1 : 0;
  return sign * lorentzian.confidence;
}

function stopTrendVote(stop) {
  if (!stop) return 0;
  return stop.trend === "up" ? 0.6 : stop.trend === "down" ? -0.6 : 0;
}

function srProximityVote(lastClose, sr) {
  const support = sr?.support?.[0], resistance = sr?.resistance?.[0];
  if (!support && !resistance) return { vote: 0, note: "no S/R level in range" };

  const distToSupport = support ? (lastClose - support.price) / lastClose : Infinity;
  const distToResistance = resistance ? (resistance.price - lastClose) / lastClose : Infinity;

  if (support && distToSupport >= 0 && distToSupport < SR_PROXIMITY_PCT) {
    const strengthBoost = Math.min(1, support.strength / 4);
    return { vote: 0.5 + 0.5 * strengthBoost, note: `bouncing off support ${support.price.toFixed(5)}` };
  }
  if (resistance && distToResistance >= 0 && distToResistance < SR_PROXIMITY_PCT) {
    const strengthBoost = Math.min(1, resistance.strength / 4);
    return { vote: -(0.5 + 0.5 * strengthBoost), note: `stalling under resistance ${resistance.price.toFixed(5)}` };
  }
  return { vote: 0, note: "mid-range, no level nearby" };
}

/**
 * @param lastClose  latest close price
 * @param lorentzian latest.lorentzian ({bias, confidence, ...}) or null
 * @param stop       latest.trailing_stop ({trend, ...}) or null
 * @param sr         computeSupportResistance() output ({support, resistance})
 */
export function computeCompositeSignal({ lastClose, lorentzian, stop, sr }) {
  const lorVote = lorentzianVote(lorentzian);
  const stopVote = stopTrendVote(stop);
  const { vote: srVote, note: srNote } = srProximityVote(lastClose, sr);

  const score = WEIGHTS.lorentzian * lorVote + WEIGHTS.stopTrend * stopVote + WEIGHTS.srProximity * srVote;
  const action = score >= BUY_THRESHOLD ? "BUY" : score <= SELL_THRESHOLD ? "SELL" : "HOLD";
  const confidence = Math.round(Math.min(1, Math.abs(score)) * 100);

  return {
    action, score, confidence,
    factors: [
      {
        label: "Lorentzian ANN", vote: lorVote,
        detail: lorentzian ? `${lorentzian.bias} · ${(lorentzian.confidence * 100).toFixed(0)}% confidence` : "not available",
      },
      {
        label: "Adaptive stop trend", vote: stopVote,
        detail: stop ? (stop.trend === "up" ? "up-trend (long side)" : stop.trend === "down" ? "down-trend (short side)" : "flat") : "not available",
      },
      { label: "Support / Resistance", vote: srVote, detail: srNote },
    ],
  };
}
