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

The hourly build can ask Claude for a short Korean market commentary (shown in the page's **AI** tab). The
page itself never calls an API, so no key is ever published: only the resulting text goes into `index.html`.

- Uses the **Message Batches API** (50 % cheaper), because the Console promo credit applies to Batch API /
  Agent SDK / Playground / Managed Agents, not to plain real-time Messages calls. One batch per hour; if it is
  not finished within ~2 minutes the next build collects it.
- Key (never commit it, never paste it in chat): create it in the Console under the organization that holds the
  credit, then either set the `ANTHROPIC_API_KEY` environment variable or save it as
  `%USERPROFILE%\.btc_chart\anthropic_key.txt`.
- No key means the AI tab simply stays hidden. `--ai off` disables it, `--ai force` submits right away.
- Caps (env vars): `BTC_AI_BUDGET_USD` (total, default 90), `BTC_AI_MAX_CALLS_PER_DAY` (24),
  `BTC_AI_MIN_MINUTES` (55), `BTC_AI_MODEL` (`claude-opus-5-5`), `BTC_AI_EFFORT` (`medium`). Three failures in a
  row (e.g. credit used up) pause it for 6 h. Spend is tracked in `out/ai.json`; `python -m btc_chart.ai` prints it.
- Needs `pip install anthropic`.

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
