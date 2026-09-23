---
name: review-performance
description: Attribute PnL to signals and exit reasons from the memebot DB, then write a dated report to docs/reviews/. Use on a schedule or when asked "how is the bot doing".
---

# review-performance

Read-only. Never edits strategy/params.yaml.

## Steps
1. Load env: `python -c "from memebot.config import settings; print(bool(settings.database_url))"` must print True.
2. Run the SQL in `queries.sql` (this folder) against `DATABASE_URL` with `psql` or psycopg. Sections:
   - **Signal attribution**: for each signal (lineage / kol / survivor / narrative) bucket entries by score decile → hit rate (pnl>0), expectancy (avg pnl_sol), n.
   - **Exit attribution**: pnl by `positions.exit_reason`; time-in-trade; how often `take_initial` fired before `hard_stop`.
   - **Reject audit**: sample 20 `signals.decision='reject'` mints, fetch current mcap (DexScreener free API) → did we reject winners? Which gate?
   - **Cluster audit**: top/bottom 10 `cluster_scores` by realized pnl of positions whose deployer is in the cluster.
   - **Drawdown**: daily pnl series, max DD, current streak.
3. Write `docs/reviews/YYYY-MM-DD.md` with tables + 3 bullet conclusions max. State sample sizes; if n < 30 for a bucket say "insufficient".
4. End with **one** candidate hypothesis in the exact format `propose-change` expects:
   ```
   HYPOTHESIS: <param path> <old> → <new> | metric=<name> | rationale=<one sentence>
   ```
   Only propose keys under `exits`, `weights`, `thresholds`. Never `risk`.
