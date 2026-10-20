# btc_chart

Personal BTC 1H dashboard. Free, keyless public market data only — no account, no funds involved.
Output is one self-contained `index.html` (ECharts is inlined, works offline).

```bash
python -m btc_chart                       # -> btc_chart/out/index.html
python -m btc_chart --hours 96            # visible 1H candles (default 72)
python -m btc_chart --skip coinbase       # skip slow sources, e.g. the first Coinbase backfill
python -m btc_chart --utc-offset 9        # label timezone (default KST)
```

Python 3.11+, standard library only. Tests: `python -m pytest tests/test_btc_chart.py`.

## What it shows

- 1H candles from Binance spot (`data-api.binance.vision`), EMA 20/50/100/200
- Unfilled 4H / 1D fair value gaps — drawn on the chart when inside the price range, always listed in the FVG map
- Cumulative CVD (net taker-buy USD, starting at 0 at the window start): Binance spot, Binance futures, Coinbase spot, OKX contracts

## Notes

- **Binance futures and Bybit are geo-blocked from many cloud hosts (HTTP 451/403).** Run it on your own PC; a blocked source shows "데이터 없음" and does not fail the build.
- Coinbase CVD is built from raw trades (`side` there is the *maker* side, so a `sell` print is a taker buy). The first run backfills the window (~10 min for 72h) and caches completed hours in `btc_chart/out/cache/`; later runs fetch only the new tail.
- OKX CVD uses the `rubik/stat/taker-volume` endpoint (`[ts, sellVol, buyVol]` per the OKX docs).
- Vendored third-party code: see `vendor/README.md` (Apache ECharts, Apache-2.0).
