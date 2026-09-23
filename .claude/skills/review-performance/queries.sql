-- signal attribution by decile
with p as (
  select s.mint, s.lineage_score, s.kol_score, s.survivor_score, s.narrative_score, s.total_score,
         pos.pnl_sol, pos.exit_reason, pos.opened_at, pos.closed_at
  from positions pos join signals s on s.id = pos.signal_id
  where pos.closed_at is not null
)
select 'total' as sig, width_bucket(total_score, 0, 1, 10) as decile,
       count(*) n, avg(pnl_sol) expectancy, avg((pnl_sol > 0)::int) hit_rate
from p group by 2
union all
select 'kol', width_bucket(coalesce(kol_score,0), 0, 1, 10), count(*), avg(pnl_sol), avg((pnl_sol>0)::int) from p group by 2
union all
select 'lineage', width_bucket(coalesce(lineage_score,0.5), 0, 1, 10), count(*), avg(pnl_sol), avg((pnl_sol>0)::int) from p group by 2
union all
select 'survivor', width_bucket(coalesce(survivor_score,0), 0, 1, 10), count(*), avg(pnl_sol), avg((pnl_sol>0)::int) from p group by 2
order by 1, 2;

-- exit attribution
select exit_reason, count(*) n, sum(pnl_sol) total, avg(pnl_sol) avg,
       avg(extract(epoch from (closed_at - opened_at))/60) avg_minutes
from positions where closed_at is not null group by 1 order by total desc;

-- daily pnl + drawdown inputs
select date_trunc('day', closed_at) d, sum(pnl_sol) pnl
from positions where closed_at is not null group by 1 order by 1;

-- reject audit sample
select mint, reject_reason, total_score, ts from signals
where decision = 'reject' and ts > now() - interval '7 days'
order by random() limit 20;

-- cluster audit
select w.cluster_id, cs.score, count(pos.id) n, sum(pos.pnl_sol) pnl
from positions pos
join tokens t on t.mint = pos.mint
join wallets w on w.address = t.deployer
left join cluster_scores cs on cs.cluster_id = w.cluster_id
where pos.closed_at is not null
group by 1, 2 order by pnl desc;

-- hypotheses ledger
select id, created_at, status, metric, baseline_value, test_value, params_diff
from hypotheses order by id desc limit 20;
