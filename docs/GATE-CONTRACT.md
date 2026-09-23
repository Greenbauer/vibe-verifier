# The gate contract

A gate is a command-line program that reads a working tree and its git history, and nothing
else: no secrets, no network, no services. That is what lets the same gate run on a laptop, in a
pre-push hook, or on any CI runner.

## Flags every gate accepts

| Flag | Meaning |
|---|---|
| `--repo PATH` | Repository root. Default: the current directory. |
| `--base-ref REF` | What a differential gate compares `HEAD` against. |
| `--format text\|github\|json` | `text` for people, `github` for PR annotations, `json` for tools. Default `text`. |
| `--soak` | Report violations but exit 0. |

Gate-specific options (`--max`, `--source`, `--exclude`) are documented in each gate's `--help`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Pass. |
| 1 | Violations found. |
| 2 | Could not run: bad usage, missing history, a crash. |

**`--soak` never masks exit 2.** Soak exists so a new gate can collect signal before it blocks
anything. A gate that cannot run has produced no signal, and a soak that is silently broken looks
exactly like a soak that is clean. A mutation-testing soak in a sibling project produced no score
for five days before anyone noticed. A broken gate gets fixed or unsubscribed, never ignored.

## Base ref resolution

An explicit `--base-ref` must resolve, or the gate exits 2: falling back silently would check the
wrong diff. Without one, the order is `$VIBE_VERIFIER_BASE_REF`, `$GITHUB_BASE_REF`, `main`,
`master`, then `HEAD~1`. Each name is tried as `origin/<name>` first. In CI, check out with
`fetch-depth: 0` so the base exists.

## Subscribing: the manifest

A repository subscribes with a plain text file, `.vibe-verifier`, one gate per line:

```
# .vibe-verifier
no-duplicate-package-json-keys
build-tools-in-devdependencies
new-source-has-test --source 'lib/**/*.ts' --soak
branch-name-length --max 37
```

It is line-based on purpose. Neither Python nor Node can parse YAML or TOML from the standard
library on every supported version, and zero dependencies is what "runs anywhere" means. Per-gate
configuration is the gate's own arguments, so there is no second config file.

Run it locally from a clone of this repository:

```bash
path/to/vibe-verifier/bin/vibe-verifier run --repo .
path/to/vibe-verifier/bin/vibe-verifier list
```

The run's exit code is the aggregate: 2 if any gate could not run, else 1 if any gate found
violations, else 0. A missing manifest, an empty manifest and an unknown gate are all exit 2. A run
that verified nothing must not look like a pass.

On a pull request the manifest is judged from the base. The runner resolves the base the way the
gates do (`--base-ref`, then `$VIBE_VERIFIER_BASE_REF`, `$GITHUB_BASE_REF`, `main`, `master`,
`HEAD~1`) and, when a manifest exists there, runs the union of the base's and the head's gates,
with a gate in both taking the base's arguments. A branch that adds a gate is bitten by it now; one
that removes a gate, appends `--soak` or raises a `--max` is still judged by the line it started
from, and the run says so. With no base, or no manifest at the base (a first subscription, a laptop
with no history), the head's manifest stands alone. Two consequences: promoting a gate by removing
`--soak` passes on its own PR and gates the next one, which is the observe-then-promote order; and
a gate leaving the catalog is sequenced (every consumer drops its line first, the gate goes a
release later), or the base's line names a gate that no longer exists and every run is exit 2.
A promotion (a branch that removes `--soak`) is judged with the base's `--soak`, so the gate it
promotes does not block on that branch: for the differential gates a manifest-only branch changes
no source anyway, and for the others the soak period has been printing every pre-existing finding
as a warning on every pull request, so a backlog at promotion time is already known.

What this is not: on GitHub, a `pull_request` workflow runs from the PR's own head, and so does
the stub that calls this runner. Without branch protection (rulesets can require a workflow from a
trusted ref) a PR can change the workflow file itself. Judging the manifest from the base is a guard
against a branch quietly weakening its own manifest, the likely failure with agent-authored PRs;
it is not a platform guarantee, and the same holds for the tools' own escape hatches
(`gitleaks:allow`, `# zizmor: ignore[...]`), which are documented, head-controlled, and visible in
the diff, where the review harness reads them.

