# Repository instructions

## Reporting current project status

1. Read the live repository metadata for the default branch (currently `main`). Resolve its remote head SHA, then read the README, [implementation reference](docs/reference/current-state.md), relevant source, tests and [evidence](docs/production-readiness.md) at that exact SHA. Report the branch, SHA and check time. A local checkout may lag the remote.
2. Read current PR and Issue metadata from GitHub when reporting their state. Use the PR's actual base, draft flag and merged status. Verify that a claimed merge is present in the default branch before counting it as delivered. Use PR bodies, archived reports and conversation notes for historical context.
3. Check Actions for the exact SHA, and state what ran, passed, failed, was skipped or is pending. Report release/tag and deployment evidence separately. When a source is unavailable, say which claim could not be verified.
4. State the environment and dataset behind each finding. Distinguish implemented code, test evidence, measured results and production validation. Keep public-repository evidence separate from personal or employment history.
5. When changing a feature or interface, update its current documentation and existing generated contracts in the same PR. Keep dated audits labelled with the snapshot date.
