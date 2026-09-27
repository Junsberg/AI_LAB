-- lineage_v0 paper runner (09-27)
alter table positions add column if not exists meta jsonb not null default '{}'::jsonb;
-- one open position per mint and mode: a rerun or overlapping job cannot double-enter
create unique index if not exists positions_open_mint_mode_uidx on positions(mint, mode) where closed_at is null;
-- one decision per mint per params version
create unique index if not exists signals_mint_version_uidx on signals(mint, params_version);
create index if not exists positions_open_idx on positions(mode) where closed_at is null;
