# Feature maps

A feature map is one short Markdown file per user-facing feature: what the feature owns in code, how
a user reaches it, what proves it works, and what tends to go wrong. An agent reads the files of the
features a change touches before making it, and re-walks them after. A map helps only while it is
true, so the [`feature-map`](../gates/feature_map.py) gate fails a pull request that leaves it wrong.

The idea of one short verification file per feature comes from Poteto's pstack
(`/create-verification-skill`). The deterministic drift check below is this catalog's own. In a
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
  the state a bug needs never meets the bug.
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

It cannot run (exit 2, which `--soak` never masks) when the map has no feature file, or a `--surface`
is malformed, matches no tracked file, or finds no surface: an extractor that reads nothing checks
nothing, and a pattern that stopped matching after a reformat would otherwise pass every map.

The line's arguments are judged from the base like any manifest line, so a pull request cannot drop
a `--surface` to pass. The map itself is judged at the head, because the map is what a pull request
has to bring up to date.

## Creating, updating and retiring features

The map changes in the same pull request as the code it describes, by whoever writes that pull
request: the agent or person adding, changing or removing the feature. The gate is what makes that
happen, because the code change alone leaves the map wrong.

- **Create.** A pull request that adds a user-facing feature adds `docs/features/<id>.md` with the
  four sections and a row in the README index. A feature that brings a new surface (a route, a page,
  a command) cannot be left out: until a file lists it, the surface is owned by no feature. Prefer a
  directory glob (`src/auth/**`) where a feature owns a directory, so a new file there is covered.
- **Read.** Before a change, find every feature whose source globs match the files being changed,
  re-walk its Reach and Verify, and run its tests, not only the tests for the behaviour meant to
  change. Reviewers read the map changes in the diff like any other change.
- **Update.** A pull request that renames or removes a surface, moves a source file, or renames a
  test a feature cites updates that feature in the same change. The gate forces each one: a renamed
  route is a stale listing and an unowned surface, a moved file leaves its glob matching nothing,
  and a renamed test leaves its anchor dangling. When the behaviour changes, its Reach, Verify steps
  and Gotchas change with it; the gate cannot see that, so the review does.
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
