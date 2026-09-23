---
name: evaluate-change
description: Judge a hypothesis branch against baseline — replay first, then paper window — and either merge (accept) or delete the branch (revert). Records the verdict in the hypotheses table.
---

# evaluate-change

Input: hypothesis id (or branch name).

## Steps
1. **Replay**: run `python -m memebot.review.replay --params <branch params> --since <eval_start>` and the same with baseline params over identical stored `signals`/`trades`. Compare the hypothesis `metric`. This is deterministic; if replay is worse, mark `reverted` immediately and stop.
2. **Paper window**: if replay is better, set `status='testing'`, `eval_start=now()`. The paper runner must load params from the branch. Minimum window: 7 days AND ≥ 30 closed positions, whichever is later.
3. **Verdict**: after the window, compute the metric on the window for hypothesis vs the baseline branch's paper run over the same window (both run in parallel in paper mode). Accept only if improvement > 1 standard error of the metric; otherwise revert. Never accept on n < 30.
4. Accepted → merge branch into the working branch, `status='accepted'`, `test_value` filled. Reverted → `git branch -D`, `status='reverted'`, keep the row (negative results are data).
5. Append one line to `docs/HYPOTHESES.md`: `#id | key old→new | metric base→test | verdict | n`.

Anti-overfit guardrails: max 1 accepted change per 7 days; a change reverted twice is banned for 60 days (check the table before proposing).
