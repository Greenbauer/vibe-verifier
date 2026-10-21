# Suite scope

A browser suite that tests a deployed preview is slow and queues for a runner, and a pull request
that changes one file under `docs/` waits for it and runs it like any other. Suite scope is the rule
that lets such a pull request go: **a pull request whose diff cannot change what a slow suite
verifies does not run that suite; it gets the suite's statuses posted as not applicable, with the
reason.** [`actions/suite-scope`](../actions/suite-scope/action.yml) decides it before the suite's
job queues, from one list the repository keeps, and every doubt runs the suite.

Two consumers each wrote this rule on their own before it was shared. One measured the cost first:
with one suite at a time, a pull request head waited a median of about 21 minutes for its turn (89
at most) and then ran 23 to 26 minutes, and replayed over the 45 pull requests it had merged, 9
changed nothing the suite could see. The other showed what the rule costs when it leans the wrong
way: it listed the paths that *run* its suite, so everything it had not thought of was skipped. A
pull request that added only a new spec was published not applicable, because the spec's directory
was not on the list, and so was one of 298 files, mostly product code, when the shell pipeline that
tested its file list died early and read as false. The fail-safes below come from those findings.

## The inert list

One file in the repository, wherever the workflow names it, with one pattern per line:

```
# .github/suite-inert: what a pull request can change without changing what the browser suite verifies.
docs/**                            # documentation no build ships
*.md                               # the top-level README
.github/**                         # other workflows and repository settings
!.github/workflows/preview.yml     # the pipeline that deploys the preview and runs the suite
tests/**                           # CI self-tests no spec imports
```

- A pattern covers a whole path from the repository root. `*` matches within one path segment, `**/`
  across directories and `?` one character, as in a feature map's `source:` globs. So `*.md` is the
  Markdown at the root and `**/*.md` is all of it.
- A line that starts with `!` names paths that are **never inert**, whatever the other lines say and
  wherever it is written, and everything under the path it names: `!docs/internal` keeps all of
  `docs/internal/` off the list. It is how a list says "all of `.github/` but the workflow that runs
  the suite".
- A pattern has no leading or trailing `/` (a directory is `dir/**`); no `[`, `]`, `{` or `}`, which
  would match only themselves here, not a set of names; no backslash, which shlex would drop as an
  escape; and no whitespace but a quoted space. A line that breaks one of these rules is malformed, so
  a pattern written with gitignore's slashes, sets or escapes, or pasted with a non-breaking space, is
  never read as matching less than it says. A byte-order mark at the start of the file is ignored,
  and a pattern matches a whole path, never one that only starts with it.
- `#` starts a comment, and each entry's reason belongs beside it: the list is a claim about what the
  deploy and the suite read, and the next reader needs to know why each line is true. A pattern that
  holds a space or a `#` is quoted. A line with two patterns is malformed, never read as one, and so
  is a line with an unquoted `#` right after a pattern's own character (`!docs/#internal/**`), which
  would otherwise end the pattern there.
- A path with a segment named `e2e`, at any depth (`e2e/`, `tests/e2e/`, `apps/web/e2e/`), is
  **never inert**, whatever the list says. Playwright matches `testMatch` at any depth, so a file
  under such a directory can be a spec, and a list that forgot a `!**/e2e/**` line would skip the pull
  request that adds one. The rule is the action's, so no list can leave it out. A changed path named
  `e2e` itself counts too, since GitHub's file list does not say whether it is a submodule or a link
  to a directory. A name that only holds `e2e` (`preview-e2e.md`, `e2e-coverage.test.mjs`) is not a
  segment named `e2e`.
- The catalog ships no list and no default. What is inert depends on what each repository's deploy
  builds and what its suite reads, and only that repository knows.

