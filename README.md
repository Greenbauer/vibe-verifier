# Vibe Verifier

The CI source of truth for verifying vibe-coded pull requests: a catalog of gates, harnesses and
contracts that subscribed repositories pin by commit, plus the management loop that keeps every
subscriber current. Modelled on the CI-management half of a private predecessor, not on a
reusable-workflow library: a library can make a check available, but only apply-down can make a
repository adopt it.

Gates only. No infrastructure, no paid dependency, open source throughout. See
[docs/GATE-CONTRACT.md](docs/GATE-CONTRACT.md) for the contract every gate obeys and
[SECURITY.md](SECURITY.md) for what counts as a vulnerability here.

## Scope: gates only

**Vibe Verifier carries gates, harnesses and contracts. Infrastructure is out of scope.** The dividing
line: Vibe Verifier owns *logic*, a gate on the working tree or a harness that drives infrastructure
the repository operates, and never owns the infrastructure itself. `new-source-has-test` needs a
diff, so it is a gate. A preview-e2e *skeleton* (resolve, seed, run, adjudicate, verdict) is a
harness. The preview environment it runs against belongs to the repository.

That line is drawn from measurement, not taste: in the repositories this grew out of, the strictest
CIs spent 12-16% of their commits on CI upkeep, and nearly all of it was runner and
preview-environment plumbing rather than gate logic.

Gates run **on whatever**: any runner, any CI provider, and a laptop. In short:

```
gates/<name>.py          Python standard library + git. Runs anywhere.
gates/_tools.py          the pinned tools a gate may need (version + sha256 per platform): PATH, then cache, then a verified download, else exit 2
gates/_workflows.py      which workflows a workflow linter judges: those changed since the base, or every tracked one with --all
tools/<name>/            a node toolchain a gate or a harness depends on, pinned by package.json + package-lock.json; installed once per lockfile digest with npm ci (`bin/vibe-verifier tool <name>` prints where)
  --base-ref <ref>       $VIBE_VERIFIER_BASE_REF -> $GITHUB_BASE_REF -> main -> master -> HEAD~1
  --format text|github|json
  --soak                 report findings, exit 0 anyway
  exits: 0 pass, 1 violations, 2 could not run
  ships: tests proving it fires on bad input, stays silent on good, exits 2 when it cannot run

bin/vibe-verifier        runs the gates a repo lists in its .vibe-verifier manifest;
                         `consumers` is the read-only inventory and drift check of subscribed repos (every catalog pin under .github/workflows/, whether a review.yml copy matches the harness outside its pin line, and the native enforcement GitHub holds: SHA pinning, the actions allowlist, Dependabot alerts);
                         `apply-down` opens one PR per consumer bumping its stale pins, plan then confirm
actions/gates/           composite action: the same runner, in CI; the caller owns runs-on
actions/criteria/        composite action: reads a PR body's criteria, so a harness skips what a PR declares it has none of
actions/qae-browser/     composite action: the pinned browser toolchain the QAE explorer runs with
consumer/                the canonical ten-line workflow stub a consumer copies; only its pin line varies
harnesses/qae/           the QAE harness: an explorer workflow template, its prompt, the verify manifest, the rules
harnesses/review/        the review harness: the model reviews under CLAUDE.md, the workflow posts the receipt, review-receipt gates on it
```

`--soak` is part of every gate, so each one gets the observe-then-promote ratchet for free, and it
never masks exit 2: a gate that cannot run can never present as one that passed.

## Quick start

```bash
bin/vibe-verifier list
bin/vibe-verifier run --repo /path/to/a/repo --manifest /path/to/.vibe-verifier
bin/vibe-verifier consumers --owner <github-owner>   # needs an admin gh login: it reads the native settings too
python3 -m unittest discover -s tests
```

## Subscribing a repository

1. Copy [`consumer/vibe-verifier.yml`](consumer/vibe-verifier.yml) to `.github/workflows/vibe-verifier.yml`
   and pin `actions/gates` to the commit you subscribe to. The stub is owned here; only its pin line varies.
2. Write `.vibe-verifier`: one gate per line with its arguments. The manifest is owned by the
   repository and judged from the base branch, so a pull request cannot switch its own gates off.
3. Optionally copy a harness: [`harnesses/review/`](harnesses/review/README.md) for the house-rules
   review with an exact-head receipt, [`harnesses/qae/`](harnesses/qae/README.md) for browser-driven
   acceptance checks that deterministic gates adjudicate.
4. Keep current with `bin/vibe-verifier consumers` (the drift check) and `apply-down` (one pin-bump
   pull request per stale consumer). Every catalog pin under `.github/workflows/` is inventoried.

Turn on `sha_pinning_required` in the repository's Actions settings; the catalog never pins anything
by tag, and neither should its consumers.

## Shape

Three layers.

