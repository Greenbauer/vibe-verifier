# Affected tests

A pull request's test job usually runs every test, although most of them cannot be affected by what
the pull request changed. This action lists the test files that can be, and the job's own runner runs
that list. It is not a gate: it decides nothing about the code, and the tests themselves still pass or
fail the job.

Measured on a 277-pull-request history of a private TypeScript monorepo: for every pull request that
changed a test, its code changes were reverted and the head's tests run on that, and each test that
failed is a deliberate break. The selection listed all 132 of them. On the last 15 pull requests of
three private repositories (that monorepo and two others) it listed a median of 16%, 22% and 39% of
their unit test files, and every test on 1 pull request in 15 in each.

## The rule

[`gates/_affected_tests.py`](../../gates/_affected_tests.py) has it in full.

1. **The import graph.** Every test file whose imports reach a changed file, directly or through other
   files: relative imports, tsconfig `paths` aliases and `baseUrl`, workspace package names (their
   `exports`, `main` or `src/index`), `vi.mock` and `jest.mock` of a path, Python `import` and
   `from ... import`, relative or absolute ([`gates/_affected_graph.py`](../../gates/_affected_graph.py)).
   A test runner's config counts as imported by every test under its directory, so a change to what
   its setup file (or a `conftest.py`) imports reaches those tests.
2. **Always: tests that read files.** Every test that reads files, starts a process or uses a database,
   or imports a file that does: the graph cannot see what it reads. Read by code shape (`node:fs`,
   `child_process`, `readFileSync(`, `import.meta.glob`, a computed `import(`, `DATABASE_URL`, Python's
   `open(`, `subprocess`, `.read_text(`), so a comment that mentions them does not count.
3. **ALL** instead when a lockfile (`pnpm-lock.yaml`, `package-lock.json`, `yarn.lock`, `bun.lock`,
   `poetry.lock`, `uv.lock`, `requirements*.txt`, ...), a tsconfig, a test runner's config (Vitest,
   Vite, Jest, Mocha, Bun, pytest, `pyproject.toml`, `conftest.py`; Playwright's only when a listed test
   imports `@playwright/test`), a runtime pin (`.nvmrc`, `.node-version`, `.python-version`) or a path
   `all-on` names changed; when a `package.json` changed `dependencies`, `devDependencies`,
   `peerDependencies`, `optionalDependencies`, `engines` or `packageManager` (its `scripts` do not
   count, so a repository that lists its test files there keeps selecting); and when there is no base
   (not a pull request) or it cannot be read.

A test file is a JS or TS file named `*.test.*` or `*.spec.*` or under `__tests__/`, a JS or TS file
that imports `node:test`, or a Python test (`test_*.py`, `*_test.py`, `test-*.py`, `*-test.py`,
`*.test.py`). `tests` replaces that definition, `exclude` drops files from it, `always` lists some
whatever changed.

## Run the selection, then hand the list to the runner

The checkout needs the base, so `fetch-depth: 0`. On a push to the default branch (or any event but a
pull request) `base-ref` is empty and `all` is `true`: main always runs the whole suite.

```yaml
      - uses: actions/checkout@<sha> # pin it
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: Select the affected tests
        id: affected
        uses: Greenbauer/vibe-verifier/actions/affected-tests@<sha>
        with:
          exclude: |
            e2e/**
      - name: Unit tests
        env:
          ALL: ${{ steps.affected.outputs.all }}
          LIST: ${{ steps.affected.outputs.list }}
        run: |
          if [ "$ALL" = true ]; then npm test; exit; fi
          mapfile -t files < "$LIST"
          if [ ${#files[@]} -eq 0 ]; then echo "No test file can be affected by this pull request."; exit 0; fi
          npx vitest run "${files[@]}"
```

Read the list with `mapfile`, never an unquoted `$(cat ...)`: a Next.js path such as
`app/(shop)/[id]/page.test.tsx` is a glob to the shell. The paths are relative to the repository root:

- **node:test, pytest, and Vitest run from the root** take them as they are: `node --test "${files[@]}"`,
  `python -m pytest "${files[@]}"`.
- **Vitest run in each package of a monorepo** matches a path against its own root, so a root-relative
  path matches nothing there. Make them absolute and let a package with none of them pass:
  `mapfile -t files < <(sed "s|^|$PWD/|" "$LIST")`, then in each package
  `vitest run --passWithNoTests "${files[@]}"`.

A selection that fails (exit 2: not a git checkout, a crash) fails the step, and the job with it. It
never reads as an empty list.

## Report mode: soak before you trust it

A repository other than the one measured starts here. It keeps running the full suite on every pull
request, and the action names any test that failed in the full run but was not selected: a miss, which
the selection alone would have let through. Run the selected tests first if you want their time.

```yaml
      - name: Select the affected tests
        id: affected
        uses: Greenbauer/vibe-verifier/actions/affected-tests@<sha>
      - name: Selected tests, timed
        if: steps.affected.outputs.all == 'false' && steps.affected.outputs.count != '0'
        env:
          LIST: ${{ steps.affected.outputs.list }}
        run: |
          mapfile -t files < "$LIST"
          npx vitest run "${files[@]}"
      - name: Full suite
        if: ${{ !cancelled() }}
        run: npx vitest run --reporter=default --reporter=junit --outputFile.junit=test-results/junit.xml
      - name: Selection misses
        if: ${{ !cancelled() }}
        uses: Greenbauer/vibe-verifier/actions/affected-tests@<sha>
        with:
          report: test-results/junit.xml
```

`report` takes one or more files: a JUnit XML report (Vitest's `--reporter=junit`, pytest's
`--junitxml=`), or a list of failed test files, one per line. node's own JUnit reporter does not say
which file a test is in, so for node:test add the action's reporter beside your own:
`node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter="$REPORTER"
--test-reporter-destination=test-results/failed.txt ...`, with `REPORTER` set from
`${{ steps.affected.outputs.reporter }}`, and pass `test-results/failed.txt`. unittest writes no report
of its own: a list of its failed test ids (`test_cart.Cart.test_total`) works, since the report finds a
dotted id's file. This repository's own [`ci.yml`](../../.github/workflows/ci.yml) reads them from the
verbose log of `python3 -m unittest discover -s tests -v`, and fails that step when the run failed and
no id could be read. Every miss is a
`::warning` on the run; a failure whose report names no file the selection counts is one too. The
step exits 0 on misses (the full run already failed the job) and 2 when the results cannot be read or
hold no test case.

Pass the same `tests`, `exclude`, `always` and `all-on` to both steps: the report recomputes the selection.

## Inputs and outputs

| Input | Default | Meaning |
|---|---|---|
| `base-ref` | `github.base_ref` | The branch the pull request merges into; empty means every test runs. |
| `tests` | (empty) | What a test file is, one glob per line. |
| `exclude` | (empty) | Test files never to list, one glob per line. |
| `always` | (empty) | Test files to list whatever changed. |
| `all-on` | (empty) | Paths whose change means every test runs, such as your own test runner script. |
| `report` | (empty) | The full run's results, one file per line: prints the misses instead of selecting. |

| Output | Meaning |
|---|---|
| `all` | `true` when every test must run. |
| `list` | A file of the selected test files, one per line; empty when none or all. |
| `count` | How many the list holds; empty when every test must run. |
| `reporter` | The node:test reporter module for `report`. |

From a clone: `bin/vibe-verifier affected-tests --repo . --base-ref main` prints the same list, with
the reason on stderr.
