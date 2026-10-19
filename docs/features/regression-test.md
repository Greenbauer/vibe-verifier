# Regression test fails on base

`regression-test-fails-on-base` fails a fix pull request unless one of its new or changed tests fails on the code before the fix and passes at the head. It is meant to replace `new-source-has-test` for test presence: a test that exists but cannot fail on the bug it was written for is what that gate lets through.

## Surfaces

- gate: `regression-test-fails-on-base`
- source: `gates/regression_test_fails_on_base.py`
- source: `gates/_regression_runners.py`
- source: `gates/_regression_python.py`
- source: `gates/_regression_node.py`
- source: `gates/_regression_frameworks.py`
- source: `gates/_regression_playwright.py`
- source: `gates/_local_only.py`

## Reach

A wired gate that runs the project's tests, so it goes in a manifest of its own (such as `.vibe-verifier-tests`), run by a job that installs the project's dependencies and writes the pull request's title and body first, the way the QAE harness writes the body:

```text
regression-test-fails-on-base --title pr-inputs/title.txt --body pr-inputs/body.md
```

```yaml
- name: Write the pull request's title and body
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}   # the job needs pull-requests: read
    PR_NUMBER: ${{ github.event.pull_request.number }}
    REPO: ${{ github.repository }}
  run: |
    mkdir -p pr-inputs
    gh pr view "$PR_NUMBER" --repo "$REPO" --json title --jq .title > pr-inputs/title.txt
    gh pr view "$PR_NUMBER" --repo "$REPO" --json body --jq .body > pr-inputs/body.md
```

`--python` names the project's interpreter, `--tests GLOB` declares more test code it never runs (an end-to-end directory's helpers, say; a spec a `--playwright` config collects runs all the same), `--node-test GLOB` declares node:test files the repository runs as `node --test <file>` from a shell script or a workflow step rather than a package.json script, `--timeout` bounds both runs (default 600 seconds). A node:test file a package.json script runs (`"test:unit": "node --require ./setup.cjs --test src/*.test.mjs"`) needs no declaration: the gate reads the script and runs the file the same way, from that package. The rules, what it runs and why are in [the gate contract](../GATE-CONTRACT.md#regression-test-fails-on-base).

### Browser specs (`--playwright`)

`--playwright CONFIG` (repeatable) names a Playwright config the gate runs a fix's browser specs with; `--local-env NAME` (repeatable) declares a variable named like a credential that holds a secret of the job's own local stack:

```text
regression-test-fails-on-base --title pr-inputs/title.txt --body pr-inputs/body.md --playwright playwright.config.ts --tests 'e2e/**' --timeout 1200
```

A changed spec that one of those configs collects (`playwright test --list`) runs under it, even when `--tests` also names it, so `--tests 'e2e/**'` keeps a spec's helpers at the head for the run before the fix while the spec itself runs. The site under test is the one the config's own `webServer` starts on the runner: Playwright starts it from the working tree as the gate left it, so the first run tests the head and the second the code before the fix. Production is never a target, and neither is a preview: a preview serves the pull request's head, never the code before the fix. Before any server, global setup or browser starts, the gate refuses (exit 2) a project that runs a changed spec, or one it depends on, whose `baseURL` is not set or is not this machine (localhost, a `*.localhost` name, 127.0.0.0/8, ::1), a `webServer` url that is not this machine, a config with no `webServer`, and a credential for anywhere else in what the run inherits: the gate's environment, a `webServer`'s `env`, and every `.env` file a framework would load anywhere in the repository but `node_modules` and hidden directories ([the rules](../GATE-CONTRACT.md#regression-test-fails-on-base)). During each run, every browser context and the `request` fixture go through a proxy the gate runs on the runner, bypassed only for loopback names, which answers every request 403 and records the host it asked for; the Node side of the run (the site's server, the specs' own code, a global setup) gets the same proxy through `HTTP_PROXY`/`HTTPS_PROXY`, `NO_PROXY` for loopback and `NODE_USE_ENV_PROXY=1`. A changed spec that fails in a run that reached for any host is exit 2, whichever request it was (the spec's own, a page's script, an in-page fetch, the site's server-side fetch): its failure cannot be told from the block, and a request made only before the fix would otherwise read as the bug showing. A spec that passes all the same is judged as usual. A fix whose changed tests all import another framework, or that node:test runs, never starts Playwright; a test that only calls `test()` or `describe()` with no import (a Vitest or Jest file with globals, or a spec that imports its `test` from a fixtures module) is listed, and when no changed test imports `@playwright/test` and that listing cannot run (a credential in the step, no Playwright), they are classified as before.

What the repository provides:

