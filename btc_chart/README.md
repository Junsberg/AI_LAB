# btc_chart

Personal BTC 1H dashboard. Free, keyless public market data only — no account, no funds involved.
Output is one self-contained `index.html` (ECharts is inlined, works offline).

```bash
python -m btc_chart                       # -> btc_chart/out/index.html
python -m btc_chart --hours 96            # visible 1H candles (default 72)
python -m btc_chart --skip coinbase       # skip slow sources, e.g. the first Coinbase backfill
python -m btc_chart --utc-offset 9        # label timezone (default KST)
```

Python 3.11+, standard library only. Tests: `python -m pytest tests/test_btc_chart.py` (the JS/Python parity tests need `node` and are skipped without it).

## Auto-refresh (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File btc_chart\refresh_task.ps1             # install: at logon + hourly
powershell -ExecutionPolicy Bypass -File btc_chart\refresh_task.ps1 -Uninstall  # remove
```

Registers a per-user task `BTC_Chart_Refresh` (no admin). It only runs while the PC is on and you are
logged in; a missed run executes once as soon as possible. Output goes to `btc_chart/out/refresh.log`.
Open `btc_chart/out/index.html` any time for the latest build.

## Web (GitHub Pages)

Live copy: https://junsberg.github.io/AI_LAB/ (public — anyone with the link can open it; it only contains market data).
`python -m btc_chart --publish` force-pushes the built page as the single commit of the `gh-pages` branch, so
history never grows. The scheduled task runs with `--publish`, so the page updates hourly while the PC is on and
shows the last build when it is off. To stop publishing: reinstall the task without `-ExtraArgs "--publish"`
and turn Pages off in the repo settings.

## AI commentary (Claude Message Batches API)

The hourly build asks Claude for Korean market commentary, shown in the page's **AI** tab. The page itself
never calls an API, so no key is ever published: only the resulting text goes into `index.html`.

- One job by default: the **briefing** (Fable 5.1, effort `xhigh`, every 4 hours (`BTC_AI_MIN_MINUTES`, default 235 so
  every 4th hourly build); `plan()` stretches the interval if it would overspend). A second **deep** job exists but is off (`BTC_AI_DEEP=on` turns it on, with
  `BTC_AI_DEEP_MODEL`/`_EFFORT`/`_MIN_MINUTES`); set `BTC_AI_MODEL=claude-opus-5-5` for a cheaper hourly note.
- Uses the **Message Batches API** (50 % cheaper) because the Console promo credit applies to Batch API /
  Agent SDK / Playground / Managed Agents, not to plain real-time Messages calls. A build waits ~2 minutes for
  its batches; unfinished ones are collected by the next build.
- Key (never commit it, never paste it in chat): create it in the Console under the organization that holds the
  credit, then set `ANTHROPIC_API_KEY` or save it as `%USERPROFILE%\.btc_chart\anthropic_key.txt`.
  No key means the AI tab stays hidden. `--ai off` disables, `--ai force` submits both jobs now.
- **Pacing**: `plan()` spreads the remaining budget (`BTC_AI_BUDGET_USD`, default 90) over the days left until
  `BTC_AI_EXPIRES` (2026-11-18) using each job's measured last cost. If calls get pricier (more facts in the
  fact sheet -> more tokens) the hourly note automatically slows down instead of overspending; the deep report
  keeps its slot. Also a daily call cap, and 3 failures in a row pause a job for 6 h (e.g. credit used up).
- Env knobs: `BTC_AI_MODEL`/`BTC_AI_EFFORT`/`BTC_AI_MIN_MINUTES` (hourly), `BTC_AI_DEEP*` (deep, off by default),
  `BTC_AI_MAX_CALLS_PER_DAY` (30).
- `python -m btc_chart.ai` prints spend and the current schedule (state is in `out/ai.json`).
- `python -m btc_chart --ai compare` runs one batch with the same facts at `BTC_AI_COMPARE_EFFORTS`
  (default `high,xhigh,max`) for `BTC_AI_MODEL` and writes `out/ai_compare_<model>.md`. Measured so far
  (Opus 5.5): high 2.7k tokens $0.03, xhigh 5.3k $0.06, max hit the 32k token limit and was cut off ($0.33).
  Fable 5.1: high $0.11, xhigh $0.13.
- Needs `pip install anthropic`.

## Market context (free public APIs, no keys)

`btc_chart/context.py` collects, at build time and each group independently (one flaky API only blanks its own
group; `--skip context` or a group name skips it): **derivatives** (Binance futures funding, long/short account
and top-trader ratios, perp basis, taker ratio), **on-chain** (CoinMetrics community API: MVRV, exchange
inflow/outflow/balance, active addresses, hashrate; daily, newest day is preliminary), **network** (mempool.space
fees and difficulty adjustment), **stablecoin supply** (DefiLlama), **Fear & Greed** (alternative.me) and
**dominance** (CoinGecko: BTC / USDT / everything outside the top 10). They are shown as the "시장 지표" card on the
수급 tab, fed to the AI as `market_context`, and the AI writes a `context` paragraph about them. The header also
shows live dominance tiles (browser fetches CoinGecko every 2 min). Some CoinMetrics metrics (realized cap, NVT,
long-term holder supply) are paid and not used.

## What it shows

The page talks to the exchanges **directly from the browser** (public endpoints, no keys, no server), so it is live
even when the PC that built it is off. Python only builds the file and supplies what a browser cannot fetch.

- **Timeframes** 15m (default) / 1H / 4H / 1D; 1000 candles per timeframe, 120 visible on phones (60/120/240/500 buttons, pinch to zoom)
- **Live price**: Binance WebSocket `miniTicker` (1 s) moves the forming candle, the header price and the 24h stats; `kline_<tf>` keeps volume/CVD current. If the socket drops it reconnects with back-off
- EMA 20/50/100/200 with an `EMA` master switch and per-line chips (choice is remembered)
- Unfilled 4H / 1D fair value gaps, computed in the page (JS port of `indicators.find_fvgs`, kept identical by `tests/test_btc_chart.py`)
- Cumulative CVD (net taker-buy USD, reset to 0 at the left edge of the visible range): Binance spot and futures on every timeframe; Coinbase spot and OKX contracts on 1H/4H only (hourly build data)
- Binance futures open interest and Coinbase premium (Coinbase USD close vs Binance USDT close, %; includes the USD/USDT basis) on every timeframe
- If Binance is unreachable the page falls back to the last build (1H) and shows a banner; tap it to retry

## Notes

- **Binance futures and Bybit are geo-blocked from many cloud hosts (HTTP 451/403).** Run it on your own PC. In the browser the same applies to the viewer's network; a blocked source shows a note instead of a chart.
- Coinbase CVD is built from raw trades (`side` there is the *maker* side, so a `sell` print is a taker buy). The first run backfills the window (~10 min for 72h) and caches completed hours in `btc_chart/out/cache/`; later runs fetch only the new tail.
- OKX CVD uses the `rubik/stat/taker-volume` endpoint (`[ts, sellVol, buyVol]`). Checked against raw BTC-USDT-SWAP trades: the column order is right, but `ts` is the bucket *end*, so rows are shifted back one hour. The volume covers more than BTC-USDT-SWAP (other BTC contracts), so compare direction, not size, with Binance.
- Not available keyless: liquidation history (Binance has no public endpoint; OKX only exposes recent rows) — would need a websocket collector.
- Vendored third-party code: see `vendor/README.md` (Apache ECharts, Apache-2.0).
