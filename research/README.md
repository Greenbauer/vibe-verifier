# Research tools

Read-only historical probes for the [pstack comparison](2026-10-03-pstack-comparison.md).
These scripts produce candidate evidence, not defect rates or release gates.
They use Python's standard library, Git, and (for metadata collection) authenticated
`gh`. Gate replays additionally use each gate's normal pinned tools. They target
macOS and Linux; repository CI runs on Ubuntu.

| Script | Output and limits |
|---|---|
| `fetch_prs.sh OWNER NAME > prs.jsonl` | PR metadata including target branch, changed-file and commit counts. PRs are paginated; nested files/commits are capped at 100. Truncated commit metadata is refused by the analyzer and must be completed with pagination before use. |
| `pr_outcomes.py --repo CLONE --prs prs.jsonl --base-branch main --observed-at ISO_TIMESTAMP --format json` | Later title-classified fix touches within `--window-days` (default 14). Only mature changes enter touch rates. Counts commits, not pushes. Supply the actual default branch. |
| `gate_backtest.py --repo CLONE --outcomes out.json --manifest FILE --format json` | Today's gate code against historical merged heads and first parents. Preserves pass/violation/cannot-run and full diagnostics. Association tables are not precision or false-alarm scores. |
| `mutation_score.py --target gates/x.py --tests test_x.py` | Single-operator mutants in a scratch worktree of committed HEAD. Source and tests use that same snapshot. Reports survivors and unmeasured runs; the score excludes timeouts and empty test selections. |
| `size_backtest.py --outcomes out.json` | Exploratory size versus later-fix-touch association on mature changes. No causal inference or gate recommendation. |
| `test_oracles.py --repo CLONE --outcomes out.json --labels labels.csv --format json` | A lexical candidate finder for touched JS/Python tests. Legacy labels `strong/weak/none` do not establish test quality. Hand-read every flag and the production function. |
| `backtest_repos.sh OUT_DIR OWNER/NAME[=MANIFEST]...` | Collects metadata and runs the probes, except mutation testing. Stores complete JSON and label CSV files outside this public repository. `OBSERVED_AT` fixes the snapshot; `SINCE` restricts source merges. |

Use the real wrapper manifest for wrapper-subscribed repositories. The batch driver
otherwise uses the clone's manifest or a clearly named `catalog-diff-gates` fallback;
the fallback is a research profile, not evidence of what that consumer ran. Branch
name checks are removed because a detached historical commit has no PR branch.
`--soak` is removed for observation of underlying violations, not to alter live policy.
Gates needing historical external receipts, artifacts, or installed tools may be
unrunnable; never count those as passes. Read diagnostics before publishing any
finding: source metadata and gate output can contain private information.

## Reproducible interpretation

Record source SHAs, metadata snapshot, target branch, selection/exclusion criteria,
manifest, tool revision, runtime and raw outputs. Freeze a cohort before inspecting
outcomes. Keep later fix PRs available for attribution even when source PRs are
restricted with `--since`. Exclude stacked merges to avoid duplicate ownership.

- A title beginning with fix/revert is a heuristic, not a verified defect.
- SZZ links rewritten lines, not causality. Documentation and old bugs can create
  links, while pure-addition fixes remain `unattributed_fix_hunks`.
- A source PR with less than a complete follow-up window is right-censored, not clean.
- Partial file metadata cannot establish a docs-only change.
- A later-fix-touch association does not make a security finding a false alarm.
- The oracle scanner misses multiline assertions, helper assertions, SQL and
  other test languages; a copied implementation can still look strong. It is
  unsuitable for a blocking test-quality gate.
- A surviving mutant may be equivalent; inspect it before adding a test. A
  timeout or missing baseline cannot establish sensitivity.
- Lead time is open-to-merge wall time, not engineering time, and commits are
  not pushes. Neither establishes shipping efficiency.

Results belong outside this public repository. The batch driver rejects output
paths inside it, including symlinks into it. Artifacts are deliberately retained
locally and regenerated from their input snapshots; delete them manually once no
longer needed. Nothing here writes to a product database or pushes a repository.
