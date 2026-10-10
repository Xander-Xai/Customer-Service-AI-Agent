# PR Convergence Plan — safe integration of #1 → #2 → #3

> **This is a recommendation, not an action.** Nothing here was auto-executed.
> No PR was merged, closed, or force-pushed. Any rebase/force-push below requires
> explicit human approval.

---

## 1. Audited state (GitHub, current)

| PR | head branch | base branch | unique commits | files | CI | mergeable |
|---|---|---|---|---|---|---|
| **#1** | `fix/agent-runtime-eval-finalization` | `main` | 10 (`main..head`) | 82 (+13589/−162) | ✅ all green (lint/test 3.10/3.11/3.12/security/agent-eval/runtime-e2e/metrics-alerting) | MERGEABLE / clean |
| **#2** | `fix/eval-reviewed-gold-credibility` | `fix/agent-runtime-eval-finalization` (PR #1) | 2 (`c67e8ed`, `821037a`) | 17 (+1533/−21) | ⬜ none (non-main base) | MERGEABLE / clean |
| **#3** | `fix/runtime-hitl-reliability-hardening` | `fix/eval-reviewed-gold-credibility` (PR #2) | 1 (`4944044`) | 6 (+561/−3) | ⬜ none (non-main base) | MERGEABLE / clean |

All three are **DRAFT** and **linear-stacked** (confirmed: `#1.head` is the merge
base of `#2`; `#2.head` is the merge base of `#3`). `main` is an ancestor of
`#1.head` (fast-forward-capable).

## 2. The duplication hazard

If #1 is merged to `main` (especially **squash** or **merge commit**), the commits
still sitting in #2/#3 are *descendants of the pre-merge #1 commits*. Until those
branches are rebased, their PR diffs re-include #1's changes → reviewers see the
same 82 files twice and a merge would double-apply (or conflict).

## 3. Recommended safe sequence (linear restack)

```bash
# 0) freeze; get explicit approval before any remote mutation.
git fetch origin

# 1) land #1 (choose ONE; squash is fine because #2/#3 will be rebased)
#    via GitHub UI: "Squash and merge" PR #1 into main.

# 2) restack #2 onto the new main
git checkout fix/eval-reviewed-gold-credibility
git rebase --onto origin/main <PR1-head-before-merge> fix/eval-reviewed-gold-credibility
#    -> should now be exactly 2 commits; PR #2 diff shrinks to 17 files only.
git push --force-with-lease origin fix/eval-reviewed-gold-credibility   # needs approval

# 3) restack #3 onto the new main (or onto the restacked #2)
git checkout fix/runtime-hitl-reliability-hardening
git rebase --onto origin/main <PR2-head-before-merge> fix/runtime-hitl-reliability-hardening
git push --force-with-lease origin fix/runtime-hitl-reliability-hardening  # needs approval
```

Then GitHub retargets each PR's base to `main`; CI runs against `main`; merge
them in order once green.

> `--force-with-lease` (never bare `--force`). Target branches here are the
> three known PR branches — **no unknown branch is overwritten**.

## 4. Alternative: consolidated single PR (no force-push)

If force-push is undesirable, cherry-pick each stack's unique commits onto a
fresh branch off `main` and open one PR:

```bash
# candidate branches (all land on main; original PRs can be closed by a human)
git checkout -b land/pr2 origin/main && git cherry-pick c67e8ed 821037a
git checkout -b land/pr3 origin/main && git cherry-pick 4944044
# or a single consolidated branch:
git checkout -b land/consolidated origin/main
git cherry-pick c67e8ed 821037a 4944044
```

Trade-off: clean history, but the stacked PR review trail is replaced by new PRs.

## 5. This finalization branch

`fix/finalization-eval-runtime-observability` is branched from #3 head `4944044`
and layered on top:

- real-provider agent-eval lane (`evaluation/agent_eval/real_provider.py`,
  `scripts/evaluate_agent_real.py`, `make agent-eval-real`);
- replay/resilience dataset case + honest `orchestration_coverage` diagnostic;
- reviewed-gold status now runnable pre-annotation (NOT_MEASURABLE, not FATAL);
- docs `docs/finalization/*`.

It should be reviewed **after** #1–#3 land, then rebased onto the result (base
branch `fix/runtime-hitl-reliability-hardening` for now, retarget to `main`
after #3 merges).

## 6. Hard guardrails

1. **No automatic merge** — human triggers every merge.
2. **No close** of any PR as part of this work.
3. **No force-push** to any branch other than the three known PR branches, and
   only after explicit approval, always `--force-with-lease`.
4. **CI must be green on the restacked branch** before merging; #2/#3 currently
   have **no CI runs** because their base is non-main.