**Writing one.** List only what nothing on the suite's path reads. That leaves off the application,
the packages it imports, the infrastructure the preview is deployed from, the specs, the scripts the
deploy or the suite's job runs, the package and compiler configuration, and the workflow that runs
the suite with everything it runs from the pull request's own tree. A test runner that matches test
files at any depth (Playwright's `testMatch`) makes a directory *name* the thing to exclude, not a
path: `tests/e2e/new.spec.ts` is a spec. The action excludes `e2e` itself; a suite whose specs also
live under another directory name needs a `!**/<name>/**` line for it. Markdown inside a tree that
is built, deployed or read as a whole stays off the list too. A short list that skips half of what
it could is working; a long one that skips a pull request it should not have is the failure this
page is about.

## Deciding

A small job decides, and the suite's job reads its output in a job-level `if:`, so a pull request
the suite cannot see never enters the suite's queue:

```yaml
jobs:
  scope:
    runs-on: ubuntu-latest   # CONSUMER: any runner with python3 and gh; nothing is checked out
    timeout-minutes: 5
    permissions:
      contents: read
      pull-requests: read
    outputs:
      not-applicable: ${{ steps.scope.outputs.not-applicable }}
      reason: ${{ steps.scope.outputs.reason }}
    steps:
      - id: scope
        uses: Greenbauer/vibe-verifier/actions/suite-scope@0000000000000000000000000000000000000000 # CONSUMER: pin the commit you subscribe to
        with:
          list: .github/suite-inert   # CONSUMER: your list

  browser-suite:
    needs: scope
    # Runs unless the decision is exactly `true`. `!cancelled()` keeps a deciding job that failed
    # from skipping the suite: without a status function GitHub skips a job whose needed job failed.
    if: ${{ !cancelled() && needs.scope.outputs.not-applicable != 'true' }}
```

The step writes two outputs, `not-applicable` (`true` or `false`) and `reason` (one line), and one
annotation saying which rule decided. It never fails on what it cannot read: it says run, with a
warning annotation naming what was unreadable. It reads GitHub three times for a pull request of up
to 100 files (the pull request, the list, one page of files) and once more for each further 100.

The suite is not applicable only when all of these hold. Everything else runs it.

| What is checked | Why |
|---|---|
| **The run is about a pull request.** `pull-request` defaults to the event's, so a push, a schedule or a dispatch names none, reads nothing and runs the suite. | Only a pull request has a diff to judge. A run on the default branch is the gate on what merged. |
| **The pull request's head is the commit this run is about** (`sha`, by default the event's head). | GitHub lists the files of the current head only. A run for an older head would be judged by another commit's diff. |
| **It targets the default branch.** | The default branch's own run verified the tree under an inert diff. Nothing verified the tree under a pull request stacked on another branch, and retargeting it later changes its diff with no new head. |
| **GitHub listed every changed file.** The pull request reports its own count; a list of another length, an entry with no file name, a file listed twice, a read that fails, no file at all, or more than the 3000 files GitHub will list, runs the suite. | A list cut short hides the paths that matter. An empty one proves nothing. |
| **Every changed path is on the inert list**, both names of a rename. | A path the list does not hold is a path nobody claimed is inert. Moving a file out of the application removes it there, and GitHub reports that under its old name. |
| **The list is the default branch's**, read from GitHub by the step itself at the branch's full name (`refs/heads/<branch>`, never a tag of the same name), never from the workspace. No list there, one with no pattern, or one that is malformed, runs the suite. | A pull request cannot widen the list to skip its own suite: the one that adds or widens a list is judged by the default branch's, and once it merges the next pull request is judged by the new one. This is the base the runner judges a manifest from ([architecture](architecture.md), invariant 3), read through GitHub instead of the runner's `git show`: the deciding job has no checkout, and where the base has no manifest the runner uses the head's own, which here would let the pull request that adds a list be skipped by it. |
| **No reason of your own is given** (`also-run`, below). | A repository can have a rule the catalog knows nothing about. |

A workflow whose suite starts on another event for a pull request's head (a deployment status, a
completed workflow run) passes the pull request it resolved, `pull-request: <number>` with
`sha: <the commit>`. The checks are the same; only the default is strict, so that a workflow that
says nothing skips nothing outside a pull request event.