- **A config whose `webServer` serves the working tree on this machine**: a dev server (`next dev`), or a build and a start in one command, on a loopback address, with every project's `baseURL` there. A server that only starts a build made earlier (`next start` alone) serves the head in both runs, and the spec then reads as not reproducing the bug. The gate sets `reuseExistingServer: false`, so a server already answering at that address makes the run fail rather than test the wrong code. A repository whose main config targets a preview can name a second config for the gate that imports the first and adds the `webServer`.
- **The job's own local stack**, started before the gate: the database with its migrations and the test accounts the specs sign in as (`supabase start`, then `supabase db reset` with a seed), and the site's environment pointing at it. `supabase status -o env --override-name api.url=NEXT_PUBLIC_SUPABASE_URL` is the Supabase CLI's own example for writing those variables; its keys say `iss: supabase-demo`, which the gate reads as local.
- **Dependencies and a browser**: the project's packages and `npx playwright install --with-deps chromium`.
- **A Node that follows `NODE_USE_ENV_PROXY`** (22.23 is checked) for the site's server-side requests to other hosts to be blocked too, a site that **reaches no other host while its specs run** (turn analytics, error reporting and remote scripts off in the gate's config, or a failure cannot be judged), and **no site build that fetches from the internet** (`next/font/google` downloads its fonts when the site compiles, and is blocked like any other host: self-host the fonts, or mock them in the gate's config).
- **A gate step whose environment holds nothing but that stack's values**: no production or preview key, token, password or connection string at the step or the job, and none in a `.env` file the site loads. A local secret the stack needs that is named like a credential (`SESSION_SECRET=local-only`) is declared with `--local-env SESSION_SECRET`; its value is still refused if it is a hosted project's token or address.
- **Specs that reach the site by its `baseURL`**.

What it costs each fix pull request whose changed tests include a spec: starting the local stack (once per job), two starts of the site, the changed specs twice (one attempt each, `--retries=0`), and three `playwright test --list` calls per config (a config load each, no server: one to find the changed specs at the head, one before each run), more when a file does not load. On the catalog's synthetic fixture (a node server and one spec) the whole gate takes about 9 seconds; in a real repository the stack's start, the site's build or dev-server warm-up and the specs themselves dominate. The job runs on every pull request unless its workflow starts the stack only for a title of type `fix`.

## Verify

- `tests/test_regression_test.py::test_a_test_that_fails_on_the_code_before_the_fix_passes`
- `tests/test_regression_test.py::test_a_test_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug`
- `tests/test_regression_test.py::test_a_test_that_fails_there_only_because_what_it_imports_is_missing_does_not_show_the_bug`
- `tests/test_regression_test.py::test_the_reason_line_passes_a_fix_and_prints_the_reason`
- `tests/test_regression_test.py::test_a_fix_that_changes_only_tests_docs_or_comments_passes_without_running_a_test`
- `tests/test_regression_test.py::test_a_pull_request_that_is_not_a_fix_passes_without_running_a_test`
- `tests/test_regression_test.py::test_a_test_that_fails_at_head_is_a_red_suite_and_cannot_run`
- `tests/test_regression_frameworks.py::test_a_jest_or_bun_test_is_not_judged_and_names_its_runner`
- `tests/test_regression_frameworks.py::test_a_fix_whose_only_tests_are_jest_or_bun_tests_passes_qualified_and_is_never_exit_2`
- `tests/test_regression_frameworks.py::test_a_test_whose_package_runs_vitest_is_a_vitest_test_without_importing_it`
- `tests/test_regression_frameworks.py::test_each_way_a_package_names_its_runner`
- `tests/test_regression_test.py::test_pytest_a_test_that_passes_there_or_fails_only_on_a_missing_module_fails`
- `tests/test_regression_test.py::test_unittest_a_test_whose_failing_check_is_in_a_subtest_fails_on_the_code_before_the_fix`
- `tests/test_regression_node.py::test_a_node_test_that_fails_on_the_code_before_the_fix_passes`
- `tests/test_regression_node.py::test_a_node_test_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug`
- `tests/test_regression_node.py::test_a_node_test_that_fails_there_only_because_what_it_needs_is_missing_does_not_show_the_bug`
- `tests/test_regression_node.py::test_a_node_test_that_fails_at_head_is_a_red_suite_and_cannot_run`
- `tests/test_regression_node.py::test_a_package_script_runs_its_files_from_its_package_with_its_own_options`
- `tests/test_regression_node.py::test_a_node_test_the_repository_does_not_run_with_node_test_is_not_judged_unless_declared`
- `tests/test_regression_node.py::test_a_file_whose_top_level_assertion_fails_on_the_code_before_the_fix_shows_the_bug`
- `tests/test_regression_playwright.py::test_a_browser_spec_that_fails_on_the_code_before_the_fix_passes`
- `tests/test_regression_playwright.py::test_a_browser_spec_that_passes_on_the_code_before_the_fix_does_not_reproduce_the_bug`
- `tests/test_regression_playwright.py::test_a_browser_spec_that_fails_there_only_because_what_it_imports_is_missing_does_not_show_the_bug`
- `tests/test_regression_playwright.py::test_a_spec_that_reaches_for_another_host_is_blocked_and_cannot_be_judged`
- `tests/test_regression_playwright.py::test_the_request_fixture_reaching_for_another_host_is_blocked_and_cannot_be_judged`
- `tests/test_regression_playwright.py::test_the_site_s_own_server_reaching_for_another_host_is_blocked`
- `tests/test_regression_playwright.py::test_a_failure_before_the_fix_in_a_run_that_reached_out_is_not_the_bug_showing`
- `tests/test_regression_playwright_refusals.py::test_a_target_that_is_not_this_machine_is_refused_before_anything_starts`
- `tests/test_regression_playwright_refusals.py::test_a_webserver_or_a_setup_project_pointed_elsewhere_is_refused`
- `tests/test_regression_playwright_refusals.py::test_a_config_that_starts_no_site_here_is_refused`
- `tests/test_regression_playwright_refusals.py::test_a_production_credential_in_what_the_run_inherits_is_refused_before_playwright_starts`
- `tests/test_regression_playwright_refusals.py::test_a_site_whose_local_stack_is_not_running_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_a_server_already_answering_at_the_site_s_address_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_no_playwright_installed_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_a_fix_with_no_spec_never_starts_playwright`
- `tests/test_regression_playwright_refusals.py::test_a_fix_whose_only_tests_use_jest_globals_is_not_held_up_by_the_browser_path`
- `tests/test_regression_playwright_refusals.py::test_each_config_shape_loads_through_the_wrapper`
- `tests/test_local_only.py::test_what_the_job_s_own_local_stack_holds_is_local`
- `tests/test_local_only.py::test_what_reaches_somewhere_else_is_refused_even_when_declared_local`

## Gotchas

- A fix is a title whose conventional-commit type is `fix`. A fix that cannot carry a test says why on a body line `regression-test: none -- <reason>`; the reason is printed as a warning and the pass is qualified.
- A test that fails before the fix only because a module, export, file or script it needs does not exist yet is a finding (exit 1), not exit 2: the gate ran, and the author can act on it (test through code that exists before the fix, or write the reason line). As exit 2 it would also break every soak, which never masks exit 2.
- It runs Vitest, node:test, pytest and unittest. A test that calls `describe`, `it` or `test` without importing a framework is Vitest's only when its package runs Vitest (the nearest Vitest config, Jest config or `package.json` naming its runner says which); a Jest or bun test is not judged, naming the runner and the file that says so, and a fix whose only tests are Jest or bun tests passes qualified with each file as an advisory, never exit 2. A Playwright spec runs only under a config `--playwright` names. Any other Playwright, shell or SQL test, a node:test file the repository does not run with `node --test` (no package.json script selects it and no `--node-test` declares it), and a script judged only by its exit code, is reported as not judged; a fix whose only tests are such files is exit 2.
- A node:test file runs alone, under Node's TAP reporter, with the script's own options less the reporter, `--watch`, sharding, coverage and isolation, which the gate sets. A file that does not load is reported by Node only as its process's stderr, which TAP keeps and JUnit drops: the gate reads the error from there. Tested on Node 20, 22 and 24.
- Shell tests stay not judged: neither a plain `*.test.sh` script nor bats reports anything but wording that separates a failed check from a file or command that does not exist yet ([why](../GATE-CONTRACT.md#regression-test-fails-on-base)).
- It swaps the files the fix changed back to the merge base in the working tree for the second run and always puts them back, so they must not differ from HEAD when it starts.
- A file whose own top-level code fails an assertion on the code before the fix, before any of its tests runs, shows the bug like a failing test; any other error that stops a file there means it did not load.
- A failure counts unless its error says something is missing, so a test whose own mock lacks a method the old code calls counts as reproducing; and a test that flakes at the head is exit 2.
- A browser run is one attempt (`--retries=0`): the run before the fix is expected to fail, and a retry there costs a whole test timeout. A spec that flakes at the head is exit 2; one that flakes on the code before the fix reads as the bug showing.
- The proxy blocks the browser, the `request` fixture, and Node's own `fetch` and `http` (and libraries that read `HTTP_PROXY`) in the site's server, the specs and a global setup. A raw TCP client, such as a database driver, is not proxied: the refusal of credentials and database hosts for anywhere else is what keeps it from reaching outside the job. The committed tests load a `.ts`, `.mts`, `.js`, `.mjs` and `.cjs` config through the wrapper, in CommonJS and ES module packages, on the catalog's pinned Playwright; Playwright 1.59 was checked by hand. The runs, and the block, are tested with Chromium.
- A spec's failure that comes from the site's own server (a 500 the code before the fix throws) counts as the bug showing, like any failure that does not say something is missing.