1. **Enforcement (native).** `sha_pinning_required` on, `allowed_actions: selected` with the
   action allowlist, Dependabot alerts (see
   [docs/GATE-CONTRACT.md](docs/GATE-CONTRACT.md#enforcement-native)), and required checks through
   branch rules. No new repository needed for any of it.
2. **Management.** Inventory and drift check of subscribed repositories (`bin/vibe-verifier
   consumers`: pin current, stale or unknown; stub canonical or not; manifest valid or not; harness
   copies matching; the native enforcement above) and apply-down of the pin bump as plan-then-PR (`apply-down`); tests of the
   machinery itself. The stub is owned here and the manifest by the consumer, so there is no
   ownership manifest to declare.
3. **Catalog.** `gates/`, `harnesses/`, `actions/`, `tools/`: secret-free, no inventory, no privileged
   mutation, no per-consumer copies.

## Catalog

**Gates**: checks on the working tree (pure), on declared inputs (wired), on a pinned tool's output
(tool-backed), or differential against the base (ratchets). Every gate obeys one contract
(`--base-ref`, `--config`, `--format`, `--soak`; exit 0/1/2) and ships with a self-test.

| Gate | Kind | What it refuses |
|---|---|---|
| `new-source-has-test` | pure | a new source file with no test that names or imports it |
| `branch-name-length` | pure | a branch whose slug exceeds a preview host's subdomain budget |
| `no-duplicate-package-json-keys` | pure | a `package.json` whose parser silently keeps the last of two keys |
| `build-tools-in-devdependencies` | pure | a build tool shipped as a runtime dependency |
| `gitleaks` (pinned 8.30.1) | tool-backed | a secret in the pull request's commits (`base..HEAD`), redacted; gitleaks' rules or the repository's `.gitleaks.toml` |
| `actionlint` (pinned 1.7.12) | tool-backed | a workflow the pull request changed that actionlint rejects (`--all` for every one); shellcheck and pyflakes off so a laptop and a runner agree |
| `zizmor` (pinned 1.30.1) | tool-backed | template injection, unpinned actions, persisted credentials or excessive permissions in a changed workflow; `--offline`; the repository's `zizmor.yml` ignores are honoured |
| `cognitive-complexity` (15) | ratchet | a changed file with more functions over the limit than it had at the base, or a new one with any; SonarSource's metric via `eslint-plugin-sonarjs`, toolchain pinned in `tools/cognitive-complexity/` |
| `max-file-lines` (500) | ratchet | a changed file over the cap that grew, or a new one over it |
| `acceptance-verdict` | wired | a verdict without one anchored PASS per acceptance criterion, anchors resolved at the head or in the run's artifacts |
| `qae-artifacts` | wired | an explorer run whose artifacts do not back its steps: a step without its screenshot, console errors or failed requests outside what the repository declares, no session or network record |
| `review-receipt` | wired | a head no review receipt names, or unresolved review threads |

The ratchets are differential, not backlogs: what nobody touched never blocks, and `--all` measures
everything for an audit or a first subscription.

**Harnesses**: parameterised skeletons the catalog owns; the repository supplies config, baselines,
specs and identities.

| Harness | Status |
|---|---|
| review (per-PR house-rules review) | built: the model posts inline findings under the repository's `CLAUDE.md` pinned to the base, the workflow posts the exact-head receipt, `review-receipt` gates on receipt and threads |
| QAE (per-PR acceptance criteria in a real browser) | built: the explorer walks the criteria with `@playwright/mcp` and writes artifacts, `acceptance-verdict` and `qae-artifacts` decide |
| geometry audit | planned |
| preview-e2e (resolve, seed, warm, run, adjudicate, verdict) | planned |

**Contracts**: rules every harness must obey, each born from a real incident. Verdict status (pending
never downgrades a verdict); base-trusted inputs (a pull request's manifest is judged from the base,
and the review harness loads `CLAUDE.md` from the base; the workflow file itself is head-controlled
until branch rules exist, so this is a guard, not a guarantee); proof anchors (citing a test is not
running it); exact-head receipts; the artifacts decide, never the model's prose.

Explicitly **not** here: action pinning and the action allowlist (native `sha_pinning_required` and
`allowed_actions: selected`), dependency vulnerability alerting (native Dependabot alerts), branch
rules (native), any domain-specific gate, and any infrastructure. Repository-specific checks work *because* they are
repository-specific.

## Rules

- **Free by default.** Nothing is paid for unless it is critical and has no free route. No paid
  service is ever a dependency of a gate; heavy gates take `runs-on` as an input and do not default
  to hosted runners.
- **Rule of Three.** A workflow enters only at 3+ live consumers, never seeded from "all the CIs".
- **Never `secrets: inherit`.** Declare secrets explicitly, callers pass by name, least-privilege
  `permissions:`.
- **No upward absorption** from live copies. (A sibling project paid 65 commits to learn this.)
- **Composite-first.** Logic in composite actions, workflows thin, externals SHA-pinned internally.
- Each repository keeps one clear CI entry point and owns its own test graph.

## Prior art

No existing project replaces this; the category has no winner. The design borrows from
CircleCI orbs (versioning), Renovate presets (indirection), GitLab CI/CD components (typed inputs),
`hoverkraft-tech/ci-github-common` (composite-first layout), and Trunk (ratchet on legacy findings).

## License

Apache-2.0. See [LICENSE](LICENSE).
