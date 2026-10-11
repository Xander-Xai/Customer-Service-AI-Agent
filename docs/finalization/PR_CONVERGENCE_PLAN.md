# PR Convergence Plan — #1 → #2 → #3 → #4, and what was actually done

> **Outcome: DONE.** The stack was collapsed into **one** integrated PR and
> merged. No force-push, no rebase, no history rewrite.
>
> | | |
> |---|---|
> | Merged SHA on `main` | **`1c6047c59745d8d7c7207160ec6d4b435438b1e8`** |
> | Merge commit | `1c6047c` — "Merge pull request #4 …" |
> | Commits integrated | 15 (`main b6757b4` → `870a85a`), all preserved |
> | Tree check | `git diff 870a85a 1c6047c` → **0 files differing** |
> | `main` CI | **all jobs green** (incl. `build-and-push`) |
> | Remote branches after | `main` only |
> | PR #1–#4 | all MERGED; Superseded notes posted on #1/#2/#3 |

Sections 1–4 are the audited plan; §5 is what was executed; §8 records the
final state.

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
- Merge happened only after: all six required checks green, evidence
  `tested_code_sha` == `git HEAD` with `dirty: false`, and the P0 regression
  suites green on real PostgreSQL.

## 8. Final state, as executed

### 8.1 What GitHub did on its own

When #4 merged, GitHub **auto-marked #1, #2 and #3 as MERGED** (their heads are
ancestors of the merge commit, so their commits landed in the base) and
**auto-deleted all four `fix/*` branches** via the repository's
delete-branch-on-merge setting. The `--delete-branch=false` flag applied to #4
only; the other three were closed as a side effect of being stacked beneath it.

This was verified safe before accepting it:

```
b96c740  reachable from main   OK
821037a  reachable from main   OK
4944044  reachable from main   OK
1c61ae9  reachable from main   OK
92aac78  reachable from main   OK      (P0 fix)
870a85a  reachable from main   OK
git diff 870a85a 1c6047c  →  0 files differing
```

Deleting a branch that is an ancestor of `main` destroys nothing: the commits
remain reachable from `main` permanently.

### 8.2 Recovery, had anything been needed

```bash
# restore any deleted branch from its recorded SHA
git branch fix/agent-runtime-eval-finalization          b96c740d8c65e8ae84e71660f73935ae2b77e5f4
git branch fix/eval-reviewed-gold-credibility           821037a4ee939e3b1e567c53c252c6f53924fc0a
git branch fix/runtime-hitl-reliability-hardening       4944044653e406d7d9661ed4a39fa131f9c1375e
git branch fix/finalization-eval-runtime-observability 870a85a94e1a72cabdea2c02e9183645326d02bd
```

| Branch | Head SHA at merge |
|---|---|
| `fix/agent-runtime-eval-finalization` | `b96c740d8c65e8ae84e71660f73935ae2b77e5f4` |
| `fix/eval-reviewed-gold-credibility` | `821037a4ee939e3b1e567c53c252c6f53924fc0a` |
| `fix/runtime-hitl-reliability-hardening` | `4944044653e406d7d9661ed4a39fa131f9c1375e` |
| `fix/finalization-eval-runtime-observability` | `870a85a94e1a72cabdea2c02e9183645326d02bd` |

### 8.3 Gates checked before merging

| Gate | Result |
|---|---|
| `test (3.10/3.11/3.12)`, `dev-compat`, `security`, `runtime-e2e` | SUCCESS |
| `lint`, `agent-eval`, `metrics-alerting` | SUCCESS |
| `build-and-push` / `deploy` | skipped on PR, `build-and-push` green on `main` |
| Review threads / review comments | none outstanding; policy requires 0 approvals |
| Evidence `tested_code_sha` vs `git HEAD` | `870a85a…` == `870a85a…`, `dirty: false` |
| Local P0 suites | unit 13/13, real PostgreSQL 7/7, `make runtime-e2e` 110 passed |