# Feature maps

A feature map is one short Markdown file per user-facing feature: what the feature owns in code, how
a user reaches it, what proves it works, and what tends to go wrong. An agent reads the files of the
features a change touches before making it, and re-walks them after. A map helps only while it is
true, so the [`feature-map`](../gates/feature_map.py) gate fails a pull request that leaves it wrong.
The QAE harness can also [re-walk](#re-walking-the-features-a-pull-request-touches) in a browser the
features each pull request touches.

The idea of one short verification file per feature comes from Poteto's pstack
(`/create-verification-skill`). The deterministic drift check and the re-walk of only the features
a pull request touches are this catalog's own. In a
historical pilot on a consumer web app, a map and a blind browser re-walk of the features past pull
requests touched caught two shipped regressions those pull requests' own acceptance criteria missed.

## The format

```
docs/features/
  README.md        the index: one row per feature, linking its file (read by people, not by the gate)
  sign-in.md       one file per feature; the file name is the feature's id
  projects.md
```

Each feature file has these sections. Others (a `## Sub-features` list, say) are allowed and not read.

````markdown
# Sign-in

Email-and-password sign-in, account confirmation and password reset.

## Surfaces

- route: `/login`
- route: `/signup`
- source: `src/auth/**`
- source: `src/routes.ts`

## Reach

Signed out, open `/login`. Create Account goes to `/signup`; Forgot password goes to `/forgot`.

## Verify

- `test/auth/login.test.ts::retains the email after a failed sign-in`
- `e2e/auth.spec.ts::a reset link signs the user in once`

1. Sign out, open `/login`, and sign in with a wrong password: the form says so and keeps the email.
2. Sign in as the seeded test user: the dashboard opens.

## Gotchas

- A reset link works once; a second use must say it expired, not sign anyone in.
````

- **Surfaces** is what the feature owns in code, one bullet per line.
  - `` - source: `glob` `` names files that implement it, relative to the repository root: `*` matches
    within one path segment and `**` across segments. A shared file is listed by every feature it
    implements. Every feature lists at least one.
  - `` - <kind>: `value` `` names a surface the code declares, such as a route, a page or a command.
    Which kinds exist, and where the code declares them, is the gate's configuration (below).
- **Reach** is how a user gets there, in the product's own words.
- **Verify** is what proves the feature works: tests as `` - `path::exact test title` `` (the title
  verbatim in that file) or `` - `path:line` `` / `` - `path:start-end` ``, the anchor forms the
  acceptance gate reads, and browser steps as a numbered list. Write each step so it sets up the
  state it checks (which user is signed in, which record exists first): a re-walk that never creates
  the state a bug needs never meets the bug. Each numbered step is one state of the feature, and a
  [re-walk](#re-walking-the-features-a-pull-request-touches) covers a few of them, not all, so a step
  must stand on its own, without the steps before it.
- **Gotchas** are the traps a change or a verification run tends to hit.

Every top-level `- ` bullet under Surfaces and Verify must have one of the shapes above. A bullet
that does not is a finding, never skipped, because a typo would otherwise drop a claim without a word.

## Keeping it true: the `feature-map` gate

Subscribe with a line in `.vibe-verifier`. The line says where the code declares each kind of surface:

```
# A web app whose routes are declared in one file:
feature-map --surface route src/routes.ts '^\s*path: "([^"]+)"'
# A command-line tool:
feature-map --surface command src/cli.py 'add_parser\("([a-z-]+)"'
# A site with no API, whose pages are files:
feature-map --surface page 'content/**/*.md'
# Anchors and source globs only (the pass says completeness was not checked):
feature-map
```

This repository's own map, [`docs/features/`](features/README.md), is the example for a repository
with no site: its surfaces are its gates (each `gates/*.py` declares `GATE = "<name>"`) and its
`bin/vibe-verifier` subcommands, and its `.vibe-verifier` carries the line that checks them.

- `--surface KIND GLOB REGEX`: every match of REGEX in every tracked file GLOB matches is a surface
  of that kind, its value the one capture group. `^` and `$` match at each line. Choose a pattern that
  matches only declarations: a string literal used elsewhere would become a surface no feature owns.
  A REGEX may not begin with `-` (write `[-]`).
- `--surface KIND GLOB`: every tracked file GLOB matches is a surface, its value the path.
- Both are repeatable, and two for one kind add up. `--dir` names the map (default `docs/features`).

The gate reads the whole map at the head on every run, not only what the pull request changed, and
fails when

1. the code declares a surface that no feature lists (it is owned by no feature);
2. a feature lists a surface the code no longer declares, or a kind no `--surface` declares;
3. a source glob matches no tracked file, or a feature lists none;
4. a Verify anchor does not resolve, or a Verify section has neither an anchor nor a step;
5. a bullet is malformed, a feature has no `## Surfaces` or `## Verify`, or its file name is not one
   word (it is the feature's id, and a QAE verdict line names the feature by it).

Since the whole map is judged, a repository subscribes clean: run the gate on the default branch
first and land the fixes with the subscription.

With no `--surface` the gate checks anchors and source globs and says plainly that completeness was
not checked: an advisory finding (a warning annotation on GitHub) and a run summary row reading
`PASS (completeness not checked)`. A surface no feature owns passes unseen there.

It cannot run (exit 2, which `--soak` never masks) when a `--surface` is malformed, matches no
tracked file, or finds no surface: an extractor that reads nothing checks nothing, and a pattern
that stopped matching after a reformat would otherwise pass every map.

The line's arguments are judged from the base like any manifest line, so a pull request cannot drop
a `--surface` to pass. The map itself is judged at the head, because the map is what a pull request
has to bring up to date.

### A head with no map

When the head has no feature file, the gate asks the base (`--base-ref`, resolved as
[every gate resolves it](GATE-CONTRACT.md#base-ref-resolution)) what happened. It decides from the
commits alone:

| The merge bases of the base and the head | The base | What it means | Result |
|---|---|---|---|
| none has a feature file | has feature files | the branch predates the map: the map landed on the base after the branch left it | pass, labelled `not checked` |
| one has feature files | either | the pull request removed the map | exit 2 |
| none has a feature file | has none | the subscription has no map | exit 2 |

- **The branch predates the map.** A pull request opened before the map merged did nothing wrong,
  and until it takes the base it has no map to keep true. The gate passes, checks nothing (not the
  `--surface` lines either: the files they read may be as new as the map), and says so: an advisory
  finding that names the base to take, and a run summary row reading
  `PASS (not checked: the branch predates the feature map)`, never a bare `PASS`. Once the branch
  merges or rebases onto the base it has the map, and the whole map is judged against its code like
  any other.
- **The pull request removed the map.** Exit 2, as a missing map always was, and the message now
  says the map was removed. It is not a finding, because `--soak` reports findings and passes: a
  soaked gate would let the removal merge, and from then on no run on the base could judge
  anything. To stop keeping a map, first merge a change that removes what reads it (the
  `feature-map` line, the `--features` option), then delete the map. After a criss-cross merge the
  base and the head have several merge bases; the map counts as removed when any of them has it.
- **Nothing tells the two apart.** No base resolves, the base and the head share no merge base (a
  shallow clone: check out with `fetch-depth: 0`), or the base has no map either. Exit 2, as
  before. So is a run whose environment names a base (`$VIBE_VERIFIER_BASE_REF`, `$GITHUB_BASE_REF`)
  that the checkout does not have: other gates fall through to `main` there, and this pass must
  not rest on a guess. A pull request that brings the first map is none of these: its head has a
  map, and it is judged on that map whatever the base has.

Before this rule every missing map was exit 2. A consumer added the gate to its gate list right
after its map merged (2026-10-08), and each pull request already open went red until it took the
base branch, through `--soak` too.

## Re-walking the features a pull request touches

The [QAE harness](../harnesses/qae/README.md) can re-walk, in the same browser session as the
acceptance criteria and on the same application, a few states of every feature a pull request's
changes touch, and the verify job refuses the run unless each one has an anchored PASS over at
least one step that was walked.

**Which features.** A feature is touched when one of its own `source:` globs matches a changed path
(both names of a rename). Two rules keep the selection narrow, both read from the map and nothing
else (no history, no outcomes):

- A changed path that more than `--shared-over` features list (default 2, so three or more) is
  shared, a stylesheet or a router, and selects none: it says nothing about which feature changed.
  In the pilot, shared files made one pull request select about 18 of its 19 features.
- At most `--max-features` (default 3) are selected: those owning the most changed paths, then by
  id. The run's log names the ones over the cap.

A feature file the base has selects by its Surfaces as the base has them, and one the pull request
adds selects by its own, like a rule file under `repo-rules`: a pull request cannot take a feature
out of its own re-walk by narrowing its globs or listing a file in more features. A feature the pull
request deletes is retired and selects nothing.

**A head with no map.** The selection follows [the gate's rule](#a-head-with-no-map). On a branch
that predates the map, `bin/vibe-verifier features` prints `features: 0` and the reason, the
explorer re-walks nothing, and `acceptance-verdict` asks for no `regression-check` line and labels
its pass `re-walk not run: the branch predates the feature map`. Features are re-walked once the
branch takes the base. Every other head with no feature file is exit 2 in both, as it always was: a
pull request that removed the map (the message says so), and a missing map nothing explains.

**Turning it on.** Add the selection to the `acceptance-verdict` line of `.vibe-verifier-qae`:

```
acceptance-verdict --criteria qae-inputs/pr-body.md --verdict qae-inputs/verdict.md --artifacts qae-artifacts --features docs/features --changed-files qae-inputs/changed-files
```

and use a QAE template ([`explore.yml`](../harnesses/qae/explore.yml) or
[`explore-codex.yml`](../harnesses/qae/explore-codex.yml)) that has the `Select the features to
re-walk` step. Optional: `--max-features N`, `--shared-over N`, `--max-states N`. The line is judged
from the base, so the pull request that adds it is not re-walked and the next one is, and one that
removes it still is.

**What happens.** The explore job runs `actions/features` (`bin/vibe-verifier features`), which
copies each selected feature file to `qae-inputs/features/<id>.md` and writes
`qae-inputs/features.md`: how many states of each feature to walk, and the changed paths that
selected each. After the criteria the explorer re-walks each feature in part:

- A feature's states are the numbered steps of its Verify section (in a file with none, what each
  cited test title describes). The explorer walks at most `--max-states` of them per feature
  (default 3): first the states closest to the feature's changed paths, and one state of every
  feature before a second state of any, so that none is left out when time runs short. Which
  states are closest is the explorer's judgment; the number is the manifest's.
- It checks what the state says and nothing more: the checks it adds to a criterion (a reload
  after a save, an invalid input) are not part of a re-walk, so a state that needs one says so.
- It records each walked state like a criterion: `qae/features/<id>.md` with a screenshot per step.
- It adds to its verdict one `regression-check` line per feature, and one `regression-skip` line
  naming the states it did not walk:

```
regression-check: sign-in -- PASS -- still signs in (qae/features/sign-in.md::step 2: signed in as the test user -> the dashboard)
regression-skip: sign-in -- 3, 4, 6
```

**What a FAIL means.** A step that was walked showed the feature no longer works the way its file
describes. A state the explorer did not get to, or could not set up, is never a FAIL: it goes on
the `regression-skip` line. Before this bound the explorer was told to walk a whole file, numbered
steps and cited tests alike, and on a consumer's live run (2026-10-07) it left three features
unfinished and wrote FAILs that named no defect, only the states it had not walked.

**The pull request's own copy.** `actions/features` copies each selected feature file from the
explore job's working tree, which holds the pull request's changes, so a pull request that changes
how a feature behaves and updates the feature's Reach and Verify in the same change is re-walked
against its own description. Only the selection reads the base (the Surfaces, above). So when a
walked step contradicts the file because the pull request means the new behaviour, the fix is to
update the file in that pull request, and the gate's FAIL finding says so.

**What the gate requires.** The verify job's `acceptance-verdict` makes the selection again itself,
from its own arguments and the map, and never takes the explorer's word for which features were
selected. For each selected feature it requires:

- exactly one `regression-check` line, a PASS whose anchor resolves. A missing line, a FAIL, or a
  PASS that points at nothing is a finding;
- at least one walked step in the evidence: a `- step k:` line in `qae/features/<id>.md` whose
  screenshot `qae/features/<id>-step-k.png` is there. A feature with a line and no walked step was
  not re-walked, which is a finding, never a pass. (`--features` therefore needs `--artifacts`.)

It prints how many steps were walked and each `regression-skip` line, and judges neither the
number of states nor what the skip line says: nothing the gate decides is read from the
explorer's prose. `qae-artifacts` holds the feature step logs to the same screenshot rule as the
criteria's, and the QA review comment lists each re-walked feature with the word the explorer
wrote and the states it left unwalked.

**Limits.** The re-walk runs when the explorer runs, that is on a pull request that lists acceptance
criteria. One that declares `- None: <why>` is not explored, so it is not re-walked either: the
declaration is a claim the review weighs against the diff. A change made only in shared files
selects no feature, so list each feature's own files, not only the shared ones it runs through
(run over 30 past pull requests of the pilot, the rule picked 0 to 3 features each, and two fixes
made wholly in shared files picked none). A re-walk covers at most `--max-states` states of a
feature, so a regression in a state it did not walk passes unseen; the skip line says which those
were. And an anchored PASS proves a step was logged and photographed, not that the feature is right.

## Creating, updating and retiring features

The map changes in the same pull request as the code it describes, by whoever writes that pull
request: the agent or person adding, changing or removing the feature. The gate is what makes that
happen, because the code change alone leaves the map wrong.

- **Create.** A pull request that adds a user-facing feature adds `docs/features/<id>.md` with the
  four sections and a row in the README index. A feature that brings a new surface (a route, a page,
  a command) cannot be left out: until a file lists it, the surface is owned by no feature. Prefer a
  directory glob (`src/auth/**`) where a feature owns a directory, so a new file there is covered.
- **Read.** Before a change, find every feature whose source globs match the files being changed
  (`bin/vibe-verifier features --manifest .vibe-verifier-qae --changed-files <file>` lists them when
  the re-walk is on), re-walk its Reach and Verify, and run its tests, not only the tests for the
  behaviour meant to change. The QAE re-walk reads the same files. Reviewers read the map changes in
  the diff like any other change.
- **Update.** A pull request that renames or removes a surface, moves a source file, or renames a
  test a feature cites updates that feature in the same change. The gate forces each one: a renamed
  route is a stale listing and an unowned surface, a moved file leaves its glob matching nothing,
  and a renamed test leaves its anchor dangling. When the behaviour changes, its Reach, Verify steps
  and Gotchas change with it; the `feature-map` gate cannot see that, so the review does, and with
  the re-walk on, a walked step that no longer matches the file fails the run until the pull
  request updates it.
- **Retire.** A pull request that removes a feature from the product deletes its file and its index
  row. The gate forces this too: the removed surfaces are stale listings, the removed files leave the
  globs matching nothing, and the removed tests leave the anchors dangling. A feature folded into
  another moves its bullets to the survivor and is deleted.

## What it does not check

- That a Verify test asserts what the feature claims: an anchor proves the test exists, not what it
  proves.
- That Reach, the Verify steps and Gotchas describe the product as it is.
- That every source file belongs to a feature: completeness holds for the surfaces a `--surface`
  declares, not for every file.
- The README index.