**A reason of your own.** `also-run` takes one line: when it is not blank the suite runs, with that
line as the reason, and nothing is read. It is for a rule only your repository has. One consumer's
suite is the only thing that scores a proof its pull request bodies can claim, so its deciding job
reads the body with its own grammar first and passes "the body claims a proof only the suite
scores" when it finds one. The catalog reads no pull request body here and has no such grammar.

## Posting not applicable

Whatever waits for the suite's commit status (a QA job, a dashboard, a person reading the checks
list) still needs one when the suite is skipped. The same action posts it, in a job of its own that
runs only when the decision was `true`:

```yaml
  browser-suite-not-applicable:
    needs: scope   # CONSUMER: and whatever must have happened first, such as the preview's deploy
    if: needs.scope.outputs.not-applicable == 'true'
    runs-on: ubuntu-latest   # CONSUMER
    timeout-minutes: 5
    permissions:
      statuses: write
    steps:
      - uses: Greenbauer/vibe-verifier/actions/suite-scope@0000000000000000000000000000000000000000 # CONSUMER
        with:
          post: |   # CONSUMER: the status contexts your suite posts, one per line
            Preview / E2E tests
          reason: ${{ needs.scope.outputs.reason }}
```

Each context is written on `sha` as `success`, with the reason as its description (cut at the 140
characters a status takes) and a link to the run: `Not applicable: every changed path (3) is on the
inert list (.github/suite-inert on main)`. A status GitHub refuses three times fails the step, since
a head with no status waits on a suite that never runs. A `post` value with a new line in it (a `|`
block, as above) is one context per line, trimmed, so a name with spaces, as the
[check names](check-names.md) give the suite's status (`Preview / E2E tests`), is one context; a
value on one line is split on spaces, as it always was. While a status is being renamed, post it
under both names, one per line, so whatever waits for either still sees it.

Posting is a separate step, not part of the decision, because *when* is the repository's to say:
one consumer posts only after the preview's deploy succeeded, since a status on the suite's context
is what releases its QA job; another first proves that no earlier run of the suite is still grading
the head. A suite whose result is a check run rather than a status, or that mirrors a result onto
another commit, keeps that part of its own.

## Keeping the list true

The list holds only while no listed path gains a reader: a step that starts reading a file under
`docs/`, a spec that loads a fixture from `tests/`. The repository knows what its pipeline and its
specs read (the paths its workflow's steps name, the files its specs open from disk), so its own
test job writes them to a file and asks the action to check the list the pull request would merge:

```yaml
      - run: node scripts/ci/suite-reads.mjs > suite-reads.txt   # CONSUMER: one path per line
      - uses: Greenbauer/vibe-verifier/actions/suite-scope@0000000000000000000000000000000000000000 # CONSUMER
        with:
          list: .github/suite-inert
          reads: suite-reads.txt
```

With `reads`, the list is read from the workspace, since this judges the list itself. The step
fails naming each path the list calls inert, and how to clear it (take it off the list, or add a
`!` line). It cannot run (exit 2) on a malformed list or an empty file of paths, so a malformed
list does not merge and a script that found nothing does not pass. One action pin serves the
decision and the check, so both read the list with the same code.

From a clone:

```bash
python3 actions/suite-scope/suite_scope.py check --list .github/suite-inert --reads suite-reads.txt
GH_TOKEN=... python3 actions/suite-scope/suite_scope.py decide --repo <owner>/<name> \
  --list .github/suite-inert --pr <n> --sha <its head commit>
```

## What it does not check

- That a listed path is inert. The list is the repository's claim; `reads` checks it against the
  readers the repository's own script finds, and no further.
- The workflow. On a `pull_request` event the workflow file is the pull request's own, so a pull
  request can edit the deciding step or the suite's `if:`. Reading the list on the default branch
  closes the quiet way to skip, and the edit that remains is in the diff. Keep that workflow off the
  inert list; an organization can require it from a trusted ref
  ([Wrappers](GATE-CONTRACT.md#wrappers)).
- What the preview serves. The decision reads the pull request's diff as GitHub lists it.
- A required check. GitHub counts a job its `if:` skipped as satisfied, so a suite whose job is a
  required check needs no status to merge; the statuses are for what reads them.
