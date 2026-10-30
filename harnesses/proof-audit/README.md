# Proof audit: a model grades whether the tests prove each requirement, and a gate checks its quotes

`ticket-coverage` checks that every requirement ID a pull request's ticket defines has a `[proof:]`
claim whose file and text exist. It never checks that the test's assertions prove the requirement as
worded, and neither does the QAE explorer, which walks only the criteria in a browser. On 2026-10-15 a
consumer's pull request passed every gate, its browser walk and three audit rounds, and its operator's
own audit still found 7 of its 28 requirements unproven. "Drag-hover and one-file enforcement on cover
upload" was proved by a test whose second dropped file was a GIF, which the type filter refuses anyway;
"created values persist after a reload" had no test that read a created journey back; "accepts PNG and
JPEG" had no JPEG case.

The proof audit closes that gap. A model on the Codex CLI is given the requirement list of the
sections the body links (and of a plan it names in a plan store: see below), and the repository's
tree, and grades every ID, cold:

- **PROVEN**: the wording split into the cases it names, and for each case the test file, the test's
  title and the assertion lines that would fail if that case broke, quoted verbatim with their line
  numbers.
- **EXCLUDED**: the plan line that gives the item to another section, quoted with its line number, and
  that section's link; the section must carry `[takes: ID]`. A stored plan's requirement is never
  EXCLUDED (the gate counts it UNPROVEN): its body's `Split off:` line gives it away instead, and the
  table lists it as **SPLIT** with its owner.
- **UNPROVEN**: what is missing, in a sentence the author can act on.

The model never sees the pull request's body or its `[proof:]` map: it finds the proving tests itself.
Its prompt ([`prompt.md`](prompt.md)) and output schema ([`schema.json`](schema.json)) are the only
copies; CI and a laptop run the same ones. Then the [`proof-audit`](../../gates/proof_audit.py) gate,
deterministic, holds the grades to the repository: every quoted assertion must be the whole text of its
line in a test file at HEAD (at least six characters, or a shorter value line such as `0,` or `[]` that
continues an assert or expect call still open above it in the same test; a line of closers, `});`, never
counts), named with the title of the test that line sits in (the nearest test declaration above it,
`void it(...)` included). A grader often has the right assertion at the wrong line number: a quote that is not on
its cited line still counts when it is the whole text of exactly one line inside the test the grade names, and the
table gives that line with the cited one beside it (`cited :33`); a quote absent there, repeated there, or found only
in another test, a hook or a helper does not. An exclusion's plan line must be at its line at the merge
base with its owner, a section the body does not link, carrying `[takes: ID]` there. A quote that is not there makes the requirement UNPROVEN: a fabricated quote is
not proof. The gate fails on any UNPROVEN. Model output is JSON held to the schema, never prose the gate
greps.

It judges proof only: not code style, not naming, and nothing the plan's wording does not state.

## The shape

Three steps, all from [`actions/proof-audit`](../../actions/proof-audit/action.yml), in the consumer's
QAE workflow so its required `Acceptance criteria` check (`qae-verify` in a copy made before the
[check names](../../docs/check-names.md)) carries the verdict:

