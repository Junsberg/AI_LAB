---
name: propose-change
description: Turn one HYPOTHESIS line (from review-performance) into a git branch with a single bounded edit to strategy/params.yaml and a row in the hypotheses table. One hypothesis per branch, always.
---

# propose-change

Input: exactly one line `HYPOTHESIS: <path> <old> → <new> | metric=<m> | rationale=<r>`.

## Rules (hard)
- `<path>` must be under `exits.`, `weights.` or `thresholds.`. Refuse anything under `risk.` — the PreToolUse hook will also block it.
- Bounded drift: numeric change ≤ ±30% of the old value (weights may go to 0 to disable a signal). Larger → refuse and say why.
- One key per branch. If the review suggests two, pick the one with larger |expectancy delta| and note the other as "next".

## Steps
1. `git checkout -b hyp/<YYYYMMDD>-<short-key>` from the current branch.
2. Edit only that key in `strategy/params.yaml`; bump `version` by 1.
3. `python -m pytest -q` must pass.
4. Insert into `hypotheses` (branch, params_diff as JSON `{path:{old,new}}`, rationale, metric, status='proposed').
5. Commit `hyp: <path> <old> → <new>` and print the branch name + hypothesis id. Do not merge, do not push to main-line branch.
