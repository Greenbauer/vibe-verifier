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

A gate may mark a finding advisory: it is printed (as a warning annotation under `--format github`,
with `"advisory": true` under `--format json`) and never counts toward exit 1. `repo-rules` uses it
for rules whose severity is below `error`.

A gate may also qualify a pass that checked less than it could (`qualify` in
[`gates/_contract.py`](../gates/_contract.py)): the runner's summary then reads `PASS (<note>)`, not
a bare `PASS`. `feature-map` does it when no `--surface` is declared, so it checked anchors and globs
but not whether every surface is owned.

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
(`gitleaks:allow`, `# zizmor: ignore[...]`, `// ast-grep-ignore`), which are documented,
head-controlled, and visible in the diff, where the review harness reads them.

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
SHA of this repository to pin. That line and each job's `runs-on:` value are the only lines a
consumer edits: the runner (a label or a flow list, such as `[self-hosted, example-lane]`) is the
consumer's choice, so `consumers` does not count it as drift and `apply-down` keeps it through every
pin bump.

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
path (`gates/`, `bin/`, `actions/`, `consumer/`, `tools/`, `rules/`): that is what makes a change
to it a stale pin that `apply-down` delivers. `harnesses/` is not one, so a harness copy takes a change
only by being edited; when the review harness is next changed for real, it becomes
`actions/review/` with its prompt inside, and the copies shrink to a pin.

`bin/vibe-verifier consumers --owner OWNER` (or `--repo OWNER/NAME`, repeatable) is the read-only
inventory and drift check. Through `gh api` it reads each repository's manifest, its stub, and every
other workflow under `.github/workflows/` that pins a catalog action, and reports whether each pin is
current, stale (a commit since it touched `gates/`, `bin/`, `actions/`, `consumer/`, `tools/` or
`rules/`) or unknown to this history; whether the stub matches the canonical one outside the pin
line and the `runs-on:` values; whether a `review.yml` copy matches `harnesses/review/review.yml` outside the
same lines (every other line of that harness is the catalog's, so a copy that differs is running a
review nobody released; the QAE
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
  stub: a gate workflow whose job is named `vibe-verifier-ok` and runs `actions/gates`, and
  optionally a copy of the review harness whose `jobs:` are the catalog's, byte for byte, apart
  from the pin, the `runs-on:` values, the name of its token secret and the one line its verify
  step takes its gate list from.
- An **organization ruleset** with the rule "Require workflows to pass before merging"
  (`workflows`) requires them, each pinned by `sha`, on the default branch of the repositories it
  targets. GitHub runs the pinned file in each targeted repository on each pull request, so the
  check name stays `vibe-verifier-ok` (a reusable workflow would report
  `<caller job> / vibe-verifier-ok`) and a pull request cannot edit or remove its own gate, which
  closes the gap above. GitHub documents the rule for Enterprise Cloud only, but an organization on
  the Team plan accepted and enforced it: probed on 2026-09-23, a pull request read `BLOCKED` while
  the required run was queued and `CLEAN` once it passed, although its own tree did not contain the
  workflow.
- **The wrapper carries every gate list; its repositories carry none.** A repository's list is its
  entry in the wrapper, `repos/<name>/vibe-verifier` (the manifest format above), and the repository
  has no `.vibe-verifier` or `.vibe-verifier-review` of its own. A required workflow cannot read
  those files at run time: its job's `GITHUB_TOKEN` belongs to the repository it runs in, so a
  checkout or `gh api` read of a private wrapper answers 404 (probed 2026-09-23 through a
  ruleset-required run). A composite action of the wrapper downloads with no credential, but the
  workflow would have to pin a commit of the wrapper itself, so a change to a list would take two
  pull requests and the first would not run it. So the wrapper writes the lists into its workflows:
  the gate workflow picks the entry for `${{ github.repository }}` and passes it as a manifest file
  outside the repository, and a repository with no entry fails that step, red, rather than running
  nothing; the review copy passes its gate list as `entries:`. How the wrapper keeps those copies
  equal to its entries is its own business (one way: generate them from the entry files, and test
  that they agree). A list that comes from the workflow is never judged from the base: a pull
  request of the target cannot edit it at all.
- GitHub's constraints on ruleset workflows (its troubleshooting guide for rules): only the default
  activity types trigger them (`opened`, `synchronize`, `reopened`), whatever `types:` says; they
  must not use `cancel-in-progress`, which is why a wrapper's review copy has no concurrency group
  and its header is its own; they do not run for events caused by `GITHUB_TOKEN`; a pull request
  already open when its repository joins the ruleset gets its run on the next push; direct pushes to
  the targeted branch are blocked; a private wrapper can be required only in private repositories,
  and its Actions access setting must admit the organization.
- There are two pins: the wrapper's pins of the catalog, and each ruleset's pin of the wrapper.
  `bin/vibe-verifier consumers --wrapper <a checkout of the wrapper>` judges both, and every `uses:`
  of a wrapper workflow still left in a repository (a legacy reusable workflow); a ruleset pin is
  stale when a commit since it touched the workflow it names. It reads each subscribed repository's
  gate list from the wrapper's entry at `--wrapper-ref` (the wrapper's own row too, since its own
  pull requests run its gate workflow): a repository with no entry needs action, and so does a
  `.vibe-verifier` or `.vibe-verifier-review` left in the repository, which nothing reads.
  `apply-down --wrapper` moves them: the wrapper's catalog pins in a pull request to the wrapper,
  whose own runs are the proof, a caller's `uses:` pins in a pull request of its own, and a
  ruleset's pins with a `PUT` of that ruleset's rules once the new commit is on the wrapper's
  default branch. So a catalog release reaches a wrapper's repositories in two runs: one that opens
  the wrapper's bump, and one after it merges.
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
  The verdict is the first field after the id: a `FAIL` whose evidence quotes the word PASS stays a
  FAIL. The same holds for `regression-check` lines.
