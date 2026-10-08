# QAE harness: an explorer that produces evidence, and a gate that decides

The LLM explores and produces artifacts. Deterministic code decides pass or fail. Its prose is
never the verdict.

Two consumers run it today: the pilot, a Next.js site started on the runner (first real run,
2026-09-18: 33 model turns, one step log and one screenshot per criterion, page snapshots and
console logs saved, a verdict comment whose anchors the gate resolved, `acceptance-verdict PASS`),
and a second site rendered from a WordPress API on the runner (since 2026-09-21; first complete run
28 turns, 3 criteria, both gates green).

## The shape

Three jobs on every pull request, in the consumer's own workflow. [`explore.yml`](explore.yml) is
the template; the consumer owns each job's `runs-on` and how the site is built and started (or which
[reachable preview](#a-reachable-preview-instead-of-a-site-on-the-runner) is used instead), and that
step declares the site's URL as its `url` output. For a site behind a login, it writes
`qae-inputs/site.md` in that same step to tell the explorer how to sign in and what state the site
starts in (a throwaway account on a throwaway backend, never production), and it supplies any
[design references](#design-references-and-roles) the criteria name. Its eight catalog
pins (`criteria@` in criteria, `features@`, `qae-inputs@`, `qae-browser@` and `usage@` in explore,
`criteria@`, `gates@` and `qa-review@` in verify) are inventoried and bumped by `consumers` and
`apply-down` exactly like the stub's.

1. **criteria** reads the PR body, its changed paths and, when the consumer supplies them,
   [its ticket's criteria](#the-tickets-criteria) with `actions/criteria`, on any runner: it
   holds no model login, checks out nothing and builds nothing. Its `count` output decides whether
   the explore job starts at all, so on the Codex lane a pull request with nothing to walk never
   queues for, or holds, the one runner with the login. Its `shards` output is the explore job's
   matrix: one explorer, unless the consumer lets a large pull request be
   [split across several](#splitting-a-large-pull-request-across-explorers).
2. **explore** runs only when the criteria job found a criterion. It checks out the PR with its
   base, picks the [features to re-walk](#re-walking-the-features-a-pull-request-touches) when that is
   on, builds and starts the PR's site on the runner (or resolves its preview), copies the design
   references the site step supplied into the evidence, installs the browser, and runs
   `claude-code-action` with [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) as its browser,
   under a [turn cap](#the-turn-cap) sized to the run. The prompt
   ([`prompt.md`](prompt.md)) tells the model to walk each criterion under the PR body's
   `## Acceptance criteria` heading, append one line per action to `qae-artifacts/qae/ACn.md`
   (`- step k: <what you did> -> <what you saw>`), save a screenshot per step, then write
   `qae-artifacts/verdict.md` with one `acceptance-check: ACn -- PASS|FAIL -- ... (qae/ACn.md::<step line>)`
   per criterion and post it as a PR comment, for people to read. A step after the model fails the
   job if anything outside `qae-artifacts/` and `qae-inputs/` changed on the runner; the config paths
   `claude-code-action` itself resets to the base branch before the model runs (`CLAUDE.md`,
   `.claude/`, `.mcp.json` and a few more) are excused only while they still match the base.
   The next step, the workflow's own, copies the verdict the explorer wrote to
   `qae-artifacts/verdicts/1.md`: that file, in this run's evidence, is the verdict the gate judges.
   Everything under `qae-artifacts/` is uploaded, always, as the artifact
   `qae-artifacts-<run_attempt>`: a failed or cancelled attempt uploads too, and under one name for
   every attempt a re-run's verify job was handed an earlier attempt's evidence. A separate 7-day artifact named
   `vv-usage-qae-explorer-<run_attempt>` contains only the numeric `usage.json`; an explorer that
   did not finish is recorded as unavailable or partial, never as zero. A run whose explore job was
   skipped (no criterion) uploads none.
3. **verify** first fails unless the criteria job succeeded (a job whose need failed is skipped,
   exactly like an explore job with nothing to walk, so this step tells the two apart). Then it
   writes the declared inputs (the PR body; its changed paths; the site URL the explore job
   declared, in `qae-inputs/site-url`), reads the criteria with the same action, and, when there
   was a criterion to explore, downloads the run's evidence and merges it into `qae-artifacts/`: each
   explorer's, from the newest attempt it left any in (this attempt's on a full re-run, an earlier
   one's when only the verify job, or only another explorer, is run again). The verdict,
   `qae-inputs/verdict.md`, is the `verdicts/<n>.md` files of that evidence in explorer order: no
   pull request comment is read. Then it runs the
   [`acceptance-verdict`](../../gates/acceptance_verdict.py) gate through the composite action with
   the manifest [`manifest`](manifest) (`.vibe-verifier-qae` in the consumer), unless the pull request
   needs no check ([below](#which-pull-requests-need-a-check)). Last, whatever happened before it,
   it posts or edits the [QA review comment](#the-qa-review-comment).

## Re-walking the features a pull request touches

Opt-in, for a repository with a [feature map](../../docs/feature-map.md): the explorer also re-walks
a few states of each feature the pull request's changed paths touch, and the verify job requires a
PASS for each, over at least one step that was walked.
Turn it on by adding `--features docs/features --changed-files qae-inputs/changed-files` to the
`acceptance-verdict` line of `.vibe-verifier-qae` (the line is judged from the base, so the next pull
request is the first one re-walked). Without that, the explore job's `Select the features to re-walk`
step selects nothing and nothing changes.

- The explore job's step runs [`actions/features`](../../actions/features/action.yml): the features
  whose own source globs match a changed path, a path more than `--shared-over` (default 2) features
  list counting for none, at most `--max-features` (default 3), each feature selected by its file at
  the base. The checkout has the base for this (`fetch-depth: 0`). It copies each to
  `qae-inputs/features/<id>.md` as the working tree has it, so a pull request that updates a feature
  file is re-walked against its own Reach and Verify, and writes `qae-inputs/features.md`: how many
  states of each feature to walk (`--max-states`, default 3) and the changed paths that selected each.
- After the criteria the explorer walks at most that many states of each feature (a state is a
  numbered step of the file's Verify section), first those closest to its changed paths, and one
  state of every feature before a second of any. It logs them to `qae/features/<id>.md` with a
  screenshot per step and adds two lines to its verdict:
  `regression-check: <id> -- PASS|FAIL -- <sentence> (qae/features/<id>.md::<step>)` and, when it
  left states unwalked, `regression-skip: <id> -- <those states>`.
- A FAIL means a step that was walked showed the feature no longer works as its file describes. A
  state that was not reached, or could not be set up, is never a FAIL: it goes on the skip line. On
  a consumer's live run (2026-10-07), told to walk every numbered step and cited test of three
  features, the explorer left all three unfinished and wrote FAILs that named only the states it
  had not walked.
- `acceptance-verdict` selects the features again itself and refuses a run unless each has exactly one
  `regression-check` line, a PASS whose anchor resolves, and at least one step in its log with its
  screenshot (none is a re-walk that did not happen). It prints the skip lines and judges neither
  them nor how many states were walked. Its finding for a FAIL says how to clear it: fix the
  regression or, when the pull request means the new behaviour, update the feature file in that
  pull request. `qae-artifacts` checks the feature step logs' screenshots like the criteria's; the
  review comment lists each re-walked feature and the states left unwalked.

A consumer whose workflow was copied before the bound keeps the old prompt, which tells the explorer
to walk every state: bump the pins, then copy the template's re-walk paragraph and step 3 (the prompt
is inline in the workflow), or the explorer still FAILs a feature it could not finish.

A wrapper passes its gate list to `actions/features` as `entries:`, as it does to `actions/gates`.

## Design references and roles

A criterion may carry two annotations, in square brackets inside its own text. `bin/vibe-verifier
criteria` prints the ones it reads (the criteria job's log shows them), and the gate refuses a bracket
that starts like one and does not parse, so a typo never drops a requirement.

```
- The home page matches the design at desktop width [ref: home-desktop]
- Only an admin can delete a post, and a read-only user sees no Delete button [as: admin, read-only]
```

**`[ref: <key>]`** compares the built screen with a design reference by looking, the way a reviewer
holds a mockup beside the page.

- The consumer's site step supplies the image as `qae-inputs/references/<key>.png` (a key is letters,
  digits, `-` and `_`), one per screen, state and width, exported at 1x so that its width in pixels
  is the viewport width it shows. Where it comes from is the consumer's choice: committed in the
  repository, fetched from a design tool with a token the step holds, or anything else the step can
  reach. Committed ones are best read from the base branch, so a pull request cannot supply the image
  it is judged against:

  ```yaml
            mkdir -p qae-inputs/references
            base="origin/$GITHUB_BASE_REF"   # the explore checkout has the base (fetch-depth: 0)
            for path in $(git ls-tree --name-only "$base" design/references/ | grep '\.png$' || true); do
              git show "$base:$path" > "qae-inputs/references/${path##*/}"
            done
  ```
- The template's `Prepare the explorer's references and widths` step
  ([`actions/qae-inputs`](../../actions/qae-inputs/action.yml)) runs after the site step and before the
  explorer. It copies each image to `qae-artifacts/references/<key>.png`, so the uploaded evidence holds
  what was compared, lists each with its width in `qae-inputs/references.md` for the explorer, and
  declares each image's sha256 as the explore job's `references` output, which the verify job writes to
  `qae-inputs/references.json` for `acceptance-verdict --references`.
- The explorer opens the image and reaches the same screen and state with the browser resized to that
  width. The comparison is a step of its own: it saves that step's screenshot first, still at the
  reference's width, then compares the two and logs the step, naming `reference <key>` (and names it in
  no other step). A control present in one image and not the other is a difference, never a match: it
  FAILs a structural mismatch (a missing or extra control, a different layout, a different state) and
  never pixel noise. Both halves come from a consumer's live runs (2026-10-07): an explorer logged the
  comparison without saving its screenshot, twice, and another compared a 375-pixel mobile reference
  with a 1280-pixel screenshot.
- The gate refuses the criterion when the workflow supplied no such image: a reference is the
  operator's to supply (needs-operator-reference) and never one the explorer invents, so the criterion
  cannot pass until the site step supplies it. It also refuses an evidence copy whose sha256 is not the
  declared one (the explorer can write under `qae-artifacts/`, so the digest travels as a step output,
  [like the site URL](#rules-the-harness-obeys-each-from-a-real-run)), and a PASS whose step log
  `qae/ACn.md` has no step line naming `reference <key>`, or one whose screenshot `qae/ACn-step-k.png`
  is missing or not exactly as wide as the reference (both widths read from the PNG header). Whether the
  two images match is the explorer's judgment, in that step line and its screenshot; the gate holds
  that the comparison happened, at the reference's width, against the image the workflow supplied.
  `qae-artifacts` names a comparison step whose screenshot is missing as the comparison it is.

**`[as: <role>, <role>]`** walks the criterion once per role.

- `qae-inputs/site.md` says how to sign in as each role: an account per role on a throwaway backend.
  On the Codex lane each role's password goes in the [secrets file](#a-preview-behind-a-login) under a
  name of its own (`QAE_ADMIN_PASSWORD`, `QAE_READONLY_PASSWORD`), and site.md names it.
- The explorer starts each of a role's step lines with `as <role>:`, and as each role checks that
  what the role may do works, that what it may not do is refused, and that controls it must not use
  are not shown.
- The gate refuses a PASS whose step log has no line starting `as <role>:` for some role the criterion
  names. That a refused path was really refused is the explorer's judgment, in those lines and their
  screenshots.

To adopt them, copy the template's `Prepare the explorer's references and widths` step and its
plumbing (the explore job's `references` output, the verify job's `REFERENCES` line), add
`--references qae-inputs/references.json` to the acceptance-verdict line of `.vibe-verifier-qae`
(judged from the base, so it applies from the next pull request), and have the site step supply the
images and the role sign-ins. A criterion naming a reference in a repository whose manifest line
lacks `--references` is refused with that instruction.

## The ticket's criteria

Opt-in. A pull request's own `## Acceptance criteria` list is written by whoever opened it, so a
requirement its ticket states can go missing from it, or the list can be `- None:`. A consumer whose
pull requests implement tickets in a tracker can supply the ticket's criteria, and the harness then
holds the pull request to them as well.

- The criteria job's `Write the ticket's criteria` step is the consumer's. Its default writes an empty
  `qae-inputs/ticket.md`, which links no ticket. Replace it with a step that writes the acceptance
  criteria of the ticket the pull request implements, read with the PR body's grammar: a
  `## Acceptance criteria` list (a ticket body that has that section can be written as it is), or a
  plain list with no headings. For tickets that are GitHub issues linked as `Closes #123` (the job
  then needs `issues: read`):

  ```yaml
        - name: Write the ticket's criteria   # CONSUMER
          env:
            GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
            REPO: ${{ github.repository }}
          run: |
            set -euo pipefail
            issue=$(grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+' qae-inputs/pr-body.md | head -1 | grep -oE '[0-9]+' || true)
            if [ -n "$issue" ]; then gh issue view "$issue" --repo "$REPO" --json body --jq .body > qae-inputs/ticket.md
            else : > qae-inputs/ticket.md; fi
  ```

  Any other tracker works the same way: find the ticket's key in the branch name or the PR body, fetch
  it with a token the step holds (a repository secret; the criteria job runs no model), and write its
  criteria. Fail the step when a linked ticket cannot be fetched, so a broken lookup never reads as "no
  ticket".
- `actions/criteria` counts the ticket's criteria as TC1, TC2, ... in addition to the PR body's, and
  passes the file's text on as the criteria job's `ticket` output. The explore and verify jobs write
  `qae-inputs/ticket.md` from that output, so every job judges the text the criteria job read, before
  any model ran.
- A ticket that lists criteria needs a check whatever the pull request says: a `- None:` declaration or
  a list that leaves one out does not drop it. A ticket that declares `- None: <why>` has none. A
  non-empty ticket in which no criteria can be found (a bold "Acceptance criteria:" line in place of a
  heading, say) is a finding, never "no criteria", so a section the reader missed cannot drop the
  ticket's requirements silently.
- The explorer walks each ticket criterion like the pull request's, with its step log `qae/TCn.md`,
  its screenshots `qae/TCn-step-k.png` and a line `acceptance-check: TCn -- ...`; `[ref:]`, `[as:]`,
  the widths and `expected-refusal:` apply to it the same way. `acceptance-verdict --ticket` requires
  its anchored PASS, `qae-artifacts --ticket` holds its log to the screenshot and width rules, and the
  QA review comment lists it, marked "from the ticket".
- The manifest template's two gate lines carry `--ticket qae-inputs/ticket.md`, a file the template
  always writes (empty without a ticket). A consumer adopting it copies the step, the job output and
  the two lines that write the file. A ticket split across several pull requests is graded whole on
  each, so write only the criteria the pull request owns, or link the sub-ticket.

The ticket's text is untrusted input to the explorer, like the PR body: the same tool rules and write
scope hold.

## Viewports, themes, and what a criterion does not say

**Widths** are opt-in and the consumer's choice: add `--widths 1280,375` (any widths, in pixels) to the
qae-artifacts line of `.vibe-verifier-qae`.

- The explore job's `Prepare the explorer's references and widths` step
  ([`actions/qae-inputs`](../../actions/qae-inputs/action.yml)) reads that line as the base has it and
  writes the widths, one per line, to `qae-inputs/widths`. The explorer checks every criterion's end
  state at each width, with the browser resized to it, as a step with its own screenshot.
- `qae-artifacts` refuses a criterion whose step screenshots (`qae/ACn-step-k.png`) include none of a
  declared width, read from each PNG's header (a browser screenshot is as wide as its viewport), and a
  step screenshot that is not a PNG. Feature re-walk logs are not held to it.
- The widths live in the manifest and nowhere else, so the explorer and the gate cannot disagree. The
  line is judged from the base: a pull request that adds or drops `--widths` changes the next one.

**Themes**: when `qae-inputs/site.md` says the site has more than one theme (light and dark), the
explorer checks each end state in every theme. No gate checks it: which theme a screenshot shows is
not readable from its header.

**Beyond the criteria**, on each criterion's screen, the explorer also uses every action control the
criterion touches through to its end state (a save that is saved, not a button that is only shown),
reloads after a save to check the entered values persisted, and, only when that screen has a form or
another input, tries one invalid input in it, expecting a handled error rather than a crash or a blank
page. Each is a step of that criterion, with its screenshot, and any that fails makes the criterion a
FAIL. They are for criteria only: a [re-walked feature](#re-walking-the-features-a-pull-request-touches)
is checked for what its states say. No gate checks them: what counts as every control a criterion
touches, or as a handled error, is judgment. Two structural checks still apply. A save that answered 400 or worse fails the network check,
and so does the invalid input's request, if it reaches the server, unless the criterion declares that
refusal in its own text (`expected-refusal: 422 /api/profile`,
[below](#the-adjudicator-the-artifacts-decide-not-the-prose)); the same words in a step line declare
nothing. The explorer is told to prefer an invalid input the page refuses before sending anything, and
never to navigate to a URL that neither the criterion names nor the site links to: on a consumer's live
run (2026-10-07) "try one invalid input" on a criterion with no form sent it to an invented URL, whose
404 the network check refused.

## The turn cap

On the Claude lane `claude-code-action` stops the explorer at `--max-turns`, and a run that stops there
fails the explore job even when the site is correct. A fixed 80 did that on a consumer (2026-10-07): two
ticket criteria at two widths took 54 turns, then more than 80 on a re-run of the same head. So the
template's `Prepare the explorer's references and widths` step
([`actions/qae-inputs`](../../actions/qae-inputs/action.yml)) sizes the cap to the run and declares it as
its `max-turns` output, which the explorer step passes as `--max-turns`:

- One walk is one criterion (the pull request's or [its ticket's](#the-tickets-criteria)) at one width
  of `qae-inputs/widths` as one role it names in `[as: ...]`, or up to three states of one feature in
  `qae-inputs/features/`: one walk a feature at the default `--max-states 3`, and one more for each
  three states above that. No run has measured the turns a state takes, so that proportion is a guess.
  Themes are not counted: whether a site has two is prose in `qae-inputs/site.md`, not a fact the step
  can read.
- The cap is 40 plus 30 a walk, never below 80 and never above 240. Measured on that consumer, the runs
  took 18 to 39 turns for two or three criteria at one width, 44 for one criterion at two widths with a
  form, and 54 to 60 for two criteria at two widths: the cap is at least twice each, and twice the 80
  the cut-off run stopped at. The slowest turn there took 4.5 seconds, so 240 turns still end inside the
  explore job's 30 minutes.
- A run that reaches the cap still fails the job: the cap bounds a runaway, it does not pass one.
- One explorer of [several](#splitting-a-large-pull-request-across-explorers) is sized to its own
  share of the criteria, plus the feature re-walk when it is the first.

The Codex lane has no such cap: the pinned Codex CLI's `exec` takes no turn limit, and the job's
`timeout-minutes` bounds it. A consumer's copy made before the cap was sized still passes
`--max-turns 80`, and keeps that cap until it copies the template's explorer line.

## Splitting a large pull request across explorers

Opt-in. One explorer is sized for six walks: the [turn cap](#the-turn-cap) is 40 plus 30 a walk and
stops growing at 240. A consumer's pull request listed 13 broad criteria at two widths, at least 26
walks. Its one explorer made one shallow pass (about four steps a criterion, against about six on
small pull requests), never signed in as two of the three roles it had logins for, stopped after 9
of its 30 minutes and FAILed 9 criteria as not completed. A consumer with more than one QAE runner
can give each runner a share of the criteria instead.

**When it happens.** The criteria job counts the walks: each criterion of the pull request and of
[its ticket](#the-tickets-criteria), once per role it names in `[as: ...]` and per width on the
manifest's qae-artifacts line. Feature re-walks are not counted: which features are re-walked is
selected from a checkout, and this job has none. It plans one explorer for every six
walks, rounded up, never more than `max-shards`, and never more than there are criteria, because a
criterion is never split. The job's log shows the count (`shards: [1,2,3]`), the walks, and one
warning when the walks are more than the planned explorers are sized for, so an oversized pull
request is visible before any explorer starts. With the default `max-shards: 1` there is always one
explorer, and those log lines are all this job adds.

**How to turn it on.** In the criteria job, raise `max-shards` on the `actions/criteria` step to the
number of QAE runners that can run at once. The template's explore job is already a matrix over the
criteria job's `shards` output, so a workflow copied from it needs nothing else. The criteria job
needs the widths to count walks, and it checks out nothing, so the template fetches
`.vibe-verifier-qae` with `gh api` into `qae-inputs/manifest` (the base's copy, or the pull
request's own where the base has none) and passes it as `manifest:`. A wrapper passes its gate list
as `entries:` instead, as it does to `actions/gates`. Without either, one width is counted.

**What each explorer does.** Every explore job runs the same steps.

- [`actions/qae-inputs`](../../actions/qae-inputs/action.yml) is given the explorer's number
  (`matrix.shard || 1`) and how many there are (`strategy.job-total`). It shares the criteria out,
  the same way in every explore job: in document order each criterion goes whole to the explorer
  with the fewest walks so far, the lowest-numbered on a tie. It declares the sentence the prompt
  gives that explorer: "Your share of the criteria is AC1, AC4, TC2: you are explorer 2 of 3. Walk
  only those, and write a verdict line for each of them and for no other criterion; ...". The model
  never works its share out, and the explore job's log names the share. On the Claude lane the turn
  cap is sized to it.
- Only the first explorer re-walks the [features](#re-walking-the-features-a-pull-request-touches),
  and its load starts at the re-walk's walks, counted as the turn cap counts them. A selected
  feature with no walked step fails the gate, so the explorer that carries the re-walk takes fewer
  criteria and reaches it in its time. When the re-walk outweighs the criteria it would get, it gets
  none and is told to re-walk only. Every explore job runs the selection step, so that all of them
  count the same features and make the same assignment, and `actions/qae-inputs` then removes
  `qae-inputs/features/` for every explorer but the first.
- An explorer with nothing to walk fails before any model runs: the criteria changed after the
  criteria job counted them.
- After the explorer, a workflow step copies its verdict to `verdicts/<n>.md` and puts the
  explorer's number in the name of everything else it left, so that the evidence of all of them fits
  in one directory: `session-*/`, `console-*.log` and `network-*.log`, which the gates read by those
  names, become `session-<n>-*/` and so on, and every other file moves under `shard-<n>/`, except
  a criterion's own (`qae/ACn*`, `qae/TCn*`), the feature logs, `references/` and the
  [page record](#an-error-a-page-outside-the-site-logged) `console-pages.jsonl`, which keep their
  places. The session log names the files it saved by their paths, so the step renames those paths
  in it too, and a network record the explorer saved is still found by `qae-artifacts`.
- Each explorer uploads its evidence under its own name: `qae-artifacts-<run_attempt>` for the
  first, `qae-artifacts-<run_attempt>-<n>` for the others. The usage artifact is named the same way.
- The verify job merges them. It takes each planned explorer's evidence from the newest attempt
  that explorer left any in, so running one failed explorer again is enough (measured on a re-run of
  one matrix job, 2026-10-08: the verify job sees every attempt's artifacts). Two explorers that
  left the same path with different content fail the step: nothing is overwritten. The one
  exception is the page record, one line per console error, which the gate reads under one name:
  every explorer's lines are appended to it. An explorer that
  left no verdict has no `verdicts/<n>.md`, its criteria have no verdict line, and
  `acceptance-verdict` refuses them. A copy of each verdict is still posted as a comment, one per
  explorer, for people to read.

**Names.** The first explorer's job is `qae-explore`, as it always was, and the others are
`qae-explore (2)`, `qae-explore (3)`, ... The matrix's first value is blank for that: GitHub names a
matrix job `<name> (<value>)` and leaves a blank value out, and shows a name that is an expression
unevaluated when the job is skipped (both measured, 2026-10-08). So a branch rule or a dashboard
that names `qae-explore` finds it on every pull request, split or not.

**What a consumer must do.**

- **Have the runners.** N explorers at once need N QAE runners, and on the Codex lane N logins
  ([below](#the-explorer-on-codex-the-openai-subscription-with-the-login-on-the-runner)). With fewer,
  the explorers queue: nothing fails, and the split saves less time.
- **Keep criteria independent.** A site step that resolves a shared preview hands every explorer the
  same site and its data, and the explorers run at the same time. A criterion must not depend on
  what another criterion left behind (a record it created, a setting it changed).
- **Make what the site step creates unique per explorer.** The step runs in every explore job. A
  login it creates gets the explorer's number in its name (`matrix.shard || 1` is available to the
  step), or two explorers sign each other out or collide on one account. A site it starts on a
  runner that shares its machine's ports with other runners needs a port per explorer.
- **Require `qae-verify`.** The extra explorers' checks exist only on a large pull request, so no
  branch rule can name them. On a split run the verify job fails unless every explorer's job
  succeeded, after its gates have run.
- **Tell the dashboard.** A [dashboard](../../docs/dashboard.md) that lists the explorer's job names
  lists the extra ones too (`qae-explore (2)`, ...), or it does not see those jobs. Each explorer's
  usage artifact is read by its own name ([`docs/USAGE-CONTRACT.md`](../../docs/USAGE-CONTRACT.md)).

A consumer's copy made before the split keeps working after a pin bump: `actions/criteria` and
`actions/qae-inputs` default to one explorer. It still reads the verdict from a comment until it
takes the template's verify steps.

## Which pull requests need a check

`bin/vibe-verifier criteria`, through `actions/criteria`, decides it in the criteria and verify jobs
with the same code. Each job fetches the PR body and changed paths itself, so an edit to the body between them can make
them disagree, as it already could for the gates. A pull request needs no browser check when:

- its body declares `- None: <why>` under `## Acceptance criteria`, or
- its body lists no criteria (none under an `Acceptance criteria` heading; a body with no such
  heading lists none, whatever bullets it has) and every changed path (both names of a rename) is
  one no site serves:
  anything under `.github/` or `docs/`, a `.vibe-verifier*` manifest, `LICENSE*`, or a file named
  `README.md`, `CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `CODEOWNERS`, `.gitignore`, `.gitattributes` or `.editorconfig` at any depth.

Neither applies when the workflow supplies [ticket criteria](#the-tickets-criteria) (or a non-empty ticket
in which none can be found): the ticket's requirements are checked whatever the pull request declares.
Criteria win over paths: a pull request that lists one is explored whatever it touches. One that lists
none and changes anything else still fails, because it has not said what to check, unless its workflow
supplies criteria for it, as for a [dependency update](#dependency-updates). The list is
adapted from the no-plan allowlist of a private predecessor's QAE, narrowed from every `*.md` to those
names because a markdown file elsewhere can be a page the site renders. A repository whose site renders
`docs/` lists criteria on those pull requests, and the review comment shows every skip, so a wrong one
is visible. When nothing needs checking, the explore job builds nothing and the verify job runs no
gate.

## Dependency updates

A Dependabot pull request could not go green on a Claude-lane consumer, for three reasons, all seen on
one consumer's four security updates (2026-10-06):

- **The explorer refused the run.** `claude-code-action` stops a run a bot started unless
  `allowed_bots` names the bot. The template names `dependabot[bot]` and no other: `*` would let any
  App that can open a pull request start the explorer with a body it wrote.
- **The run had no model token.** A run Dependabot starts reads the Dependabot secret store, never
  the Actions one (its log says `Secret source: Dependabot`), so `CLAUDE_CODE_OAUTH_TOKEN` is empty
  there until the repository adds it to that store. The workflow's `permissions:` apply to that run's
  `GITHUB_TOKEN` as to any other, so the verdict and the review comment are still posted.
- **The body says nothing to check.** It has no `## Acceptance criteria` section, so it lists no
  criteria and the verdict gate refuses it. (Read as a plain list it was 105 of them, one for each
  line of a changelog; a body is read by its heading only since then.)

Folding each update into a pull request a person pushes and describes works, and does not scale. A
dependency update is not skipped either: it is the pull request most likely to break a page nobody
edited. A consumer that takes them does three things, in this order.

**1. Start the site in a container.** GitHub keeps the two secret stores apart so that a dependency
update, third-party code nobody has read, cannot reach a repository's secrets. The template's site
step installs, builds and serves on the runner itself, as the user the explorer step then runs as
with the token in its environment: an install script, or the server still running beside it, can read
it from there. Every pull request that changes a dependency has that exposure; a Dependabot one
changes nothing else. So the install, the build and the server go in a container that is given the
tracked files read-only and nothing else: no secret, no token, an unprivileged user with no
capabilities, and one published port. In place of `npm ci` and the template's site step (node stays,
for the browser toolchain; drop `cache: npm`, which restores a directory the runner no longer uses):

```yaml
      - name: Build and start the site under test   # CONSUMER
        id: site
        env:
          IMAGE: node:24-bookworm-slim@sha256:<the digest you pin>
        run: |
          set -euo pipefail
          mkdir -p qae-artifacts/qae qae-inputs
          src="$RUNNER_TEMP/qae-site"
          mkdir -p "$src"
          git archive HEAD | tar -x -C "$src"
          docker run --detach --name qae-site --user node --cap-drop ALL --security-opt no-new-privileges \
            --publish 3000:3000 --volume "$src:/src:ro" --env NEXT_TELEMETRY_DISABLED=1 \
            "$IMAGE" sh -ec 'cp -R /src /home/node/site && cd /home/node/site && npm ci && npm run build && exec npm start'
          (docker logs --follow qae-site > qae-artifacts/server.log 2>&1 &)
          for i in $(seq 1 120); do
            if curl -fsS http://localhost:3000/ > /dev/null 2>&1; then
              echo "site up after ${i} tries"; echo "url=http://localhost:3000" >> "$GITHUB_OUTPUT"; exit 0
            fi
            if [ "$(docker inspect --format '{{.State.Running}}' qae-site)" != true ]; then
              echo "::error::the site's container stopped before the site answered"; tail -50 qae-artifacts/server.log; exit 1
            fi
            sleep 5
          done
          echo "::error::site did not answer on http://localhost:3000 within 10 minutes"; tail -50 qae-artifacts/server.log; exit 1
```

The copy is `git archive` of the checkout, so the container sees the tracked files of the revision
under test and nothing the runner holds. What the site needs to be whole goes in the same container:
the pilot's contact form sends mail, so its step also starts a small SMTP sink there, which accepts
every message and keeps none, and the send is a criterion instead of a 500.

**2. Add the token to the Dependabot store**, and only after step 1:
`gh secret set CLAUDE_CODE_OAUTH_TOKEN --app dependabot`. It is the one secret that store needs.
What stays exposed: the container shares a kernel with the runner, and the explorer reads the pull
request body, which on a Dependabot pull request quotes the release notes of the package. Those are
untrusted text like any body, under the same [tool rules and write scope](#rules-the-harness-obeys-each-from-a-real-run).

**3. Supply the criteria.** The [ticket's criteria](#the-tickets-criteria) step is where a workflow
states what a pull request is held to when its body cannot. For a pull request that changes only the
package manifest and its lockfile, the pilot writes a file of its own, read from the base branch so
the pull request cannot rewrite what it is judged against:

```yaml
      - name: Write the ticket's criteria   # CONSUMER
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          REPO: ${{ github.repository }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          set -euo pipefail
          : > qae-inputs/ticket.md
          other=$(grep -cvxE 'package(-lock)?\.json' qae-inputs/changed-files || true)
          if [ -s qae-inputs/changed-files ] && [ "$other" = 0 ]; then
            gh api "repos/$REPO/contents/.github/qae-dependency-criteria.md?ref=$BASE_SHA" \
              -H 'Accept: application/vnd.github.raw' > qae-inputs/ticket.md
            if [ ! -s qae-inputs/ticket.md ]; then echo "::error::.github/qae-dependency-criteria.md is empty at $BASE_SHA"; exit 1; fi
          fi
```

The file is a `## Acceptance criteria` list of what must still work when only dependencies moved: the
pages that render, a design reference, the form that sends. They are graded as `TC1`, `TC2`, ... like
any ticket's, the decision is by changed path and not by author, so the same change made by hand is
held to them too, and a pull request that also lists criteria of its own is held to both.

A `.github/dependabot.yml` that groups security updates (`applies-to: security-updates`) makes
several advisories one pull request and one walk; it changes nothing above.

## The QA review comment

The verify job's last step ([`actions/qa-review`](../../actions/qa-review/action.yml), `bin/vibe-verifier
qa-review`) keeps one comment on the pull request, edited in place on every run, whose first line is
`<!-- vibe-verifier:qa-review -->` and whose heading is one of:

| Heading | When |
|---|---|
| `## QA review: not required ✅` | the pull request needs no check; the comment gives the reason |
| `## QA review: could not run ⚠️` | the explore job did not finish, or the verify job stopped before its gates ran, so nothing was judged |
| `## QA review: passed ✅` | the gates passed |
| `## QA review: changes needed ❌` | the gates refused |

Earlier rows win. The state comes from the jobs' and the gates' outcomes, never from the explorer's
prose: under it the comment lists each criterion with the word the explorer wrote for it (PASS, FAIL,
or no verdict), names the head commit it judged, and links the run. It edits only a comment posted by
`github-actions[bot]` that starts with the marker. The explorer's own verdict comment is still posted
on every run the explorer makes, for people to read: the gate's input is the verdict file in the run's
evidence, never a comment. On a pull request from a fork the workflow token is
read-only, so the review step cannot post there and fails the verify job.

## A reachable preview instead of a site on the runner

The site step has two shapes, and both end by writing `url=<the site>` to `$GITHUB_OUTPUT`. A site
whose API answers on another host (a static front end and an API gateway, say) also writes
`origins=<that host's URL>`, space-separated when there is more than one. The gate then judges
requests to those hosts, and the console's `Failed to load resource` lines for them, exactly as it
judges the site's; without it, a 401 or 500 from the API is on a host the gate skips. The
template's builds and starts the site on the runner and declares `http://localhost:3000`. A
repository whose pull requests already get a reachable preview builds nothing on the runner: it drops
`npm ci` and `cache: npm` (node stays, for the browser toolchain), grants the explore job
`deployments: read`, and puts a step like this in place of the build and start. It waits for the
newest `Preview` deployment of the head commit that the host reports to GitHub (Vercel does) and
declares that deployment's URL:

```yaml
      - name: Resolve the preview under test   # CONSUMER
        id: site
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          REPO: ${{ github.repository }}
        run: |
          set -euo pipefail
          mkdir -p qae-artifacts/qae qae-inputs
          for i in $(seq 1 60); do
            id=$(gh api "repos/$REPO/deployments?sha=$HEAD_SHA&environment=Preview&per_page=1" --jq '.[0].id // empty')
            if [ -n "$id" ]; then
              read -r state url <<< "$(gh api "repos/$REPO/deployments/$id/statuses?per_page=1" --jq '.[0] | "\(.state) \(.environment_url)"')"
              case "$state" in
                success) echo "preview ready after ${i} tries: $url"; echo "url=$url" >> "$GITHUB_OUTPUT"; exit 0 ;;
                failure|error) echo "::error::the preview of $HEAD_SHA ended in $state"; exit 1 ;;
              esac
            fi
            sleep 10
          done
          echo "::error::no ready preview of $HEAD_SHA within 10 minutes"; exit 1
```

Deployments and their statuses both list newest first, so `.[0]` is the head commit's latest preview
and its current state (checked against a Vercel preview, 2026-09-23). A branch alias or any other
lookup does as well: the rest of the harness reads only the `url` output. A preview behind an app
login writes `qae-inputs/site.md` in this same step, exactly as above. The URL travels in the clear
(the prompt, the job's outputs, the logs), so it must not carry a credential such as a protection
bypass token; a preview behind Vercel SSO hands its bypass to the browser as a cookie instead
([below](#a-preview-behind-vercel-sso)).

## A preview behind Vercel SSO

A preview under Vercel's Deployment Protection answers a request with no Vercel session with a
redirect to Vercel's login. Vercel's
[Protection Bypass for Automation](https://vercel.com/docs/deployment-protection/methods-to-bypass-deployment-protection/protection-bypass-automation)
is a per-project secret: a request carrying it in the `x-vercel-protection-bypass` header passes,
and adding `x-vercel-set-bypass-cookie: true` makes Vercel answer with a redirect whose `Set-Cookie`
holds the bypass as a cookie, so the browser's later requests pass too. The secret stays with the
workflow. The consumer's site step, which is given it as a repository secret, trades it for that
cookie with curl, writes the cookie as a Playwright storage state (cookies only) outside the
workspace, and declares the file as its `storage-state` output next to `url`. On the Codex lane,
`actions/qae-codex` takes it as `storage-state`: it refuses a file that is not a cookies-only storage
state before the model runs, starts playwright-mcp with `--storage-state`, and passes every cookie
value to playwright-mcp's `secrets` (in a `--config` file), which replaces it with
`<secret>NAME</secret>` in each tool result, saved file and session log. Without that, `browser_run_code_unsafe` (a default tool) returns
the cookie to the model and the session log uploaded with the artifacts keeps it.

After the preview resolves ([above](#a-reachable-preview-instead-of-a-site-on-the-runner)), with the
secret in the step's env as `BYPASS`:

```yaml
          # The headers go in on stdin, so the secret is in no process's argv.
          jar="$RUNNER_TEMP/vercel-bypass.jar"
          printf 'x-vercel-protection-bypass: %s\nx-vercel-set-bypass-cookie: true\n' "$BYPASS" \
            | curl -fsS -o /dev/null -c "$jar" -H @- "$url/"
          python3 - "$jar" "$RUNNER_TEMP/storage-state.json" <<'PY'
          import json, sys
          cookies = []
          for line in open(sys.argv[1]):
              http_only = line.startswith("#HttpOnly_")   # curl's jar marks HttpOnly cookies this way
              if http_only:
                  line = line[len("#HttpOnly_"):]
              elif line.startswith("#") or not line.strip():
                  continue
              domain, _, path, secure, expires, name, value = line.rstrip("\n").split("\t")
              if name == "_vercel_jwt":
                  cookies.append({"name": name, "value": value, "domain": domain, "path": path,
                                  "expires": int(expires), "httpOnly": http_only,
                                  "secure": secure == "TRUE", "sameSite": "Lax"})
          if len(cookies) != 1:
              sys.exit("Vercel set no bypass cookie for this preview")
          json.dump({"cookies": cookies, "origins": []}, open(sys.argv[2], "w"))
          PY
          echo "storage-state=$RUNNER_TEMP/storage-state.json" >> "$GITHUB_OUTPUT"
```

Checked against a protected preview with playwright-mcp 0.0.81 (2026-09-24): with no storage state
the browser lands on Vercel's login; with it, on the app. The cookie Vercel sets is `_vercel_jwt`,
HttpOnly, Secure, `SameSite=Lax`, host-only and seven days long (`Max-Age=604800`), and a copy sent
to another preview of the same project gets the login redirect, so what the browser holds opens one
deployment for a week. `$RUNNER_TEMP` is emptied at the start and end of every job, where a
self-hosted runner's workspace is not. The Codex explorer runs unsandboxed and can read any file the
runner user can, the storage state included: redaction keeps the cookie out of what the browser
tools return, not out of a determined model's reach, which is one more reason the secret itself
never goes near it. The Claude lane takes no storage state yet: its browser is configured inline in
the template, with no released step to check the file and redact its values, so a Claude-lane
repository behind SSO starts its site on the runner, as the pilot does.

## A preview behind a login

When the explorer must sign in, the site step writes the login somewhere the workflow controls and
hands the explorer only a name for each credential:

```yaml
          # in the site step, after reading the login (masked) into $password
          echo "::add-mask::$password"
          jq -n --arg p "$password" '{QAE_PASSWORD: $p}' > "$RUNNER_TEMP/qae-login.json"
          chmod 600 "$RUNNER_TEMP/qae-login.json"
          echo "secrets-file=$RUNNER_TEMP/qae-login.json" >> "$GITHUB_OUTPUT"
          echo "Sign in at $url/login as $email; for the password, type QAE_PASSWORD exactly." > qae-inputs/site.md
```

The explorer step passes `secrets-file: ${{ steps.site.outputs.secrets-file }}` to `actions/qae-codex`.
A value can be any non-empty string: the action hands the values to playwright-mcp as the `secrets` of
a JSON `--config` file, so quotes, `#` and backslashes need no escaping. playwright-mcp then types
the value when the explorer enters the name, and shows `<secret>QAE_PASSWORD</secret>` wherever a
tool result holds the value exactly as written.

**What the explorer is shown is not where a value is kept out.** playwright-mcp looks for the value
as written and for nothing else (the pinned 0.0.81, 2026-10-08). `browser_evaluate` and
`browser_run_code_unsafe` return their script's result as JSON, so a script that reads the password
field back returns a value that holds a `"` or a `\` with those escaped, and the explorer can be
shown it. A value with neither comes back as its name. The action leaves that alone. Refusing those two
characters would turn away two in five 24-character passwords drawn from all of printable ASCII and
still not make the claim true: a page can show a value any way it likes (in a URL, say). And the
explorer runs unsandboxed, so it could read the value from its runner anyway
([threat model](../../docs/threat-model.md)), which is why a login is a throwaway seat on a
throwaway backend.

**The evidence is.** The session log keeps every tool result inside a JSON block, where that
read-back value is escaped a second time, and the explorer could write a value anywhere under
`qae-artifacts/`. So after the explorer the action replaces every value in each non-image file there
(the session log, the step logs, the verdict), in three spellings: as written, inside JSON strings
nested up to four deep (the session log is two), and percent-encoded with every character but
letters, digits and `-._~` encoded (a space as `%20` or `+`). Then it removes its copies. The posted
verdict and the uploaded evidence hold no value in those spellings. Any other is not found: a URL a
browser wrote leaves some punctuation as it is, for one. Never
write a value into `site.md` or the prompt: the explorer does not need it, and the action can only
redact what the file declares. `codex exec --ephemeral` keeps the run's session rollout, which holds
everything the model read, off the runner.

**The name is the value however the explorer enters it.** playwright-mcp resolves a name in two of
its tools only, `browser_fill_form` and `browser_type`. Any other way to enter text sends the name
itself, and the one a model picks is `browser_run_code_unsafe`, whose snippet is handed the page:
`page.locator('input[type=password]').fill('QAE_PASSWORD')` signs in with the twelve letters of the
name. In the session logs of 89 explorations (2026-10-05 to 2026-10-08), 80 password entries went
through the two tools and one through run-code. That one was answered 400 by the site's sign-in
endpoint; the explorer then entered the name through `browser_fill_form` and signed in, all five
criteria passed, and `qae-artifacts` failed the run on the 400 (its console line and its request).
Another run failed on the same console line that day, after the same wrong first sign-in; a re-run
replaced its evidence before its session log was read. So the action loads a second `--init-page`
hook whenever it writes secrets,
[`secret-names.js`](../../actions/qae-browser/secret-names.js), which resolves a name in the
Playwright client every tool and snippet goes through: `fill`, `type` and `pressSequentially` on a
page, a frame, a locator or an element handle, and `keyboard.type` and `keyboard.insertText`. Text
that is exactly a name becomes its value; text that only holds one is typed as written. The names
are the `secrets` of the same `--config` file, so a name means one thing in every tool. The hook
wraps classes of the pinned build and stops a tab from opening when one of those methods is gone,
and the catalog's `qae-browser` CI job signs in through it in the real browser, with the hook and
without it. Three ways stay open, because none is a text-entry call: a script that sets the field
inside the page (`browser_evaluate`), the name pressed one key at a time (`browser_press_key`), and
text dropped onto the page (`browser_drop`).

A text-entry call that fails quotes the text it was typing in its error (`fill("...")` in
Playwright's call log), and playwright-mcp returns a tool's error to the explorer as it is, where
it redacts a result. A fill that missed its field would hand the explorer the password. So the
hook also replaces the value of the secret a failed call was typing with `<secret>NAME</secret>`
in that error, for a name it resolved and for a value one of playwright-mcp's two tools typed.

The gate is not what changed. An allowance for a refused sign-in would have to excuse a 400 at the
site's own sign-in endpoint, and the gate cannot tell the explorer's wrong password from a sign-in
the pull request broke: both are the same request, the same status and the same console line. A
criterion that expects a refusal there still declares it (`expected-refusal: 400 <path>`).

## Rules the harness obeys, each from a real run

- **Anchors resolve or the PASS is refused.** A step line the model did not write cannot be cited.
- **The verdict is the file in the run's own evidence.** After the explorer, the workflow copies
  `qae-artifacts/verdict.md` to `verdicts/<n>.md`, the explore job uploads it with the evidence, and
  the verify job reads it from the download. No pull request comment is read. It used to be the
  newest `acceptance-check:` comment posted by the workflow's identity (`github-actions[bot]`),
  filtered so that no other commenter could paste a PASS. That filter could not tell one run from
  another: after a pull request was closed and reopened, one run's verify job read another run's
  comment. A file in the run's own artifact has neither problem.
- **The write scope is enforced, not requested.** "Write only under qae-artifacts/" in the prompt is
  a suggestion; the scope step is the rule. Found by review on the pilot PR. The action's own
  reset of `CLAUDE.md` and friends to the base branch is not a write, and a PR that changes one of
  them shows it as changed on the runner: the step compares those paths to the base instead of
  failing on sight. Found on the second consumer's first pull request.
- **Nothing to explore costs no model session.** A PR that needs no check
  ([above](#which-pull-requests-need-a-check)), or lists no criteria at all, skips the build and the
  explorer: the first because there is nothing for a browser to check, the second because the verdict
  gate fails it whatever the explorer does. The skip is decided by
  `bin/vibe-verifier criteria`, the same grammar the verdict gate reads, through the
  `actions/criteria` composite action. Before it, three pin bumps and a CI-only PR each paid for a
  full explore of nothing, and one turned the job red on the account's usage cap (2026-09-21).
- **The explorer reads its inputs and artifacts, and nothing else.** The PR body it reads is
  untrusted, and `gh pr comment` is an allowed command, so an unrestricted `Read` would let a body
  ask the model to read the runner's environment and post the tokens. The allowed tools are
  `Read(qae-inputs/**)`, `Read(qae-artifacts/**)` and `Edit(qae-artifacts/**)` (the rule Claude
  Code consults for Write too; a `Write(path)` rule is accepted and ignored). Found by review on
  the second consumer's first pull request.
  The comment rule is the exact command the prompt gives (`gh pr comment <n> --body-file
  qae-artifacts/verdict.md`), not `gh pr comment:*`: `gh` opens `--body-file` itself, so the `Read`
  rules never see that read, and `--body-file /proc/self/environ` would post the environment. Found
  by review on corral's first pull request (2026-09-23). The browser needs no rule for this:
  playwright-mcp blocks `file://` navigation by default.
- **Declared inputs, never operated infrastructure.** The harness needs a URL it can reach, and the
  workflow declares it, never the pull request's files and never the model: the site step's `url`
  output is named in the prompt, and the explore job passes it on as its `site-url` output to the
  verify job, which writes `qae-inputs/site-url` for `qae-artifacts --site-file`. Further origins the
  step declared (`origins`) travel the same way, as `site-origins`, into the same file. It is a step output
  and not a file in the artifact because the explorer writes under `qae-artifacts/`, and it must not
  choose which site the gate judges. The digests of the [design references](#design-references-and-roles)
  travel the same way, as the explore job's `references` output, for the same reason. The pilot
  starts the site on the runner because the repo's Vercel previews sit behind Vercel SSO with no automation bypass configured; a repo with a reachable
  preview resolves its URL instead ([above](#a-reachable-preview-instead-of-a-site-on-the-runner)),
  and on the Codex lane one behind SSO adds the bypass cookie ([above](#a-preview-behind-vercel-sso)).
- **A pull request with nothing to check says so.** The harness runs on every pull request, and the
  verify job runs whatever the jobs before it did (`if: always()`). The explore job is skipped when
  there is no criterion, and GitHub counts a skipped required check as satisfied, so never require
  `qae-explore` without `qae-verify`: the verify job fails when the criteria job did not finish. So a change
  with no rendered surface declares it, one criterion reading `- None: <why>`, or changes only paths
  no site serves ([above](#which-pull-requests-need-a-check)). The harness then builds nothing,
  starts no explorer and runs no gate (both gates pass on a `- None:` declaration anyway), and the
  review comment states the skip. A bare `- None`, or an absent section on a PR that changes the
  site, still fails.
  Found the first time apply-down opened a pin-bump PR and the gate refused it, correctly.
- **Each attempt keeps its own evidence.** The explore job uploads `qae-artifacts/` always, so a
  failed or cancelled attempt uploads too, and a run's attempts share one artifact namespace. Under
  one name for every attempt, a consumer's third attempt passed its explore job and its verify job
  was handed the cancelled second attempt's artifact: no step log, no session log, both gates red on
  a correct run, and no re-run could ever pass (2026-10-07). The artifact is named
  `qae-artifacts-<run_attempt>` (and `-<n>` for an explorer past the first of a
  [split run](#splitting-a-large-pull-request-across-explorers)). The verify job downloads every
  attempt's and takes each explorer's newest, never one named for its own attempt: re-running the
  verify job alone, or one explorer alone, leaves the other evidence in the earlier attempt. It
  fails when the explore job reports an attempt newer than any evidence it finds, or none: that
  attempt left nothing to judge.
- **The browser step asks apt only for what is missing, within a bound.** `playwright install
  --with-deps` refreshes every package list and re-installs Playwright's whole package list on each
  run, for the nine font packages a GitHub-hosted `ubuntu-24.04` runner lacks (every library chromium
  loads is already there; the fonts change what a screenshot shows, so they stay). That step takes
  about 20 s and took 165 s, 295 s, 402 s and past 20 minutes on four runs of two days, with the
  runner's first apt mirror serving about 90 kB/s (2026-10-06 and 2026-10-07): apt's own timeouts
  end a stalled transfer, never a slow one. `actions/qae-browser` installs chromium, asks
  Playwright's own simulation (`install-deps --dry-run`) which packages apt would install, and
  fetches only those: within 90 s, then once more within 300 s without the machine's first mirror
  where it lists more than one (a hosted runner's `/etc/apt/apt-mirrors.txt`, put back afterwards).
  The bound covers downloads only, so it never interrupts dpkg. A fetch that fails twice fails the
  step. Measured on hosted runners with the first mirror held to 73 kB/s: 117 s, against 14 s with
  it healthy.

## The explorer on Codex: the OpenAI subscription, with the login on the runner

[`explore-codex.yml`](explore-codex.yml) is the same harness with the model run through the Codex
CLI instead of `claude-code-action`, for a repository whose explorer should spend a ChatGPT
subscription. Everything outside the explorer block is byte-identical to `explore.yml` (a test holds
that), the prompt is [`prompt.md`](prompt.md) with one lane-specific step, and the verify job is the
same. What differs, and why:

- **No model token is a repository secret.** OpenAI's own action takes API keys only; a ChatGPT-managed
  login is a `~/.codex/auth.json` that Codex refreshes in place, which OpenAI documents for CI as
  "seed it once on a trusted private runner, keep it between jobs, one job stream per copy, never on
  a public repository". So the lane runs on a self-hosted runner (`runs-on: [self-hosted,
  qae-codex]`, consumer-owned) whose runner user holds `$HOME/.codex-qae`, seeded once with
  `CODEX_HOME=$HOME/.codex-qae codex login --device-auth` and `cli_auth_credentials_store = "file"` in
  its `config.toml`. One runner is one store is one job at a time, and only pull requests with a
  criterion reach it: the criteria job decides on any other runner first. `actions/qae-codex` checks the
  store is a ChatGPT login with a refresh token and never writes it. A repository whose pull requests
  go quiet copies [`codex-keepalive.yml`](codex-keepalive.yml) too: Codex refreshes a store that is
  about eight days old during any run, and the weekly exec keeps that happening.
- **The model holds no GitHub token either.** The explorer step is given no secret and the checkout
  has none, so the model cannot post, push, or read a credential. The workflow posts
  `qae-artifacts/verdict.md` itself after the model finishes, which is why step 4 of the prompt
  reads "do not post anything" in this lane. The comment is for people; the verify job reads the
  file. The post is tried up to five times, because one GitHub 5xx would otherwise fail a finished
  exploration's job.
- **Three settings a non-interactive Codex run needs**, each found on the first spike (2026-09-23,
  zack.land on a laptop): `--sandbox danger-full-access`, because under `workspace-write` Codex
  cancels the browser's navigate and run-code calls client-side while screenshots and snapshots still
  answer, so a run looks alive and never loads a page; `approval_policy = "never"`, because nobody can
  answer a prompt; and stdin closed, because `codex exec` otherwise waits on it. The sandbox is not
  the guard here: the write-scope step is, exactly as in the Claude lane, and the runner user is
  dedicated to this work.
- **What the runner needs.** Linux x64 with a dedicated unprivileged runner user, the Playwright
  system packages (`playwright install-deps chromium`, once, as root: `actions/qae-browser` installs
  them itself only as root or through passwordless sudo, and otherwise installs the browser alone;
  both lanes start playwright-mcp with `--browser chromium`, that installed build, so the runner
  needs no Google Chrome),
  node for `actions/setup-node`, and the label `qae-codex`. Register one runner per repository; a personal account has no shared
  runner pool. Keep it off any box that must stay credential-free. Pointed at a
  [reachable preview](#a-reachable-preview-instead-of-a-site-on-the-runner), the runner installs and
  builds nothing of the app: it drives the browser against the preview's URL.
- **More than one QAE job at a time is more than one login.** N parallel explorers, of N pull
  requests or of one [split](#splitting-a-large-pull-request-across-explorers) pull request, need N runners
  (or N container slots), each with its own Codex home seeded by its own
  `codex login --device-auth`. Never point two at one store: a refresh rotates the token, and two
  jobs on one `auth.json` retire each other's session ("refresh token has already been used"). Three
  limits come with it. Every login on one ChatGPT seat spends that seat's usage, so N runners finish
  a queue sooner but use it up faster. Each extra runner helps only if the site step can serve N
  pull requests at once: a preview pipeline that deploys one head at a time keeps the extra runner
  waiting. And the keepalive must reach every store: give each runner its own name as a second
  label and run [`codex-keepalive.yml`](codex-keepalive.yml) once per runner, since a job sent to
  the shared `qae-codex` label lands on only one of them. A runner whose store is not seeded yet
  must stay offline (or its slot unstarted), or every QAE job routed to it fails at the login check.
  Cap each runner's memory hard as well as softly (on systemd, `MemoryMax` equal to `MemoryHigh`):
  with only a soft limit and no swap, one browser tab that grew to 3.7 GB held a runner stalled for
  hours, long past its job's GitHub timeout, and every queued QAE job waited behind it.
- **Usage stays numeric.** `actions/qae-codex` consumes `codex exec --json` without printing or
  retaining the JSONL stream, sums only validated `turn.completed.usage` integers, and uploads the
  same dedicated 7-day artifact as the Claude lane. It still writes `qae-artifacts/final.md` for
  the existing flow and returns the Codex process status after preserving partial usage. The
  weekly keepalive has no browser MCP and does not upload an explorer usage artifact. See
  [`docs/USAGE-CONTRACT.md`](../../docs/USAGE-CONTRACT.md) for the shared schema and privacy rules.

## The adjudicator: the artifacts decide, not the prose

On the pilot's first run the explorer PASSed a criterion that said "loads without console errors"
while the console held a 404 for Vercel's analytics script (absent when the site runs locally),
reasoning that the error was environmental; on the second run it wrote FAIL for the same 404. That
is the model judging meaning, and not the same way twice. So the second gate in the manifest,
[`qae-artifacts`](../../gates/qae_artifacts.py), reads what playwright-mcp and the explorer saved
and refuses on structural facts:

1. every `- step k:` line in a step log has a non-empty `qae/ACn-step-k.png` (a step naming
   `reference <key>` is reported as that comparison);
2. no `[ERROR]` in any `console-*.log` outside `--allow-console` patterns, except Chromium's
   `Failed to load resource` line for a host other than the declared site and its further declared
   origins, which is judged (and skipped) like that host's request in 4, and an error only pages
   outside the site logged ([below](#an-error-a-page-outside-the-site-logged));
3. the session log exists (`--save-session`) and shows a `browser_navigate`;
4. a `browser_network_requests` result exists (the prompt asks for one after each criterion; a call
   that saved its result to a file is read from that file, which must be in the evidence), and
   no request to the site under test (`--site-file`, the URL the explore job declared and any further
   origin after it, or a fixed `--site`) answered 400 or worse or failed, outside `--allow-request` patterns. A request the page cancelled
   itself, `net::ERR_ABORTED`, is not judged when the record also holds that request (same method and
   URL) answered below 400 somewhere in the run, and is a finding when it does not
   ([below](#a-request-the-page-cancelled));
5. with `--widths`, each criterion's step screenshots include one of each declared width
   ([above](#viewports-themes-and-what-a-criterion-does-not-say)).

Run against the pilot's real first-run artifacts with no allowlist it found three things: the
second step of AC2 had no screenshot, the analytics 404, and no network record. The consumer
declares what is environmental on the manifest line, and nothing else:

```
qae-artifacts --artifacts qae-artifacts --site-file qae-inputs/site-url --allow-console '/_vercel/insights/script\.js' --allow-request '/_vercel/insights/script\.js'
```

Those flags are for the environment, and they hold for every pull request. A refusal that is the
correct outcome of one pull request's criterion (an auth gate answering 401 to a signed-out
browser) is declared in that criterion instead, on its own line in `## Acceptance criteria`:

```
- Signed out, opening `/api/quotes` shows no data (expected-refusal: 401 /api/quotes)
```

The gate reads the declaration from the criteria file the verify job fetched, never from the
explorer's artifacts, and it excuses exactly that status at exactly that URL for that run: the
request in the network record and Chromium's `Failed to load resource: ... status of 401` console
line for it. Only a handled refusal can be declared: 401 or 403 (an auth gate), 400, 404, 409 or 422
(a server refusing the invalid input the explorer is told to try). A path resolves against the site under test, and is appended to each further declared origin (so a refusal on an API with a per-preview host can be declared); an `http(s)` URL is taken as
written; the match is exact, query included. A 5xx at the same URL, the same 401 at any other URL,
and every other console error still fail, and a declaration of another status, or a path with no
site, is itself a finding. Because the declaration is a criterion, the explorer must still show the
refusal happens, and a reviewer reads the allowance as part of the spec. An HTML comment does not
declare anything.

### An error a page outside the site logged

A criterion can send the explorer to a page the site does not serve: a hosted checkout or sign-in
page that a link opens in a new tab. That page's console is not the site's. A real run was refused
for one: the hosted page could not reach its own analytics endpoint (`Failed to load resource:
net::ERR_BLOCKED_BY_RESPONSE.NotSameOrigin @ <that endpoint>:0`) while the site logged nothing, and
with one manifest line shared by several repositories the only way out was to reword the criterion.

The console log cannot say which page logged a line. A line ends with the script or resource the
error is about (` @ <url>:<line>`), which reads the same on the site's page and on the hosted one.
Checked in the pinned browser: playwright-mcp keeps one `console-*.log` per tab, a tab a link opened
is not the current tab unless the explorer selects it, and the session log then lists that tab's URL
but never ties a console log to it.

So the harness records the page. [`console-pages.js`](../../actions/qae-browser/console-pages.js)
is loaded into playwright-mcp with `--init-page`, and for each console error it appends one line to
`console-pages.jsonl` in the artifact directory: the URL of the page the tab showed (query and
fragment dropped) and the SHA-256 of the error's line as the console log holds it. No text is
copied, so the record is not a second place a credential can land.

With a site declared, the gate does not judge a console error when all three hold:

- the record names at least one page for that line;
- every page it names is an `http(s)` page outside the declared site and its further origins;
- the line is not about a URL of the site or of those origins.

Everything else is judged as before:

- an error the record does not name, so a run without the record changes nothing, and neither does
  a gate with no site declared;
- an error a page of the site logged. The site's page failing to load a third-party script can be
  the site's own doing (its content security policy, a wrong URL), and the line cannot say which;
- a line both a hosted page and a page of the site logged;
- an error on a page that is not an `http(s)` page (`about:blank`, a `blob:`, the browser's error
  page).

The rule in 2 for a `Failed to load resource: ... status of NNN` line on another host is unchanged:
that line is skipped whichever page logged it.

The Claude lane passes the hook in its `--mcp-config`
(`"--init-page","${{ steps.browser.outputs.console-pages }}"`, the `console-pages` output of
`actions/qae-browser`), and the Codex lane's action passes it itself. A copy of the Claude lane
without that argument writes no record and is judged as before.

A tab a link opened starts logging before playwright-mcp opens its log, so its first lines carry a
negative offset (`[      -4ms] [ERROR] ...`). The gate reads those lines too.

### A request the page cancelled

`[FAILED] net::ERR_ABORTED` is the page cancelling its own request: a view that drops its fetch when
the user moves on, a search box replacing the last query, and also a page giving up on an endpoint
that never answers (a client timeout). Checked in the pinned browser, all of them write the same
line, and the record holds no timing, no initiator and no navigation (a request still pending when a
document unloads is recorded with no failure at all, and a client-side route change makes no
request). So the gate cannot tell moving on from giving up, and asks the record a question it can
answer: was this request answered at all? When the same method and URL (query included, fragment
dropped) answered below 400 anywhere in the run's network records, the endpoint is shown to work and
the cancelled line is not judged. When it never was, the line is a finding: `request cancelled and
never answered in this run`. A cancelled `PUT` is not excused by a `GET` of the same URL, and no
answer excuses any other failure (`net::ERR_CONNECTION_*`, `net::ERR_TIMED_OUT`, `net::ERR_FAILED`,
a failure with no reason, a 5xx).

A request the site always cancels and never completes by design (a query replaced on every
keystroke, whose URL changes each time) is declared with `--allow-request`, like anything else
environmental. A finding for a slow request the explorer simply left behind clears on a re-run in
which that request is answered once.

A manifest copied before the site URL was declared reads `--site http://localhost:3000`, and keeps
working: `--site` judges a fixed URL, `--site-file` the declared one, and a line takes one or the other.
