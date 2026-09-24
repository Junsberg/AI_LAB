alter table cluster_scores add column if not exists tokens_evaluated int not null default 0;
