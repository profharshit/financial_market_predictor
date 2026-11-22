/**
 * dom.js — every element id the dashboard touches, gathered once. If you
 * rename an id in index.html, this is the only other file that needs to know.
 */
export function queryDom() {
  const $ = id => document.getElementById(id);
  return {
    status: $("status"),
    notice: $("notice"),
    disclaimer: $("disclaimer"),
    syncBtn: $("sync"),
    autoToggle: $("auto"),

    kClose: $("k-close"), kCloseN: $("k-close-n"),
    kStop: $("k-stop"), kStopN: $("k-stop-n"),
    kSig: $("k-sig"), kSigN: $("k-sig-n"),
    kNext: $("k-next"), kNextN: $("k-next-n"),

    signalAction: $("signal-action"),
    signalConfidence: $("signal-confidence"),
    signalFactors: $("signal-factors"),
    signalNote: $("signal-note"),

    srList: $("sr-list"),

    predTotal: $("pred-total"), predCorrect: $("pred-correct"),
    predIncorrect: $("pred-incorrect"), predPush: $("pred-push"),
    predAccuracy: $("pred-accuracy"), predPnl: $("pred-pnl"),
    predPending: $("pred-pending"),
    predHistoryBody: $("pred-history-body"),
    resetLedgerBtn: $("reset-ledger"),

    fcBody: document.querySelector("#fc tbody"), fcFoot: $("fc-foot"),
    scBody: document.querySelector("#sc tbody"), scFoot: $("sc-foot"),

    chart: $("chart"), tip: $("tip"), volume: $("volume"),
    rangeGroup: $("range"),

    foot: $("foot"),
  };
}