1. **plan** (`mode: plan`), in the criteria job, on any runner: reads the body's `Ticket:` lines and the
   linked sections at the merge base (ticket-coverage's grammar and parser), and the plans its markers
   name in a plan store when `plans-endpoint` is given, and cuts the requirement IDs into shards of at
   most `max-ids` (default 10). Outputs `count`, `shards` (the grade jobs' matrix) and `plan` (the
   requirements, as JSON). No Ticket line and no stored plan is a count of 0.
2. **grade** (`mode: grade`), one job per shard, on the runner that holds the QAE explorer's
   ChatGPT-managed Codex login (`$HOME/.codex-qae`, checked as `actions/qae-codex` checks it): runs
   `bin/vibe-verifier proof-audit --requirements <plan> --shard K`, which writes `<K>.json` (the IDs it
   was given, the model's output, and the Codex CLI's own error when the run failed). Codex runs from
   the catalog's lockfile with the runner's config ignored, the output schema enforced, and stdin
   closed; a run that changed the working tree is refused. The job uploads the file as an artifact,
   always. Its sandbox is `danger-full-access`, as the QAE explorer's: on a Linux runner in an
   unprivileged container Codex's read-only sandbox (bubblewrap) cannot start
   (`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`), every read the model tries fails,
   and it grades everything UNPROVEN. That was the first run on a consumer's lane (2026-10-15).
   `sandbox: read-only` is there for a runner that can start it.
3. **verify** (`mode: verify`), in the verify job, never skipped by a `- None:` or a carried walk:
   downloads the grades, runs the gate, appends the per-ID table to the step summary and posts it as one
   pull request comment, edited in place, so the author can fix every gap in one pass. The step's exit
   code is the gate's. With `soak: "true"` (the gate's common `--soak`) the table is summarized and posted
   the same, and an UNPROVEN requirement is reported without failing the step, so a consumer can measure
   a new audit (a plan store's, say) before it blocks; a gate that could not run (exit 2) still fails it.

No Ticket line and no stored plan audits nothing, and that alone passes (a CI or docs pull request
implements no plan section). Two such bodies fail instead, so dropping the line is never a way past the
audit:

- a body that claims `[req: ID]` items (a proof map kept under its own heading, say) without the
  `Ticket:` line that links the section defining them;
- with `require-ticket` set, a pull request that changes a file matching one of its globs (the
  application's source, say) and neither links a section nor says why none applies on a line
  `No ticket: <why>`. The reason is in the body for the reviewer to read; the gate checks only that it
  is there.

A grade job that hits a model usage limit fails with the CLI's own message as its annotation
(`Codex ended with an error: You've hit your usage limit ... try again at ...`), and the gate then
exits 2 with "a model usage limit: nothing was graded; run it again after the reset". So does a shard
that ran past its `timeout` (3000 seconds by default), answered with something that is not JSON or does
not match the schema, or was never graded. None of these is ever a pass.

### Adding it to a QAE workflow

In the criteria job, after the step that writes `qae-inputs/pr-body.md`, and as three job outputs:

```yaml
    outputs:
      proof-count: ${{ steps.proof.outputs.count }}
      proof-shards: ${{ steps.proof.outputs.shards }}
      proof-plan: ${{ steps.proof.outputs.plan }}
    steps:
      - name: Plan the proof audit
        id: proof
        uses: Greenbauer/vibe-verifier/actions/proof-audit@<sha>
        with:
          mode: plan
          body: qae-inputs/pr-body.md
```

A job of its own, on the runner with the Codex login:

```yaml
  proof-audit:
    name: "Acceptance criteria: proof audit"
    needs: criteria
    if: needs.criteria.outputs.proof-count != '0'
    strategy:
      fail-fast: false
      matrix:
        shard: ${{ fromJSON(needs.criteria.outputs.proof-shards) }}
    runs-on: [self-hosted, qae-codex]   # CONSUMER: the runner that holds the Codex login
    timeout-minutes: 55
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha>
        with:
          persist-credentials: false
      - uses: actions/setup-node@<sha>
        with:
          node-version: "24"
      - name: Grade the requirements
        uses: Greenbauer/vibe-verifier/actions/proof-audit@<sha>
        with:
          mode: grade
          plan: ${{ needs.criteria.outputs.proof-plan }}
          shard: ${{ matrix.shard }}
          grades: ${{ runner.temp }}/proof-audit-grades   # outside the checkout: see below
          model: ""              # CONSUMER: the Codex model; empty is the runner's
          reasoning-effort: ""   # CONSUMER
      - name: Keep the grades
        if: always()
        uses: actions/upload-artifact@<sha>
        with:
          name: proof-audit-grades-${{ matrix.shard }}
          path: ${{ runner.temp }}/proof-audit-grades/
          if-no-files-found: error
          overwrite: true
```

In the verify job, add `proof-audit` to its `needs`, and after the QA gates:

```yaml
      - uses: actions/download-artifact@<sha>
        if: ${{ !cancelled() && needs.criteria.outputs.proof-count != '0' }}
        with:
          pattern: proof-audit-grades-*
          path: ${{ runner.temp }}/proof-audit-grades
      - name: Run the proof audit
        if: ${{ !cancelled() && steps.inputs.outcome == 'success' }}   # the step that wrote qae-inputs/pr-body.md
        uses: Greenbauer/vibe-verifier/actions/proof-audit@<sha>
        with:
          mode: verify
          body: qae-inputs/pr-body.md
          grades: ${{ runner.temp }}/proof-audit-grades
          require-ticket: |     # CONSUMER: paths a pull request changes only with a Ticket line or `No ticket: <why>`
            src/**
          token: ${{ secrets.GITHUB_TOKEN }}
```

A consumer whose plans live in a plan store gives the plan and verify steps the same three inputs, as it
gives `actions/ticket` (harnesses/qae/README.md, "The ticket's criteria"; "A plan kept in a plan store"
below says what it grades):

```yaml
          title: ${{ github.event.pull_request.title }}
          plans-endpoint: https://plans.example/api/v1   # CONSUMER: answers GET <endpoint>/plans/<id>
          plans-token: ${{ secrets.PLANS_TOKEN }}         # CONSUMER: a read-only token of the store's
          soak: "true"                                    # verify only, optional: report before it blocks
```

The verify job is already a required check, so nothing in branch protection changes. A grade job
does not wait for the preview, so it normally finishes long before the explorers do.

### Grades the pull request supplies

Both jobs check out the pull request being judged, so the grades stay in the runner's temporary
directory, outside the checkout, from the grader's write to the gate's read. A grades directory inside the
checkout is one the pull request can commit to: a file it commits there (a `<K>.json` in a folder of its
own, or a `<K>.json` directory that makes the grader's own write fail) is uploaded beside or instead of the
grader's, and a link it commits sends the grader's write, the upload or the download anywhere on the
runner. The gate refuses such grades too: a path the branch tracks at, under or on the way to `grades`, or a
symbolic link in it, is a finding, and nothing else is judged (`planted` in
[gates/_contract.py](../../gates/_contract.py), the rule the QAE gates hold their evidence to). That
refusal sees only what the verify job's own checkout holds, so it catches a copy that keeps both jobs'
grades in the checkout, but not grades the grade job uploaded from there into a download outside it. A
consumer copy made before this keeps them in the checkout (`path: proof-audit-grades/` and
`grades: proof-audit-grades`): refresh the copy, not only its pins.

## A plan kept in a plan store

A `Ticket:` section's requirements carry their IDs. A plan a requester submits to a plan store, and amends
later, carries none, so `actions/ticket` gives QAE only its Acceptance items, and a requirement written
anywhere else in it was never graded. Given the store, the audit grades its whole wording. A `[plan:<id>]`
marker in the title or body (outside code fences, HTML comments and `Split off: ... -> ...` lines) names the
plan, read from the store's own address when the step runs (a redirect is not followed, so its token goes
nowhere else), so an amendment added while the pull request is open is graded on its
next run. [`gates/_plan_store.py`](../../gates/_plan_store.py) cuts it into requirements, the same way every
time:

- every list item (nested items and continuation lines joined to their parent) and every paragraph of the
  plan, outside code fences, in the order written: `PL-1`, `PL-2`, ...;
- then each amendment's, oldest first: `AM1-1`, `AM1-2`, ... for the first; an amendment with no item and
  no paragraph at all (only a heading or a code block) is one requirement, its whole text;
- left out: headings; labels, lines that are bold and nothing else and no sentence
  (`**Added acceptance criteria**`; `**Prices never show in dollars.**` is a paragraph); closed HTML comments;
  everything under an aside heading (its subsections too), one whose text is the phrase Why, Problem(s),
  Background, Context, Repo baseline, Out of scope or Non-goals, in any case and with or without emphasis
  (`## **Why**`), followed by nothing but punctuation and what it opens (`## Why (verified on ...)`,
  `## Repo baseline: ...`, but never `## Context menu`, `## Background jobs` or `## Context-aware search`:
  a dash counts only after a space), though never under the plan's title, a `# ...` heading before any
  other; the one list, paragraph or code block directly under an aside label (`**Background**`), not a
  block after it; and an item or paragraph that opens with the label `Out of scope:` or a bold
  `**Out of scope` (`Out-of-scope requests return 403.` is a requirement);
- a second plan the pull request names numbers its IDs `P2PL-1`, `P2AM1-1`, so no two plans share one.

They follow the Ticket sections' requirements. The model is given each with `plan:<id>` as where it is
defined, and is told there is no file to read for it. A requirement this pull request leaves to another
goes on a line of the body, a list item or not, which only the plan step and the gate read:

```text
Split off: PL-4, AM1-2 -> #123
Split off: PL-7 -> https://github.com/<owner>/<name>/pull/45
Split off: AM2-1 -> [plan:<id of the plan that takes it>]
```

A split-off ID is not graded, and the table lists it as SPLIT with its owner (a pull request's URL as
`<owner>/<name>#45`). A `Split off:` line without `->` is prose and splits nothing off. A `->` line not of
that form, an ID no plan the pull request names defines (a Ticket section's item goes to another section
with `[takes: ID]` instead), an ID split off twice, an owner that is the pull request itself (`#<its
number>`, or its URL; the plan and verify steps pass the number as `--pr`) or the plan it implements, and
every requirement split off, so that nothing is graded, are could-not-run, never a pass. Whether `#123` is
a real pull request is not checked (the gate makes no GitHub call): the table shows the owner for a reviewer
to follow. A store that does not answer 200 (a redirect included), a marker with no token, and a
plan with nothing to grade are could-not-run too. With no `plans-endpoint` a marker is not read, and the
run says so in a note (a `Split off:` line then cannot run: no plan defines its IDs). The plan's wording
reaches the model's prompt as written by its requester: an instruction in it can steer a grade, never past
the gate's checks of every quote. The store's token reaches the plan step and the gate, never the model's
run.

## Run it before pushing

The same plan, prompt, schema and gate, from a catalog checkout, against a committed branch (the gate
reads git history):

```sh
<catalog>/bin/vibe-verifier proof-audit --repo . --base-ref main --body /tmp/body.md \
  --model <the workflow's model> --reasoning-effort <the workflow's effort>
```

`--body` is the pull request body as it will be posted; keep it outside the repository, which the model
reads. For a plan in a plan store add `--title="<the pull request's title>" --plans-endpoint=<URL>` (and
`--pr=<its number>` once it has one), with the store's token in `$VV_PLANS_TOKEN`, so the run grades the
list CI grades; the `=` form keeps a title that starts with a dash a title. The shards are graded in
parallel (`--jobs`, default 4) with the login in `$CODEX_HOME` (default `~/.codex`;
`codex login --device-auth` seeds one), then the table is printed and the command exits as CI's step
does: 0 every requirement proven, excluded or split off, 1 any UNPROVEN, 2 could not run (no login, a
usage limit, an unreadable ticket link or plan store). `--grades DIR` keeps the grade files. Pass the
workflow's `require-ticket` globs as `--require-ticket GLOB` (once each) to check an unlinked body as CI
does.

## Shards and time

One model run grades at most `max-ids` requirements, and each shard is a job of its own, so a family of
28 requirements is three runs at once. Measured on that pull request's head (2026-10-15, its
workflow's model at high effort): on the consumer's runner lane, grade jobs of 273, 244 and 368 seconds
for shards of 10, 9 and 9 requirements, about 7 minutes from the plan to the verdict; from a laptop,
4 to 7 minutes for the three at once. The job budget is 55 minutes.

## The one-file replay

[`tests/fixtures/proof-audit/c8`](../../tests/fixtures/proof-audit/c8) is the GIF case with the same
shape and none of its code: a plan item, an uploader that crops every accepted file, and a test that
drops a PNG and a GIF and asserts one crop. The real grader graded it UNPROVEN ("the second dropped file
is a GIF that the type filter rejects anyway"), and that grade is kept in `grades/1.json`; under
`proven/` the uploader keeps only the first file, the test drops a PNG and a JPEG and asserts which one
was kept, and the real grader graded that PROVEN. The suite replays both grades through the gate, and
holds the PROVEN grade to the unfixed tree, where its quote is not there. With a Codex login,
`VV_PROOF_AUDIT_LIVE=1` (optionally `VV_PROOF_AUDIT_MODEL`, `VV_PROOF_AUDIT_EFFORT`) runs the real
grader on both trees: `python3 -m unittest tests.test_proof_audit.Replay` from the catalog root.

## Limits

- Whether an assertion exercises a case is the model's judgment. The gate proves the quotes are real,
  not that they mean what the model says; the grader is told to grade UNPROVEN when in doubt, and it
  is strict (it asked for a visibility assertion where a test read only an error's text). Two grades of
  one head can differ: six grades of the 28-requirement head found 9 to 12 UNPROVEN, the one-file and
  persistence gaps every time. One of them took a server test of a JPEG upload intent, quoted by its
  `assert.equal(` line, as proof that the upload form accepts a JPEG; the gate now refuses a quote that
  only opens a call, and the prompt asks for a screen's behaviour to be proved on that screen.
- The model is told to judge from the code alone and only to read. In CI it runs without a sandbox (see
  above), on a runner that holds no token for it; locally, under the read-only sandbox. Neither stops it
  reading a file outside the checkout, such as the runner's event payload, which holds the body; it is
  told not to look. A run that changed the working tree is refused, but a write outside the checkout is
  not seen.
- A grade covers the tree the job checked out (the pull request merged with its base, in CI). A push
  is graded again.
- `ticket-coverage` still requires the body's `[proof:]` claims; the audit does not read them.
- An assertion counts for the test it sits in, found as the nearest test declaration above it (a
  `test(...)`/`it(...)` call and its `.only`, `.skip`, `.each(...)` forms, or a Python `def test_*`); a test
  whose title is not a string literal is matched on the declaration's text. A line in a hook or a helper
  does not count, which is what the prompt asks for too: the line must also sit inside the named test's
  body, which ends at the first line after its declaration indented no deeper than the declaration (a
  declaration wrapped over up to 40 lines, a comment line, and the text of a JS template literal or block
  comment or a Python triple-quoted string do not end it), so a hook or helper after the test is refused even
  when the grader cites its exact line, whatever the test's title. The reading is line by line, not a parser, and
  is built for formatted code: it steps over strings, over a regex literal whose `/` follows an operator, an
  opening bracket, a comma or `return`, and over a template literal's `${...}` as code, so a template nested in
  one (`${items.map((i) => `<li>${i}</li>`)}`) closes where it does. JSX (in a template's `${...}` too), or a
  regex after `)` or `await`, holding a backtick or `/*` reads as opening a template or comment, so the body runs to
  the end of the file and a hook after it passes. Code after the `{` on a JS declaration's own line that ends in `)` or `);` can end the
  body early and turn away an assertion in it.
- The grade artifacts are named by shard. A re-run whose plan has fewer shards than the first attempt's
  finds the extra shard's grades, and the gate cannot run (never passes) until the next push.
