-- outcome rules v4 (09-27): dead tokens and the multiple reachable after entry
alter table token_outcomes add column if not exists entry_multiple numeric;
alter table cluster_scores add column if not exists tokens_dead int not null default 0;
alter table token_outcomes add column if not exists rules_version int;
