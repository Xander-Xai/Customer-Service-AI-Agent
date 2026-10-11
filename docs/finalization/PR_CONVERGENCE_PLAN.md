# PR Convergence Plan — #1 → #2 → #3 → #4, and what was actually done

> **Outcome first:** the stack was collapsed into **one** integrated PR (#4
> retargeted to `main`). No force-push, no rebase, no history rewrite. Sections
> 1–4 are the audited plan; §5 is what was executed.

---

## 1. Audited state (GitHub, at start of this pass)

| PR | head | base | commits ahead of `main` | CI | mergeable |
|---|---|---|---|---|---|
| **#1** | `fix/agent-runtime-eval-finalization` (`b96c740`) | `main` | 10 | ✅ all green | MERGEABLE |
| **#2** | `fix/eval-reviewed-gold-credibility` (`821037a`) | PR #1 | 12 | ⬜ **none** | MERGEABLE |
| **#3** | `fix/runtime-hitl-reliability-hardening` (`4944044`) | PR #2 | 13 | ⬜ **none** | MERGEABLE |
| **#4** | `fix/finalization-eval-runtime-observability` (`1c61ae9`) | PR #3 | 14 | ⬜ **none** | MERGEABLE |

All four were **drafts**, with no review decision recorded.

## 2. The reason #2/#3/#4 had no CI at all

`.github/workflows/ci.yml` triggers on:

```yaml
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]
```

A PR whose base is another feature branch **does not match** `branches: [main]`,
so no workflow runs. PRs #2, #3 and #4 were therefore **never CI-verified** —
not "CI was skipped", but "CI never executed". This is the single strongest
argument for collapsing to one PR targeting `main`.

## 3. Ancestry (re-verified, not assumed)

```
main b6757b4
  └── #1 b96c740   (main..#1 = 10 commits)
        └── #2 821037a   (12)
              └── #3 4944044   (13)
                    └── #4 1c61ae9   (14)
```

`git merge-base --is-ancestor` confirmed each step, and that `b96c740`, `821037a`
and `4944044` are all contained in `1c61ae9`. The stack is strictly linear, so
retargeting #4 to `main` changes its diff from "PR #3 → #4" to "main → #4"
with **no semantic conflict**.

## 4. Integrated-diff sanity check

`git diff --stat b6757b4..1c61ae9` → **99 files, +17607 / −165**.

- **Zero** files deleted outright (`--diff-filter=D` count = 0).
- Every file with deletions has far larger additions; the largest deletions are
  doc edits (`docs/limitations.md` −46/+102, `docs/production-readiness.md`
  −16/+31, `README.md` −11/+28). No duplicated change, no accidental content loss.

## 5. What was executed

1. Fixed the P0 recovery defect on top of #4 (see `EVIDENCE_MANIFEST.md` §2).
   Commit `92aac78`, branch `fix/finalization-eval-runtime-observability`.
2. Pushed, then retargeted **PR #4 base: `fix/runtime-hitl-reliability-hardening` → `main`**.
   No force-push was used at any point; the base change is a PR-setting change.
3. A base change emits `edited`, not `synchronize`, so it did not by itself
   trigger CI. The docs commit that followed the retarget supplied the
   `synchronize` event that ran the full suite against `main`.
4. Merge only after: required checks green, no unresolved review blockers, and
   the P0 regression suites passing on real PostgreSQL/Redis.

### Why single-PR beat sequential merging

| Option | Consequence |
|---|---|
| Merge #1→#2→#3→#4 in order | #2/#3/#4 would have been merged **without ever having run CI**; a 14-commit stack reviewed in four fragments where only the first fragment was verified |
| Collapse into #4 on `main` | One 99-file diff, one full CI run, one reviewable unit |

## 6. Residual PR / branch handling

- #1/#2/#3 are left **open** until #4 lands, so the stacked bases they depend on
  are never removed from under them.
- After #4 merges, each gets a `Superseded by #4` note and is closed by a human
  decision — all three branches are ancestors of the merged SHA, so nothing is
  lost.
- Branch deletion is gated on: no unique unmerged commits, no open PR referencing
  it, no active worktree. Recorded SHAs and recovery commands before deletion.

## 7. Guardrails honoured

- No force-push to any branch. No rebase. No default-branch history rewrite.
- No PR force-merged; branch protection (`test 3.10/3.11/3.12`, `dev-compat`,
  `security`, `runtime-e2e` required) stayed in force throughout.
- No PR closed before its replacement actually merged.