- `--artifacts DIR` adds a second root anchors may resolve in: the run's own evidence. Browser
  evidence is never a file in the tree, so an explorer writes one step log per criterion into that
  directory (`qae/AC1.md`, one line per step and what was seen) and anchors to a line of it
  (`qae/AC1.md::saw the "invite expired" message`). A tracked path still wins, and a path that
  escapes the directory never resolves.
- The gate never judges whether the evidence covers the criterion's meaning. It refuses a PASS that
  points at nothing, which is the floor; a PASS that points at the wrong real thing is a review
  concern.
- `--features DIR` with `--changed-files FILE` and `--artifacts DIR` (optional `--max-features N`,
  default 3, `--shared-over N`, default 2, and `--max-states N`, default 3) adds the
  [feature re-walk](feature-map.md#re-walking-the-features-a-pull-request-touches):
  every feature of that map whose source globs match a changed path (a path more than N features list
  counts for none; each feature selected by its file at the base) needs exactly one
  `regression-check: <id> -- PASS -- <evidence>` line, held to the same anchor rule, and at least one
  walked step in the evidence: a `- step k:` line of `qae/features/<id>.md` with its screenshot
  `qae/features/<id>-step-k.png`. The gate selects them itself; `bin/vibe-verifier features` prints the
  same selection for the explore job, and tells the explorer to walk at most `--max-states` states of
  each. A re-walk is partial by design: a `regression-skip: <id> -- <states>` line names what the
  explorer left unwalked, and the gate prints it and never judges it. A FAIL is a finding that says a
  walked step no longer matches the feature file, and how to clear it: fix the regression, or update
  the file in the pull request when it means the new behaviour. Without `--artifacts` the re-walk
  cannot be judged: exit 2.
- A criterion's own text may carry [annotations](../harnesses/qae/README.md#design-references-and-roles),
  held to its step log `qae/ACn.md` under `--artifacts`. `[ref: <key>]` names a design reference: the
  criterion is refused unless `--references FILE` (the JSON object of key to image sha256 the workflow
  declared; an empty file is none) lists the key and the evidence holds that very image at
  `references/<key>.png`, and a PASS also needs a step line naming `reference <key>`, where every such
  line is a comparison whose screenshot `qae/ACn-step-k.png` is exactly as wide as the reference (both
  read from the PNG header). A reference the workflow did not supply is the operator's to supply, never
  invented. `[as: <role>, <role>]` makes a
  PASS need, for each role, a step line starting `as <role>:`. A bracket that starts like either and
  does not parse is a finding. `bin/vibe-verifier criteria` prints each criterion's annotations after
  its count line.
- `--ticket FILE` (opt-in) is the acceptance criteria of the ticket the pull request implements,
  written by the consumer's workflow with the PR body's grammar
  ([the harness](../harnesses/qae/README.md#the-tickets-criteria)). Each is `TC1`, `TC2`, ... and needs
  its own anchored PASS, in addition to the pull request's; a `- None:` in the PR body does not drop
  them. An empty file links no ticket, a ticket that declares `- None: <why>` has none, and a non-empty
  one in which no criteria can be found is a finding. `bin/vibe-verifier criteria --ticket FILE` counts
  them the same way.
- A missing input file is exit 2. A criteria file with no criteria is a finding: a repository that
  subscribes has decided its PRs state them.
- A pull request with nothing to check says so, in the same section: one item reading
  `- None: <why>`. That passes both QAE gates, and the harness's explore job reads the same
  declaration through `actions/criteria` (`bin/vibe-verifier criteria <file>`, which prints
  `none: <reason>` or `criteria: <n>`) to skip the model session. A bare `- None` does not, and neither does silence:
  the declaration is a claim a reviewer can weigh against the diff, and an absent section is
  indistinguishable from forgetting. This is the "declared, not silent" rule the harness contracts
  use everywhere. The QAE harness adds one exemption, in the harness and not in the gate: given the
  pull request's changed paths (`--changed-files`), a body with no criteria whose every path is CI
  configuration or documentation no site serves needs no check, and the harness runs no gate. There
  the diff, read by code, is the claim, and the harness's review comment states the skip and its
  reason on the pull request ([the list](../harnesses/qae/README.md#which-pull-requests-need-a-check)).

In a workflow the two files come from `gh pr view <n> --json body --jq .body` and from the newest
PR comment containing `acceptance-check:`. Who writes that comment is the harness's business (the
plan's explorer); this gate only decides whether what was written is a verdict.

## Wired gates: pinned tools

Some checks are not worth rewriting in the standard library, and a secret scanner is the first:
gitleaks carries the rules, the allowlist grammar (`gitleaks:allow`, `.gitleaks.toml`) and the
maintenance. A gate may depend on such a binary, on these terms, all in
[`gates/_tools.py`](../gates/_tools.py):

- **One pin per tool**: a version and a sha256 per platform, read from the release's own checksum
  file where it publishes one; otherwise from the release assets themselves, with how each digest was
  checked written next to it. The pin lives in the catalog, so a consumer takes a new tool version
  the way it takes a new gate, by bumping its action pin.
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
because the login lives on the runner (see the harness README). The fourth is
`tools/changed-code-mutation/` (StrykerJS and its Vitest runner plugin, with a stub in Vitest's
place; see [Changed-code mutation](#changed-code-mutation)). A local package a lockfile links
(`file:`), such as that stub, is copied into the cache with the lockfile and takes a new cache
directory only when its version in the lockfile changes.

### Python toolchains

A tool that lives on PyPI is pinned by `tools/<name>/requirements.txt`: exact versions, and the
sha256 of every wheel the index lists for them. It installs with `pip install --require-hashes
--only-binary :all: --no-deps` into a venv in the cache, one directory per requirements digest and
interpreter version (a compiled wheel is built for one CPython), so pip takes only a listed wheel and
never builds from source. No `python3` with `venv` and `pip`, or an install that fails, is exit 2.
The first is `tools/cognitive-complexity-python/` (complexipy, which measures Python files for
`cognitive-complexity`; its `measure.py` runs under the venv's interpreter). The second is
`tools/changed-code-mutation-python/` (mutmut and everything it needs but pytest, which must be the
project's own); its venv is made from the project's interpreter (`--python`), so every compiled wheel
matches it, and the gate appends the venv's packages to that interpreter's path rather than running
the venv.

### Ratchets

`cognitive-complexity` and `max-file-lines` judge the source files a pull request changed, against
the same files at the base: a file is a finding only when it has more functions over the
complexity limit than it had (or is new and has any), or is over the line cap and grew. What
nobody touched never blocks, and a refactor that removes one over-limit function is never undone
by the count. A file the pull request moved is compared with itself at its old path (git's
rename detection), so moving a long or tangled file without changing it never blocks either. That is the lesson of a predecessor's observe stage, which reported main's own nine
findings on every pull request and could not be promoted. `--all` measures everything, for an
audit or to decide a first subscription. `cognitive-complexity` runs eslint on its own config and
an empty suppressions file, so a repository's `eslint.config.*` and `eslint-suppressions.json`
(eslint's bulk suppressions) are for its own lint and never reach the gate: a suppressed count is
not the base the ratchet compares with, and entries for rules the gate does not run would
otherwise fail every run as unused. Python files go to complexipy, whose `# noqa: complexipy` and
`# complexipy: ignore` comments are neutralized before measuring for the same reason. Both gates
measure JS, TS and Python files by default; `--source` narrows them.

### Changed-code mutation

`new-source-has-test` proves a test file exists, and a test that copies the code it covers,
asserts nothing, or matches the source as text satisfies it while that code is broken.
`changed-code-mutation` measures whether the tests notice: StrykerJS, pinned in
`tools/changed-code-mutation/`, changes the code one small edit at a time (a `>` to `>=`, a
condition to `true`, a block emptied), each edit a *mutant*, and runs the project's own tests
against it. A mutant every test still passes on survived.

```
changed-code-mutation                                       # a Vitest project at the repository root
changed-code-mutation --project apps/web --break 70 --soak
```

- **Only the changed lines.** The gate takes the non-test JS and TS source files changed since the
  base (tests, `*.d.ts`, `*.config.*`, `*.stories.*` and `node_modules` always excluded;
  `--source` and `--exclude` as in the other source-file gates) and, in each, the line ranges the
  diff added or modified at `HEAD` (`git diff -U0`; a moved file is compared with itself at its old
  path, and a deletion leaves nothing to mutate). Those ranges are Stryker's `file:start-end`
  mutate ranges, so a mutant is made only where the edited code lies wholly inside a range. Whole
  files are never mutated. Stryker refuses a range on a path its glob syntax reads as a pattern, so
  the gate escapes each `[`, `*`, `?` and `(` as a one-character class (`app/[[]id]/page.tsx`), which
  matches only that path: Next.js dynamic segments and route groups are judged like any other file.
- **The project's own Vitest.** It is resolved from `--project` the way Node resolves it, so a
  copy hoisted to the repository root counts. The gate installs nothing for the project: the job
  runs `npm ci` (or the project's equivalent) first. No Vitest, or a Vitest outside 2.x to 4.x, is
  exit 2: under Vitest 5 the pinned Stryker 10.0.0 kills no mutant at all (measured 2026-10-05:
  a test that kills 8 of 8 under Vitest 2.1.9, 3.2.7 and 4.1.10 killed none under 5.0.3), so any
  verdict there would be false. Stryker's Vitest plugin falls back to a Vitest installed beside
  it when the project's cannot be loaded; the toolchain installs a stub there that throws, so the
  Vitest that runs is always the project's. Vitest's `related` filter is off, so the first run
  runs the whole suite: with it on, a test that never imports the changed file is not run at all,
  and when no test imports it Stryker writes no report.
- **Verdict.** The score is killed / (killed + survived + not covered) over the changed lines.
  Below `--break` (a whole number, default 60) the gate fails with one finding for the score and one per
  surviving mutant, at its line, naming the code it replaced, the replacement and Stryker's
  mutator. Mutants that timed out, did not compile, crashed the test runner, or carry a
  `// Stryker disable` comment are left out of the score and printed. No mutant on the changed
  lines is a pass.
- **Why 60.** It is Stryker's own default `thresholds.low`, the score below which its report marks
  a project as poor (its `break` is off by default). A changed range often holds only a handful of
  mutants, so one equivalent mutant (an edit no test could tell apart from the original) moves the
  score by 10 to 30 points, and 60 leaves room for it, while a test that copies the logic or
  asserts nothing scores near 0 (the fixture's kills 0 of 8). Raise it on the base manifest once a
  soak shows a repository's scores. A mutant no test can kill is marked in the source with
  `// Stryker disable next-line <Mutator>: <reason>`: head-controlled and visible in the diff,
  like the other tools' escape hatches.
- **Bounded, never partial.** More than `--max-files` changed source files (default 20), or a run
  longer than `--timeout` seconds (default 900), is exit 2 naming the bound, never a verdict on
  part of the change. On the timeout, and on a SIGTERM or SIGINT the gate receives (a Ctrl-C in a
  terminal), Stryker's whole process group is killed and its sandbox removed. The cost is one run of
  the whole suite plus, per mutant, the tests that cover it, or the whole suite again when no test
  loads the changed file.
- **Subscribing.** The canonical stub installs no dependencies and stops its job at 5 minutes, and it
  runs every gate in `.vibe-verifier`, so this gate is not listed there: there it would be exit 2 on
  every pull request that changes source. It goes in a manifest file of its own, such as
  `.vibe-verifier-tests`, run by a second job that runs `npm ci` (or the project's equivalent) and
  then `actions/gates` with `manifest: .vibe-verifier-tests`, under a job timeout above `--timeout`.
  The runner judges that file from the base like any manifest in the repository. Not `entries:`: in
  a repository's own workflow a pull request can edit it.
- **Exit 2 also when** the suite fails at `HEAD` (Stryker cannot measure a red suite), Stryker exits
  non-zero, or Stryker matched fewer files than it was given, such as a path with brace syntax
  (`{a,b}`), which no escape makes literal. That last is read from Stryker's own
  "Found N of M file(s) to be mutated" line, because a file whose changed lines hold no mutant is
  absent from its report.
- **Python files.** A changed `.py` file (Python tests, `test_*.py`, `*_test.py` and `conftest.py`,
  always excluded) goes to mutmut 3.8, pinned in `tools/changed-code-mutation-python/` (see
  [Python toolchains](#python-toolchains)), and its mutants join the same score. mutmut runs the
  project's own pytest in the project's own interpreter, `--python` (default `python3`; a relative
  path is the repository's, such as `--python .venv/bin/python`): its package is appended to the end
  of that interpreter's `sys.path`, so the project's own packages win. No pytest there, or Python
  before 3.10, is exit 2. It runs on a copy of the project's tracked files under the gate's own
  `[tool.mutmut]` table (any the project has is replaced): mutmut generates a mutant per change it
  can make in every function of the changed files and runs the whole suite once to learn which tests
  reach which function; the gate places each mutant on the source line where it first differs from
  the function it copies and keeps only those on the changed lines; mutmut then runs just those,
  each against the tests that reach its function. When mutmut writes no coverage (it exits 1 both
  when the suite fails and when no test reaches a changed function), the gate runs the project's
  pytest once itself: a failing suite is exit 2, a passing one makes every mutant on the changed lines
  not covered. mutmut makes no mutant in a decorated function, a method of a nested class, or a line
  marked `# pragma: no mutate` (Python's escape hatch, with the reason in the comment); changed lines
  there are printed as left out and never scored. Like Stryker's `perTest` coverage, a test reaches
  a function only in its own process: code a test runs as a separate process (a CLI it calls) is not
  covered.
- **What it does not catch.** CSS and markup; behavior only an end-to-end or browser test reaches
  (the [QAE harness](../harnesses/qae/README.md) covers that); code no unit test can load; and
  equivalent mutants, which survive whatever the tests do. A flaky test can kill a mutant by
  failing for its own reasons. The gate runs the pull request's own code and tests, as the
  project's CI already does, so it belongs in a job that holds no secrets, and a test written to
  detect the mutation run could kill every mutant: that is in the diff, where the review harness
  reads it, not something this gate can see.

### Repository rules

Agents copy whatever pattern the nearest code shows, so a repository's established patterns belong
in rules a gate runs, each failing with a message that says what to do instead; prose (a
`CLAUDE.md` section, a review prompt) is for judgment calls. `repo-rules` runs
[ast-grep](https://ast-grep.github.io/) rules, written in YAML for TypeScript, TSX, JavaScript,
Python or any other language ast-grep parses, with ast-grep pinned in `gates/_tools.py`:

```
repo-rules                                    # the repository's .vibe-verifier-rules/
repo-rules --pack example                     # plus a catalog pack, rules/example/
repo-rules --rules lint/rules --pack example --soak
```

- `--rules DIR` (repeatable) is a rule directory of the repository; with none given,
  `.vibe-verifier-rules/` when the base or the head has it. `--pack NAME` (repeatable) is a pack of
  this catalog, read from the pinned revision; [`rules/`](../rules/README.md) says what belongs in
  one. A run with no rules at all, an unknown pack, or a `--rules` directory with no rule files is
  exit 2.
- A rule directory holds rule files (`*.yml`, `*.yaml`, at any depth) and, under `tests/`, their
  ast-grep rule tests, each written as a block mapping (one `key: value` per line; the gate reads them
  with ast-grep's YAML grammar, and a document whose `id` it cannot read is exit 2):

  ```yaml
  # .vibe-verifier-rules/no-console-log.yml
  id: no-console-log
  language: TypeScript
  message: Use `log.info` from src/log instead of console.log.
  note: The logger tags each line with the request id.
  rule:
    pattern: console.log($$$ARGS)
  ```

  ```yaml
  # .vibe-verifier-rules/tests/no-console-log-test.yml
  id: no-console-log
  valid:
    - log.info("ready")
  invalid:
    - console.log("ready")
  ```

- **Every rule proves itself.** A rule with no `message`, with no rule test holding at least one
  `valid` and one `invalid` case, or whose tests fail is exit 2 naming the rule, never a pass, and
  `--soak` does not mask it. Snapshot tests are skipped: the test proves the rule fires and stays
  quiet, not where its label lands.
- **Ratchet, by count per rule and file**, the way ESLint's bulk suppressions work. A file
  violates a rule only when it has more of that rule's findings at HEAD than at the merge base under
  the same rules, so editing, restyling, re-indenting or moving a violation within its file never
  blocks; a file the pull request moved is compared with itself at its old path, and only files the
  pull request changed are scanned. The trade-off is accepted: fixing one violation and adding
  another of the same rule in the same file nets zero and passes. When a count rises, the gate
  reports that rule's findings on lines the pull request added or changed (from the diff), with the
  count before and after; when none of them is on such a line (the change was elsewhere, such as
  removing the `try` a rule looks for), it reports all of that rule's findings in the file and says
  so. `--all` reports every finding in every tracked file, for an audit or a first subscription.
- **Severity.** A finding blocks unless its rule says `severity: warning`, `info` or `hint`; those
  are printed as advisory (a warning annotation on GitHub) and never fail the gate, under `--all`
  too. An unset severity blocks, although ast-grep itself reads it as `hint` and plain `sg scan`
  does not fail on it: a rule is advisory only when it says so. Severity is part of the rule file,
  so it is base-controlled: a pull request that downgrades a rule is still judged at the base's
  severity.
- **Base-controlled.** A rule file the base has is judged as the base has it, at its base path: a
  pull request that edits, weakens, deletes, moves or renames a rule is still judged by it, the run
  says so, and the change applies from the next pull request. A rule file the pull request adds
  applies at once, to what the pull request adds (it runs over the merge base too, so code already
  there does not count). A pack comes from the pinned catalog. An absolute `--rules` path is a
  directory outside the repository, such as one an organization's [wrapper](#wrappers) writes its
  own rules to; it is read as it is, since a pull request of the target cannot edit it.
- **A rule sees only its language's extensions.** ast-grep picks a file's language by extension, so
  a `language: Tsx` rule covers `.tsx` files only, never `.ts`, and finds nothing there without a
  word. A rule meant for both is written once per language, or its directory maps more extensions to
  its language with an `sgconfig.yml` at the directory's root, of which the gate reads one key,
  `languageGlobs`, in ast-grep's own format:

  ```yaml
  # .vibe-verifier-rules/sgconfig.yml
  languageGlobs:
    tsx: ['*.ts']
  ```

  Each rule directory and pack runs as an ast-grep project of its own, its rule tests included, so a
  mapping applies to its own directory's rules and to no one else's: once `*.ts` is tsx there, that
  directory's `language: TypeScript` rules no longer see `.ts` files, while a TypeScript rule in
  another directory or pack still does. Two directories can therefore map one extension differently
  without either being picked over the other. The mapping is base-controlled: the base's
  `sgconfig.yml` applies even when the branch edits or removes it, and one the branch adds applies
  once it merges, because a mapping can narrow what rules see. An `sgconfig.yml` the gate cannot
  read, or a language ast-grep does not know, is exit 2. Since the projects are separate, a rule id
  may appear in only one directory of a run (exit 2 naming both), and a rule's tests count only from
  its own directory's `tests/`.
- Each finding prints `path:line`, the rule id, the message and, when the rule has one, its note.
- Of a rule directory's `sgconfig.yml` only `languageGlobs` is read; the gate writes the rest of each
  project itself. Shared utility rules go in each rule's `utils:`, and `utilDirs` and custom
  languages are not available. A line can opt out with ast-grep's `// ast-grep-ignore: <rule-id>` comment,
  head-controlled and visible in the diff like the other tools' escape hatches. ast-grep's report of
  an unused suppression is off: a repository that also runs ast-grep for its own rules suppresses
  rules this gate never loads.
- ast-grep's releases publish no checksum file and no attestation, so its pin is the sha256 GitHub
  recorded for each release asset, matched against a download of each before it was written down.
- **The rules block is generated, never hand-synced.** `bin/vibe-verifier rules-doc --repo DIR
  --manifest FILE [--write PATH]` reads the manifest's `repo-rules` lines (their `--pack` and
  `--rules` arguments; the rest change no rule), reads those packs and rule directories as the
  working tree has them, and renders one Markdown block: every rule ast-grep runs (one whose severity
  is `off` never runs and is left out), sorted by id, each with whether it blocks or is advisory
  (severity, as above), its language with the globs its directory's `sgconfig.yml` maps to that
  language (`with`) and to another one (`without`: a file such a glob matches is parsed as that
  other language, so this rule never sees it), its
  `files` and `ignores` globs, and its message. The block opens and closes with fixed comment lines that say it is generated and to edit the rule files
  instead. Without `--write` it is printed; with it, the block in PATH is replaced, or appended when
  PATH has none. The rule files are read with the gate's own reader, so `rules-doc` refuses (exit 2)
  what the gate refuses: a rule without a message or without a `valid` and an `invalid` case, a
  document whose `id` cannot be read. It does not run the rule tests; the gate does. The
  [agent skills](../skills/README.md) read this block before editing and regenerate it after
  changing a rule.

  ```
  repo-rules --pack example --doc AGENTS.md
  ```

  `--doc PATH` (repeatable, a path in the repository) makes the gate render the same block, for the
  rule files at HEAD and the `repo-rules` lines of the manifest the run read at HEAD, and report a
  finding, naming the command that regenerates it, when PATH lacks the block or holds a different
  one. Blank lines do not count: the block is written with a blank line after its first marker and
  before its last so Markdown formatters such as Prettier leave it alone, and one a formatter
  reflowed anyway, or one whose characters it backslash-escaped (Prettier writes `_x` as `\_x`), is
  still current; any changed text is not. The runner tells every gate which manifest that is, in `$VIBE_VERIFIER_MANIFEST`; the gate's
  own arguments are the base's line on a pull request, and are what it renders from only when it runs
  alone or the head manifest has no `repo-rules` line. HEAD and not the base: the block describes what
  the branch will merge, so the pull request that changes a rule, or the packs and directories the
  manifest enables, regenerates the block with it, while the code is still judged by the rules and
  the line the base has. A missing or stale block is a finding
  like any other: it blocks, and under `--soak` it is reported only, because the gate ran and the
  signal is real; a rule file at HEAD that cannot be read is exit 2, which `--soak` never masks. Two
  consequences of the rest of this contract: the pull request that adds `--doc` to the manifest is
  judged by the base's line, which lacks it, so it adds the block in the same change and the check
  bites from the next pull request; and a catalog pin bump that changes a subscribed pack's rules
  leaves the block stale, so its pull request needs the block regenerated, which `apply-down` (pin
  lines only) does not do.

### Universal checks

A rule of the form "every X goes through Y" was once graded by a lint the pull request itself wrote,
as a list of the forms its ticket happened to name; calls in other forms survived. `universal-checks`
reads the rule's definition from outside the pull request: the repository's `ci/universal-checks.md`
(`--rules PATH` names another) as the merge base of the base ref and HEAD has it.

```
# ci/universal-checks.md
Universal checks:
- `\.(?:route|waitForResponse|waitForRequest)\s*\(` in `e2e/*.spec.ts` conforms to `apiPath\(`
- `(?<!function )\bapiPath\(` in `e2e/helpers.ts` conforms to `^`
```

- **One rule per bullet** under a `Universal checks:` heading (up to three `#`, bold, any case,
  optional colon); a wrapped bullet joins the one above it. Every match of the population regex in a
  tracked file the glob selects is a member. A member ending in a call, or followed by one, is graded
  on the call argument its match ends in: for a call-shape population the first argument (the matcher,
  not the handler body), and a population that consumes leading arguments
  (`\.on\(\s*"response"\s*,`) grades the next one. Any other member is graded on the rest of its
  line. It conforms when that text matches the conforms regex, whose alternatives are the exemptions.
- **Write the population as every place the rule governs**, by the shape of the code that does the
  governed thing (for a rule about an API, every method of it that can do that thing), never by the
  wrong forms someone happened to mention. A rule that a helper stays in use lists its calls in the
  files that must call it and conforms to `^`: if the calls go, the population is empty and the rule
  fails.
- Regexes are Python `re` with MULTILINE. The glob is `fnmatch` over the repository-relative path, so
  `*` also crosses `/`, as in the rule files this grammar was ported from. Comments in the `//` and
  `/* */` forms are blanked first, so a match inside one is not a member, and string, template and
  regex literals are skipped when finding an argument; a comment in another language's form still
  counts.
- **Judged from the merge base.** A pull request that edits, weakens or deletes a rule is still judged
  by the rule as the merge base has it. When the head's copy of the file differs (or the base has
  none), it is graded too, so a rule a pull request adds is proven before it merges, and a malformed
  copy cannot merge and then break every later pull request. A rule that lands on the default branch
  after a pull request branched reaches that pull request when it updates from the base.
- **Fails closed.** Exit 1 for each member that does not conform, at its line. Exit 2, which `--soak`
  never masks, when no copy of the file exists at the merge base or the head; when a copy parses to no
  rule, holds a malformed rule or a heading with none under it, or has a backticked bullet outside a
  section (a note between bullets ends the section above it); when a regex does not compile; when a
  glob matches no tracked file; and when a population matches nothing. An empty population is a typo
  or a rule whose code is gone, never a pass. To retire a rule, remove its line first and merge, then
  delete the code it governed; to rename what it governs, widen the rule to accept both names, then
  rename, then narrow it.
- It sees text, not meaning: whether the author named every code shape of the API is the author's
  reading, and a rule pairing two things (an import with its use) needs a test of its own.

## Feature map

`feature-map` keeps a [feature map](feature-map.md) (one Markdown file per user-facing feature under
`docs/features/`) true against the code at the head. A pure gate: it reads the tracked files and
nothing else.

```
feature-map --surface route src/routes.ts '^\s*path: "([^"]+)"'
feature-map --dir docs/features --surface page 'content/**/*.md'
```

- `--surface KIND GLOB REGEX` (repeatable): the code's surfaces of one kind are every match of the
  one capture group of REGEX (`^` and `$` per line) in every tracked file GLOB matches; with no
  REGEX, each matched path is one. A feature owns a surface by listing `` - KIND: `value` `` under
  its Surfaces.
- A finding is a surface no feature lists, a listing the code no longer declares (or of a kind no
  `--surface` declares), a `- source:` glob matching no tracked file (or a feature with none), a
  Verify anchor that does not resolve (the `acceptance-verdict` anchor forms, resolved the same way),
  and a malformed bullet or missing section. The whole map is judged on every run, so a repository
  subscribes clean.
- With no `--surface`, anchors and globs are checked and the pass is qualified
  `completeness not checked`, with an advisory finding saying why.
- Exit 2 when the map has no feature file, or a `--surface` is malformed, matches no tracked file or
  finds no surface.

## Harnesses

A harness is a parameterised workflow that produces the declared inputs a wired gate grades. The
first is the QAE harness in [`harnesses/qae/`](../harnesses/qae/README.md): an explore job whose
model drives a real browser and writes step logs, screenshots and a verdict comment; a verify job
that feeds those to `acceptance-verdict` and keeps one review comment on the pull request saying
whether QA passed, needs changes, was not required, or could not run. The consumer owns `runs-on`, the
design references and role sign-ins its site step supplies, how its site is started (or
which reachable preview it uses, and on the Codex lane the cookies the browser starts with when that
preview is behind Vercel SSO) and the action pins; the harness owns the prompt, the grammar and
the rules.

The harness's second gate, `qae-artifacts`, is the adjudicator: it reads the run's artifact
directory (`--artifacts`) and refuses on structural facts, never on the model's prose: a step
(of a criterion, or of a re-walked feature under `qae/features/`) without its screenshot, a console error outside `--allow-console`, a missing session or network
record, a request to the site under test that answered 400 or worse or failed (one the browser
cancelled itself, `net::ERR_ABORTED`, is not judged) outside
`--allow-request`. With a site declared, Chromium's `Failed to load resource` console line for
another host is skipped like that host's request; every other console error is still judged. The site is `--site <URL>`, or `--site-file <FILE>`: the URL the explore job
declared, written by the verify job, where a missing or malformed file is exit 2. A network call that
saved its result to a file is read from that file, and one the evidence does not hold is a finding. The file may list
further URLs after the site's, other origins the site is served from (its API on another host), and
requests and resource errors on those are judged as the site's are. The consumer
declares only what is environmental. A pull request declares an expected refusal in a criterion's
own line, `expected-refusal: <status> <path-or-URL>`, read from `--criteria`, where the status is a
handled refusal (400, 401, 403, 404, 409 or 422): it excuses exactly that status at exactly that URL
for that run, and nothing else. With `--widths N[,N...]` (opt-in) each criterion's step screenshots
must include one of each width, read from the PNG header; the explore job tells the explorer the same
widths, from the same manifest line as the base has it, in `qae-inputs/widths`. With `--ticket FILE`
the ticket's criteria (`qae/TCn.md` logs) are held to the same rules, their `expected-refusal:`
declarations count, and a pull request that declares `- None:` is still judged when its ticket lists
criteria.

The second harness is the review harness in [`harnesses/review/`](../harnesses/review/README.md).
Its wired gate, `review-receipt`, reads three declared files: the newest
`review-receipt: <sha> -- <mode> -- run <id>` comment the workflow itself posted for the pull
request's head (with none for that head, its newest receipt, so the finding names the head that
one was for), the pull request's head SHA, and the count of unresolved review threads. The newest
receipt overall is not the input because a review of an older head can finish, and post, after
the current head's. It passes only when a receipt
names this exact head and, when `--threads` is wired, no thread is unresolved. The receipt is
deterministic text from a workflow step the model cannot reach, which is the exact-head receipt
contract: a review that did not complete leaves no receipt and is red, and a push after the review
is a change nobody reviewed. The one exception is a run whose execution log structurally proves
the reviewer subscription hit a rate or usage limit: the job passes with a warning and posts a
receipt whose mode is `limited`. The gate accepts it for its exact head like any other mode, still
requires zero unresolved threads when `--threads` is wired, and the scope step never uses it as
the delta anchor, so the next real review covers what it let through. An invalid or missing
token, a crash, a timeout or the turn cap stays red.

## Not built yet

SARIF output, preset bundles (`extends`), a pre-commit hook index, and the LLM review as a wired
gate.