A `pull_request_target` stub (workflow and manifest from the default branch, the head checked out
by SHA, no secrets, `contents: read`) would close all three natively and was considered
(2026-09-22). It was not taken because a pin bump would then run under the old catalog rather than
the one it introduces, a setup or gate-adding pull request would run only after merging, and the
stub itself would trip the catalog's own `zizmor` (`dangerous-triggers`) and ship with a permanent
ignore. The merge guard on the operator's machine refuses an agent merge of any pull request that
modifies an existing stub or manifest, which is where that decision is enforced. For the workflow
file, an organization can close the gap natively: see [Wrappers](#wrappers).

## Subscribing in CI

The consumer's workflow calls the composite action directly. The workflow owns `runs-on`, so a gate
runs on whatever runner the repository chooses. The canonical stub is
[`consumer/vibe-verifier.yml`](../consumer/vibe-verifier.yml): copy it to
`.github/workflows/vibe-verifier.yml` and replace the placeholder on the last line with the commit
SHA of this repository to pin. That line is the only line a consumer edits.

Keep that job free of `if:` conditions. GitHub reports an `if`-false job as `skipped`, and counts a
skipped required check as satisfied.

`bin/vibe-verifier apply-down` bumps stale pins and nothing else: the stub's, and every catalog
pin in any other workflow under `.github/workflows/` (a harness copy such as `qae-explore.yml` pins
`gates@` and `criteria@`). With no `--confirm` it prints a plan and a digest; `--confirm <digest>`
opens one pull request per repository changing only pin lines, one commit per file. It never
merges, skips a repository whose stub has drifted, and returns an already-open bump instead of
opening a second. A digest that no longer matches is refused, so what is applied is what was read.

Before bumping a pin that moves a tool version (`gates/_tools.py`, or a `tools/<name>/` lockfile),
run that gate with `--all` on every consumer and land the cleanups with the bump. `zizmor` adds
audits about monthly, and a workflow gate judges the whole of any workflow a pull request touches,
so a bump that is not swept re-arms a backlog that lands on whoever next touches `ci.yml`.

Anything a composite action reads through `$GITHUB_ACTION_PATH/../..` must live under a release
path (`gates/`, `bin/`, `actions/`, `consumer/`, `tools/`): that is what makes a change to it a
stale pin that `apply-down` delivers. `harnesses/` is not one, so a harness copy takes a change
only by being edited; when the review harness is next changed for real, it becomes
`actions/review/` with its prompt inside, and the copies shrink to a pin.

`bin/vibe-verifier consumers --owner OWNER` (or `--repo OWNER/NAME`, repeatable) is the read-only
inventory and drift check. Through `gh api` it reads each repository's manifest, its stub, and every
other workflow under `.github/workflows/` that pins a catalog action, and reports whether each pin is
current, stale (a commit since it touched `gates/`, `bin/`, `actions/`, `consumer/` or `tools/`) or
unknown to this history; whether the stub matches the canonical one outside the pin line; whether a
`review.yml` copy matches `harnesses/review/review.yml` outside its pin line (every line of that
harness is the catalog's, so a copy that differs is running a review nobody released; the QAE
template has a consumer-owned build block and is not compared); whether every gate in the
manifest exists; and the native enforcement below. Exit 2 if any repository could not be read, 1
if anything needs action, else 0. It needs an **admin** `gh` login (the native settings are not
readable otherwise), so it runs from an operator's machine or a controller, not from this
repository's own CI. This repository gets a row of its own, labelled `source`: it subscribes
through its own `ci.yml` and carries no stub, so its pin, stub and manifest are not judged and its
native state is.

There is no reusable workflow for pure gates. It could reference its own commit (a called workflow
sees it as `job.workflow_sha`; checked 2026-09-22), but it would take `runs-on` away from the
consumer and add a second pin-line shape for `apply-down` to track. Composite actions do the same
job with the one pin shape there is.

## Enforcement (native)

Three GitHub settings do work no gate can do, because GitHub applies them itself, at job setup,
before any workflow step runs and wherever the job came from. All three are on in the four
repositories this catalog governs (three consumers and this repository), rolled out one repository
at a time on 2026-09-22.

- **`sha_pinning_required`** (on since 2026-09-17): every `uses:` must name a full 40-hex commit.
- **`allowed_actions: selected`**, with `github_owned_allowed: true`, `verified_allowed: false`,
  and the patterns `anthropics/claude-code-action@*` and `oven-sh/setup-bun@*` (plus
  `supabase/setup-cli@*` on the consumer that deploys with it). Those are the whole third-party
  surface: the review harness pins `anthropics/claude-code-action`, which nests exactly one action,
  `oven-sh/setup-bun`, which nests none; that consumer's two deploy workflows pin `supabase/setup-cli`. **Actions owned by the same
  account as the repository are allowed by rule**, which is why `Greenbauer/vibe-verifier/actions/*`
  is not on the list.
- **Dependabot alerts** (`PUT /repos/{owner}/{repo}/vulnerability-alerts`), covering the npm and
  GitHub Actions advisory databases. Alerts only, on purpose: there is no `dependabot.yml`, no
  version updates and no security-update pull requests, because a bump of `claude-code-action` in a
  consumer would drift its harness copy from the catalog template, a bump of `supabase/setup-cli`
  would swap a credentialed deploy job onto a composite that installs from npm, and npm bumps touch
  product files the agent merge grant does not cover. The operator owns the alert signal.

Enforcement reaches an action a workflow never names: a step inside a composite action fails the
same way. Verified both directions on 2026-09-22 — with `oven-sh/setup-bun` missing from the list
the review job failed at `Set up job` naming it, and a scratch job whose local composite used an
unlisted, fully SHA-pinned action failed with "The action ... is not allowed in ... because all
actions must be from a repository owned by ..., created by GitHub, or match one of the patterns".
That is stronger than any file-reading gate, which only sees what the workflow names.

**Drift shows up in the inventory, not in CI.** A workflow's `GITHUB_TOKEN` gets 401 from
`/actions/permissions`, so `bin/vibe-verifier consumers` is where a revert to `all`, a switched-off
alert or a lost SHA-pin surfaces. The pattern list is printed there for review and is not judged:
GitHub holds the only copy and this repository carries no second one to compare against, so a
widened list is visible in the row without failing it. Revert path, per repository:
`allowed_actions: all`, sending `sha_pinning_required=true` on the same PUT so it is not lost.

**When a pull request adopts a new action** the job fails at setup with the message above, which is
the intended stop. The fix is not to widen the list reflexively: read what the action does and what
it nests, decide whether it belongs in the job at all, then add the pattern
(`PUT /repos/{owner}/{repo}/actions/permissions/selected-actions`, sending the whole list — the
endpoint replaces it) and record it in the bullet above in the same change. A pattern is added per
repository, so one consumer carrying `supabase/setup-cli@*` gives the other three nothing.

**Cross-owner consumers need a third pattern.** The same-owner rule covers only repositories
under the catalog's own account. A repository under another owner or an organization that runs
`allowed_actions: selected` cannot use `Greenbauer/vibe-verifier/actions/gates` until its list
carries `Greenbauer/vibe-verifier/*`, the `OWNER/REPO/*` shape GitHub documents for an action that
lives in a subdirectory. Verified 2026-09-23 on the first organization consumer: with the two
patterns above the stub's run ended in `startup_failure` (no job, no log, no check run), and the
rerun passed the moment the third pattern was added. Turn the policy on only after the pattern is
in the list, or every pull request in that repository loses its gate at setup.

## Wrappers

An organization can subscribe its repositories through one CI repository of its own, a
**wrapper**, instead of a stub in each (first done 2026-09-23).

- The wrapper's workflows trigger on `pull_request` and pin the catalog's actions by commit, like a
  stub: a gate workflow whose job is named `vibe-verifier-ok` and runs `actions/gates` against the
  repository's own `.vibe-verifier`, and optionally a copy of the review harness whose `jobs:` are
  the catalog's, byte for byte, apart from the pin and the name of its token secret.
- An **organization ruleset** with the rule "Require workflows to pass before merging"
  (`workflows`) requires them, each pinned by `sha`, on the default branch of the repositories it
  targets. GitHub runs the pinned file in each targeted repository on each pull request, so the
  check name stays `vibe-verifier-ok` (a reusable workflow would report
  `<caller job> / vibe-verifier-ok`) and a pull request cannot edit or remove its own gate, which
  closes the gap above. GitHub documents the rule for Enterprise Cloud only, but an organization on
  the Team plan accepted and enforced it: probed on 2026-09-23, a pull request read `BLOCKED` while
  the required run was queued and `CLEAN` once it passed, although its own tree did not contain the
  workflow.
- GitHub's constraints on ruleset workflows (its troubleshooting guide for rules): only the default
  activity types trigger them (`opened`, `synchronize`, `reopened`), whatever `types:` says; they
  must not use `cancel-in-progress`, which is why a wrapper's review copy has no concurrency group
  and its header is its own; they do not run for events caused by `GITHUB_TOKEN`; a pull request
  already open when its repository joins the ruleset gets its run on the next push; direct pushes to
  the targeted branch are blocked; a private wrapper can be required only in private repositories,
  and its Actions access setting must admit the organization.
- There are two pins: the wrapper's pins of the catalog, and each ruleset's pin of the wrapper.
  `bin/vibe-verifier consumers --wrapper <a checkout of the wrapper>` judges both, and every
  `uses:` of a wrapper workflow still left in a repository (a legacy reusable workflow); a ruleset
  pin is stale when a commit since it touched the workflow it names. `apply-down --wrapper` moves
  them: the wrapper's catalog pins in a pull request to the wrapper, whose own runs are the proof,
  a caller's `uses:` pins in a pull request of its own, and a ruleset's pins with a `PUT` of that
  ruleset's rules once the new commit is on the wrapper's default branch. So a catalog release
  reaches a wrapper's repositories in two runs: one that opens the wrapper's bump, and one after it
  merges.
- Moving a repository from its stub to a wrapper deletes the stub, a workflow that invoked the
  catalog, so the merge guard leaves that pull request to the operator.

## Adding a gate

1. Add `gates/<name_with_underscores>.py`. Build it on `run_gate` from `gates/_contract.py`, which
   supplies the common flags and the exit codes. Raise `CannotRun` when no verdict is possible.
   Python standard library only.
2. Add tests in `tests/` that drive the gate through its command line. A gate does not enter the
   catalog until its tests prove three things: it fires on a known-bad input, it stays silent on a
   known-good one, and it exits 2 when it cannot run.
3. Catalog rule: a gate enters when three live repositories need it.

## Wired gates: declared inputs

A pure gate reads the working tree and git history and nothing else. A wired gate also reads
inputs the caller declares on its manifest line, always as files, so the gate itself never talks
to GitHub, never holds a token, and runs on a laptop from two saved files. The caller (a workflow
step, or a person) fetches whatever the inputs are and writes them down first.

The first wired gate is `acceptance-verdict`:

```
acceptance-verdict --criteria .vibe-verifier-inputs/pr-body.md --verdict .vibe-verifier-inputs/verdict.md
```

- `--criteria` is the PR body; the list under its `## Acceptance criteria` heading is the required
  set, numbered `AC1`, `AC2`, ... in order. A plain file with one criterion per line also works.
- `--verdict` is the verdict text, normally the QAE's PR comment. Every criterion needs exactly one
  line `acceptance-check: ACn -- PASS -- <evidence>`, and the evidence must carry at least one anchor
  that resolves in the working tree: `<path>::<test title>` with the title verbatim in that file,
  or `<path>:<line>` / `<path>:<start>-<end>` inside the file's length.
- `--artifacts DIR` adds a second root anchors may resolve in: the run's own evidence. Browser
  evidence is never a file in the tree, so an explorer writes one step log per criterion into that
  directory (`qae/AC1.md`, one line per step and what was seen) and anchors to a line of it
  (`qae/AC1.md::saw the "invite expired" message`). A tracked path still wins, and a path that
  escapes the directory never resolves.
- The gate never judges whether the evidence covers the criterion's meaning. It refuses a PASS that
  points at nothing, which is the floor; a PASS that points at the wrong real thing is a review
  concern.
- A missing input file is exit 2. A criteria file with no criteria is a finding: a repository that
  subscribes has decided its PRs state them.
- A pull request with nothing to check says so, in the same section: one item reading
  `- None: <why>`. That passes both QAE gates, and the harness's explore job reads the same
  declaration through `actions/criteria` (`bin/vibe-verifier criteria <file>`, which prints
  `none: <reason>` or `criteria: <n>`) to skip the model session. A bare `- None` does not, and neither does silence:
  the declaration is a claim a reviewer can weigh against the diff, and an absent section is
  indistinguishable from forgetting. This is the "declared, not silent" rule the harness contracts
  use everywhere.

In a workflow the two files come from `gh pr view <n> --json body --jq .body` and from the newest
PR comment containing `acceptance-check:`. Who writes that comment is the harness's business (the
plan's explorer); this gate only decides whether what was written is a verdict.

## Wired gates: pinned tools

Some checks are not worth rewriting in the standard library, and a secret scanner is the first:
gitleaks carries the rules, the allowlist grammar (`gitleaks:allow`, `.gitleaks.toml`) and the
maintenance. A gate may depend on such a binary, on these terms, all in
[`gates/_tools.py`](../gates/_tools.py):

- **One pin per tool**: a version and a sha256 per platform, read from the release's own checksum
  file. The pin lives in the catalog, so a consumer takes a new tool version the way it takes a new
  gate, by bumping its action pin.
- **Resolution fails closed.** A binary of that name on PATH is used only if it reports exactly the
  pinned version; otherwise the cache (`$VIBE_VERIFIER_TOOLS`, default `~/.cache/vibe-verifier/tools`);
  otherwise the pinned release asset is downloaded, verified against its sha256 before anything is
  extracted, and cached. No network, an unsupported platform, a digest mismatch or an unwritable cache
  is exit 2, never a pass. `--soak` cannot mask any of it.
- **The gate owns the invocation.** Flags, scope and output parsing are the gate's, tested against the
  real binary and against a stand-in for the edges.

The first is `gitleaks`: it scans the commits since the base ref (`base..HEAD`), so a secret already
in history is a separate clean-up and never a reason to admit a new one; rules follow gitleaks'
precedence (`--config`, then the repository's `.gitleaks.toml`, then the defaults); every report is
redacted. A range with no commits passes without running the tool.

`actionlint` and `zizmor` lint and audit the workflows under `.github/workflows/`. Both judge only
the workflows the pull request added or changed since the base (`--all` judges every tracked one,
for an audit or a first subscription), because a finding in a workflow nobody touched is never this
PR's. They are not differential within a file: every finding in a touched workflow is reported,
including ones the base already had, because the fixes are one-line hardening this catalog asks
for and forgiving them permanently would invert the gate. So a repository subscribes clean: run
`--all` first and land the fixes (the three consumers were swept on 2026-09-22), and sweep again
when the tool pin moves. Both are pinned to the environment: actionlint's shellcheck and pyflakes
integrations are off (they switch themselves on wherever those tools happen to be installed), and
zizmor runs `--offline`. A finding a repository ignores the way the tool documents (`zizmor.yml`, a
`# zizmor: ignore[...]` comment) is not a finding here; zizmor's `--min-severity` and
`--min-confidence` pass through.

### Node toolchains

A tool that lives on npm is pinned differently from a binary: `tools/<name>/package.json` names
exact versions and `package-lock.json` carries every package's tarball integrity, so
`npm ci --ignore-scripts` into the cache (one directory per lockfile digest) reproduces the same
tree wherever it runs, without asking the registry what is current. No node or npm on PATH, or an
install that fails, is exit 2. The first is `tools/cognitive-complexity/` (eslint,
eslint-plugin-sonarjs, the TypeScript parser, typescript). The second is `tools/qae-browser/`
(playwright-mcp and the Playwright build it pins), which the QAE harness installs through
`actions/qae-browser` (`bin/vibe-verifier tool qae-browser`, then that Playwright's chromium) instead
of resolving them from the registry in the job that holds the account-level token. The third is
`tools/codex/` (the Codex CLI and its platform binary), which `actions/qae-codex` installs for the
QAE harness's Codex lane (`harnesses/qae/explore-codex.yml`); that lane holds no model token at all,
because the login lives on the runner (see the harness README).

### Ratchets

`cognitive-complexity` and `max-file-lines` judge the source files a pull request changed, against
the same files at the base: a file is a finding only when it has more functions over the
complexity limit than it had (or is new and has any), or is over the line cap and grew. What
nobody touched never blocks, and a refactor that removes one over-limit function is never undone
by the count. A file the pull request moved is compared with itself at its old path (git's
rename detection), so moving a long or tangled file without changing it never blocks either. That is the lesson of a predecessor's observe stage, which reported main's own nine
findings on every pull request and could not be promoted. `--all` measures everything, for an
audit or to decide a first subscription.

## Harnesses

A harness is a parameterised workflow that produces the declared inputs a wired gate grades. The
first is the QAE harness in [`harnesses/qae/`](../harnesses/qae/README.md): an explore job whose
model drives a real browser and writes step logs, screenshots and a verdict comment; a verify job
that feeds those to `acceptance-verdict`. The consumer owns `runs-on`, how its site is started and
the action pins; the harness owns the prompt, the grammar and the rules.

The harness's second gate, `qae-artifacts`, is the adjudicator: it reads the run's artifact
directory (`--artifacts`) and refuses on structural facts, never on the model's prose: a step
without its screenshot, a console error outside `--allow-console`, a missing session or network
record, a request to `--site` that answered 400 or worse or failed outside `--allow-request`. The
consumer declares only what is environmental.

The second harness is the review harness in [`harnesses/review/`](../harnesses/review/README.md).
Its wired gate, `review-receipt`, reads three declared files: the newest
`review-receipt: <sha> -- <mode> -- run <id>` comment the workflow itself posted, the pull
request's head SHA, and the count of unresolved review threads. It passes only when a receipt
names this exact head and, when `--threads` is wired, no thread is unresolved. The receipt is
deterministic text from a workflow step the model cannot reach, which is the exact-head receipt
contract: a review that did not complete leaves no receipt and is red, and a push after the review
is a change nobody reviewed.

## Not built yet

SARIF output, preset bundles (`extends`), a pre-commit hook index, and the LLM review as a wired
gate.
