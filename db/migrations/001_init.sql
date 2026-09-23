-- memebot schema v1 (Supabase / Postgres)
-- Principle: store raw observations + every decision input so Claude can replay & attribute.

create table if not exists tokens (
  mint            text primary key,
  symbol          text,
  name            text,
  deployer        text not null,
  launch_platform text not null,          -- pumpfun | bonk | raydium | other
  created_at      timestamptz not null,
  migrated_at     timestamptz,            -- bonding curve → AMM
  pool_address    text,
  first_seen_slot bigint,
  meta            jsonb default '{}'::jsonb
);
create index if not exists tokens_deployer_idx on tokens(deployer);
create index if not exists tokens_created_idx on tokens(created_at desc);

-- Deployer lineage: funding tree + cluster membership
create table if not exists wallets (
  address        text primary key,
  first_seen     timestamptz not null default now(),
  cluster_id     text,                    -- deterministic "rug DNA" cluster hash
  funded_by      text,                    -- first inbound SOL source
  funded_at      timestamptz,
  funding_source_type text,               -- cex | bridge | wallet | unknown
  tags           text[] default '{}',     -- kol_precursor | copier | deployer | bot
  meta           jsonb default '{}'::jsonb
);
create index if not exists wallets_cluster_idx on wallets(cluster_id);

create table if not exists wallet_edges (
  src        text not null,
  dst        text not null,
  kind       text not null,              -- funded | cosigned | same_cex_route
  amount_sol numeric,
  observed_at timestamptz not null default now(),
  primary key (src, dst, kind)
);

-- Per-token outcome, used to score deployer clusters
create table if not exists token_outcomes (
  mint           text primary key references tokens(mint),
  peak_mcap_usd  numeric,
  peak_at        timestamptz,
  peak_multiple  numeric,                 -- vs first trade price
  rugged         boolean,
  rug_at         timestamptz,
  rug_reason     text,                    -- lp_pull | deployer_dump | mint_abuse | none
  holders_peak   int,
  holders_now    int,
  evaluated_at   timestamptz not null default now()
);

create table if not exists cluster_scores (
  cluster_id     text primary key,
  tokens_total   int not null,
  tokens_rugged  int not null,
  tokens_10x     int not null,
  score          numeric not null,        -- 0..1
  updated_at     timestamptz not null default now()
);

-- Calls (public t.me/s pages) and inferred on-chain "call moments"
create table if not exists calls (
  id          bigserial primary key,
  source      text not null,             -- tg:<channel> | onchain:volume_spike
  mint        text not null,
  called_at   timestamptz not null,
  mcap_at_call numeric,
  raw         jsonb default '{}'::jsonb
);
create index if not exists calls_mint_idx on calls(mint, called_at);

-- Every buy/sell we observe for tracked mints (thin, for KOL reverse-map & exits)
create table if not exists trades (
  sig         text primary key,
  mint        text not null,
  wallet      text not null,
  side        text not null,             -- buy | sell
  sol_amount  numeric not null,
  token_amount numeric,
  slot        bigint,
  ts          timestamptz not null
);
create index if not exists trades_mint_ts_idx on trades(mint, ts);
create index if not exists trades_wallet_idx on trades(wallet);

-- Decision log: full input snapshot for each candidate
create table if not exists signals (
  id            bigserial primary key,
  mint          text not null,
  ts            timestamptz not null default now(),
  params_version int not null,
  lineage_score numeric,
  kol_score     numeric,
  survivor_score numeric,
  narrative_score numeric,
  total_score   numeric not null,
  decision      text not null,           -- enter | skip | reject
  reject_reason text,
  snapshot      jsonb not null           -- liquidity, holders, bundle%, etc.
);

create table if not exists positions (
  id            bigserial primary key,
  mint          text not null,
  signal_id     bigint references signals(id),
  mode          text not null,           -- paper | live
  opened_at     timestamptz not null default now(),
  closed_at     timestamptz,
  entry_sol     numeric not null,
  entry_price   numeric not null,
  size_tokens   numeric not null,
  remaining_tokens numeric not null,
  peak_price    numeric,
  realized_sol  numeric default 0,
  exit_reason   text,                    -- take_initial | trailing | hard_stop | holder_drop | dead_volume | deployer_sell | kol_sell
  pnl_sol       numeric
);

create table if not exists fills (
  id           bigserial primary key,
  position_id  bigint references positions(id),
  ts           timestamptz not null default now(),
  side         text not null,
  sol_amount   numeric not null,
  token_amount numeric not null,
  price        numeric not null,
  sig          text,
  reason       text
);

-- Self-improvement loop: one hypothesis = one bounded params diff
create table if not exists hypotheses (
  id             bigserial primary key,
  created_at     timestamptz not null default now(),
  branch         text not null,
  params_diff    jsonb not null,
  rationale      text not null,
  metric         text not null,          -- e.g. expectancy_sol, hit_rate, max_dd
  baseline_value numeric,
  test_value     numeric,
  eval_start     timestamptz,
  eval_end       timestamptz,
  status         text not null default 'proposed'  -- proposed | testing | accepted | reverted
);
