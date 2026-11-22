# Dashboard overhaul — integration guide

## What this is

A drop-in replacement for `ui/index.html` plus a new `ui/static/` folder,
built on top of the existing FastAPI service. It does **not** change
`serve_api.py`'s prediction logic, the model, the database schema, or any
Python file in `src/` — it only reads endpoints that already exist
(`/health`, `/latest`, `/bars`, `/forward-test`, `/model/info`, `/sync`),
including the `lorentzian` block that `/bars` and `/latest` already return
(see `INTEGRATION.md` §2/§3 — it's already there, just not drawn yet, per
`SYSTEM_GUIDE.md` §7/§8 item 10).

## 1. Files to copy

```
financial-market-project/
└── ui/
    ├── index.html                 ← replace the existing file
    └── static/                    ← new folder
        ├── css/
        │   └── dashboard.css
        └── js/
            ├── api.js
            ├── chartLayout.js
            ├── compositeSignal.js
            ├── dom.js
            ├── format.js
            ├── main.js
            ├── predictionTracker.js
            ├── priceChart.js
            ├── supportResistance.js
            ├── ui.js
            └── volumeChart.js
```

Copy the whole `ui/` folder from this delivery over your project's `ui/`
folder (or copy `static/` in alongside your existing `index.html`, then
replace `index.html`).

## 2. The one backend change you need

`serve_api.py` currently serves only the single `index.html` file at `/ui`
(`FileResponse(UI_PATH, ...)`). The new dashboard is split into several
JS/CSS files, so it needs one additional static-file mount. Add this near
the top of `serve_api.py` (imports) and once more right after `app = FastAPI(...)`:

```python
# add to the import block near the top of serve_api.py
from fastapi.staticfiles import StaticFiles

# add right after `app = FastAPI(...)` / `app.add_middleware(CORSMiddleware, ...)`
app.mount("/ui/static", StaticFiles(directory=os.path.join(HERE, "../ui/static")), name="ui-static")
```

That's the only Python change. Nothing else in `serve_api.py`,
`inference_engine.py`, `store.py` or `schema.sql` needs to change for this
pass — re-run `smoke_test_api.py` after adding the mount; it doesn't touch
`/ui` or `/ui/static` so it should still print `ALL TESTS PASSED` unchanged.

## 3. Run it

Same as always:

```bash
cd src
uvicorn serve_api:app --port 8000
```

Open **http://localhost:8000/ui**.

## 4. What's new, file by file

| File | Responsibility |
|---|---|
| `format.js` | Pure formatting helpers (`f5`, `when`, `marketClosed`, …) — no DOM, no fetch. |
| `api.js` | Thin `fetch` wrapper: adds the `X-API-Key` header, throws a typed error on 401. |
| `chartLayout.js` | The **one** place that computes chart pixel geometry, so the price and volume SVGs always share the same x-axis. |
| `supportResistance.js` | Swing-pivot Support/Resistance detector, computed client-side from `GET /bars`. Transparent and inspectable (see its own header comment for the exact method). |
| `compositeSignal.js` | The dashboard's own BUY/SELL/HOLD call: a weighted, explainable combination of the Lorentzian bias, the adaptive-stop trend, and proximity to a support/resistance level. **This is a presentation heuristic, not the same thing as the API's own `edge_verified` / `skill_established` flags** — see the file's header comment. Weights and thresholds are in one place so you can point to them and explain the "strategy" directly. |
| `predictionTracker.js` | Records every directional composite call and scores it (win/loss/push) once its target bar has actually happened, using `localStorage`. See "Known limitation" below. |
| `priceChart.js` | Candles, the adaptive stop, the Lorentzian kernel line + its own buy/sell markers, S/R levels, and the forecast fan — one SVG. |
| `volumeChart.js` | Volume bars underneath, aligned to the same x-axis via `chartLayout.js`. |
| `dom.js` | Every element id the app touches, gathered once. |
| `ui.js` | All non-chart DOM rendering (KPIs, panels, tables). Pure: takes data in, writes the DOM, nothing else. |
| `main.js` | The only file that knows about intervals and DOM events. Orchestrates: fetch → compute (S/R, composite signal, ledger) → render, on load, on sync, and once a minute. |

## 5. Assumptions made (please adjust if these don't fit)

1. **Prediction ledger persistence.** `predictionTracker.js` stores its
   history in the browser's `localStorage`, keyed `fmp_composite_predictions_v1`.
   That means the scorecard is per-browser and resets if you clear site data
   or open the dashboard on a different machine — but it needed **zero**
   backend changes, which seemed like the right default for a first pass.
   If you'd rather have it durable and shared across everyone viewing the
   dashboard, the natural next step is a `predictions` table (mirroring
   `signals`/`forecasts` in `schema.sql`) and two small endpoints
   (`POST /predictions`, `GET /predictions`) — happy to build that as a
   follow-up if useful.
2. **Resolution horizon.** Each composite call is scored against price 4
   hours later, matching the convention `signal.model_bias` already uses
   in the API (see `INTEGRATION.md` §3). A move smaller than 1 pip counts
   as a "push" rather than a win or a loss.
3. **Composite signal weights** (Lorentzian 45% / stop trend 20% / S/R
   proximity 35%, thresholds at ±0.35) are a reasonable starting point, not
   a tuned or backtested strategy — they're deliberately all in one place
   (`compositeSignal.js`) so you can justify or retune them for your
   presentation.
4. **Support/Resistance** uses a standard swing-pivot method (4 bars each
   side, ~8 pip merge tolerance, last 250 bars). Also all in one file if you
   want to tune it.

If any of these should instead be a backend/DB change, or you had a
different "strategy" in mind for the composite signal, let me know and I'll
adjust before you present this.

## 6. Suggested features for a stronger presentation

Roughly cheapest-first:

1. **Persist the prediction ledger server-side** (see §5.1) so the scorecard
   survives refresh/reset and can be shown as evidence across a longer
   demo window, not just one browser session.
2. **An equity-curve chart** for the simulated P/L (cumulative pips over
   time) next to the win/loss/push counts — turns the ledger into a visual,
   which reads well in a slide-style presentation.
3. **Session/volatility badges** on the header (London/NY/Tokyo session,
   current ATR percentile) — cheap to compute from data you already have
   and makes the "why now" story more concrete.
4. **Wire in `lorentzian.exit`** (already returned by the API, not drawn
   yet) as a small marker where a held position's fixed-bar exit fires —
   completes the Lorentzian story on the chart.
5. **A "what changed" diff toast** when the composite signal flips
   (e.g. "HOLD → BUY: Lorentzian turned up, price bounced off support
   1.10065") — nice for narrating a live demo without reading the factor
   list yourself.
6. **Keyboard-free presentation mode**: a full-screen toggle that hides the
   sync controls and just auto-refreshes, useful when the dashboard is
   projected during a viva/defence.
7. Longer term, the two items already on `SYSTEM_GUIDE.md`'s own roadmap
   pair naturally with this UI work: wiring `lorentzian` into `schema.sql`/
   `store.py` (§8 item 10) and a `/predict`-side anomaly detector badge
   (§8 item 9) — both would slot into the sidebar next to the composite
   signal card.
