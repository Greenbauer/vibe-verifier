# Research tools

Scripts that measure whether a gate or practice changes outcomes, used by the
[pstack comparison](2026-10-03-pstack-comparison.md) to test each proposed change before it ships.
Python standard library only. Nothing here is a gate: none of it is under a release path, so
changing it never makes a consumer's pin stale.

| Script | Answers |
|---|---|
| `fetch_prs.sh OWNER NAME > prs.jsonl` | Saves every PR's metadata (size, pushes, review threads, files, commit SHAs) through `gh api graphql`. |
| `pr_outcomes.py --repo CLONE --prs prs.jsonl` | Which merged PRs had lines rewritten by a later fix within `--window-days` (SZZ), the rework share, and the escape rate. |
| `gate_backtest.py --repo CLONE --outcomes out.json --manifest FILE` | Replays each merged PR through today's gates and scores every manifest line against those escapes: hits, misses, false alarms. |
| `mutation_score.py --target gates/x.py --tests test_x.py` | How many single-operator mutants of one gate its test module kills, and which survive. |
| `size_backtest.py --outcomes out.json` | Whether a PR-size threshold would have flagged the PRs a later fix rewrote, and at what precision. |
| `test_oracles.py --repo CLONE --outcomes out.json` | Which test cases each PR touched, graded strong, weak, or none, with `--labels` writing the flags to a CSV for hand-labeling. |
| `backtest_repos.sh OUT_DIR OWNER/NAME...` | All of the above, minus mutation scoring, for each repository, read-only, into a directory outside this repository. |

A typical pass, on this repository:

```bash
research/fetch_prs.sh Greenbauer vibe-verifier > /tmp/prs.jsonl
python3 research/pr_outcomes.py --repo . --prs /tmp/prs.jsonl --format json > /tmp/outcomes.json
python3 research/gate_backtest.py --repo . --outcomes /tmp/outcomes.json --manifest .vibe-verifier
python3 research/mutation_score.py --target gates/acceptance_verdict.py --tests test_acceptance.py
```

Limits worth knowing before trusting a number:

- **SZZ is a proxy.** A fix that rewrites a line is not proof the line was a bug, and a fix that only
  adds lines cannot be blamed (it is counted as `unattributed_fix_hunks`). Bigger PRs own more lines,
  so they are blamed more often by chance alone.
- **A surviving mutant is a lead.** Some survivors are equivalent (a changed message, a `required`
  flag whose absence ends in the same exit 2). Read each one before calling it a gap.
- **Small histories give directional numbers.** Report n with every rate.

The comparison, its evidence, and the test-and-implement plan are in
[2026-10-03-pstack-comparison.md](2026-10-03-pstack-comparison.md).
