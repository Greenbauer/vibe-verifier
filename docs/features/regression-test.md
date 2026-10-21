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
- source: `gates/_local_guard.py`
- source: `gates/_regression_sealed.py`
- source: `tools/regression-browser/**`

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

Each Vitest run, node:test file and Python run is guarded ([the rules](../GATE-CONTRACT.md#test-runs-reach-only-this-machine)): the gate's step holds no credential for anywhere else, and neither does a `.env` file in the repository or a file a node:test script loads with `--env-file`, or the run is exit 2 before it starts (`--local-env NAME`, repeatable, declares a secret of the job's own local stack); a connection to anything but this machine is refused, and a changed test that fails in a run that refused one is exit 2.

### Browser specs (`--playwright`)

`--playwright CONFIG` (repeatable) names a Playwright config the gate runs a fix's browser specs with; `--stack-network NAME` (repeatable) names the Docker network the job's local stack runs on; `--local-env NAME` (repeatable) declares a variable named like a credential that holds a secret of the job's own local stack; `--browser-image IMAGE` names the image to run in when the project's Playwright version has no official one:

```text
regression-test-fails-on-base --soak --title pr-inputs/title.txt --body pr-inputs/body.md --playwright playwright.config.ts --stack-network supabase_network_app --tests 'e2e/**' --timeout 1200
```

A changed spec that one of those configs collects (`playwright test --list`) runs under it, even when `--tests` also names it, so `--tests 'e2e/**'` keeps a spec's helpers at the head for the run before the fix while the spec itself runs. The site under test is the one the config's own `webServer` starts from the working tree as the gate left it, so the first run tests the head and the second the code before the fix. Production is never a target, and neither is a preview: a preview serves the pull request's head, never the code before the fix.

**The seal.** Every Playwright call (the listing, the run at the head, the run before the fix) runs in a container of Playwright's own image for the project's version (`mcr.microsoft.com/playwright:v<version>-noble`), on a Docker network the gate creates `--internal`, with no address on its bridge. A container there has a route to the network's other containers and to nothing else: not the internet, not a cloud metadata address, and, for the run's container, which holds no capability, not this machine. Docker's DNS answers it no outside name. So the kernel refuses every other destination, whatever tries it: the browser (any engine, WebRTC included), the site's server, a spec, a global setup, a process any of them starts, a spec that sets its own proxy. The job's stack is moved onto the network at the first browser run (a listing starts no site, so it does not wait for that): each container of `--stack-network` leaves its networks, joins the sealed one under the same names, and is restarted, so nothing in it keeps an address from before (Supabase's API gateway does); after the runs each is moved back and restarted again. A container on an internal network publishes no port, so while the stack is sealed a relay, a container of the gate's own on an ordinary network that runs only a forwarder to the stack, forwards no IP packet and holds no capability, publishes the same ports on this machine again: the gate's own runs of the same fix on this machine still reach the stack (tested with node:test and Python). In the run's container each port the stack publishes on this machine is forwarded from the same loopback address, so `http://127.0.0.1:54321` still reaches Supabase's API, and the site's settings work unchanged.

**What the network refused** is recorded: every outside name a process looked up, and the kernel's count of IPv4 packets that had no route (an address given as numbers). A changed spec that fails in a run whose network refused anything is exit 2, whichever request it was (the spec's own, a page's script, an in-page fetch, the site's server-side fetch): its failure cannot be told from the refusal, and a request made only before the fix would otherwise read as the bug showing. A spec that passes all the same is judged as usual.

**Before anything runs**, the gate refuses (exit 2) a credential for anywhere else in what the run inherits: the gate's environment and every `.env` file a framework would load anywhere in the repository but `node_modules` and hidden directories ([the rules](../GATE-CONTRACT.md#regression-test-fails-on-base)). From the sealed listing, before any server, global setup or browser starts, it refuses a project that runs a changed spec, or one it depends on, whose `baseURL` is not set or is not loopback (localhost, a `*.localhost` name, 127.0.0.0/8, ::1), a `webServer` url that is not loopback, a config with no `webServer`, and a credential in a `webServer`'s `env`. A fix whose changed tests all import Vitest, `@jest/globals` or `bun:test`, or that node:test runs, never starts Playwright (one that imports another runner, such as mocha, is listed, and not collected); a test that only calls `test()` or `describe()` with no import (a Vitest or Jest file with globals, or a spec that imports its `test` from a fixtures module) is listed, and when no changed test imports `@playwright/test` and that listing cannot run (a credential in the step, no Playwright, no Docker), they are classified as before (a credential in the step then stops the unit run of a Vitest file the same way, exit 2; a Jest file is not run).

What the repository provides:

- **A runner with Docker**, and the gate's step not in a job `container:` (the gate mounts the job's own paths into its containers). GitHub's hosted Ubuntu runners have Docker, and the catalog's self-hosted lanes give each job its own Docker daemon ([lanes](../../lanes/README.md)).
- **A config whose `webServer` serves the working tree**: a dev server (`next dev`), or a build and a start in one command, on a loopback address, with every project's `baseURL` there. It runs inside Playwright's image, which has Node (24 in 1.64.1's), npm and yarn but not pnpm, so the command calls the framework's own binary (`node_modules/.bin/next dev -p 3000`), npm or yarn. A server that only starts a build made earlier (`next start` alone) serves the head in both runs, and the spec then reads as not reproducing the bug. A repository whose main config targets a preview can name a second config for the gate that imports the first and adds the `webServer`.
- **The job's own local stack**, started before the gate on a Docker network, named with `--stack-network` (the Supabase CLI's is `supabase_network_<project_id>`): the database with its migrations and the test accounts the specs sign in as (`supabase start`, then `supabase db reset` with a seed), and the site's environment pointing at its ports on 127.0.0.1. `supabase status -o env --override-name api.url=NEXT_PUBLIC_SUPABASE_URL` is the Supabase CLI's own example for writing those variables; its keys say `iss: supabase-demo`, which the gate reads as local. The gate restarts the stack twice (into the seal at the first browser run, out of it after the runs), so it runs after the job's other steps that use it.
- **The project's packages installed**, with a Playwright version that has an official image (every release has one; an alpha or a fork names an image with `--browser-image`). No browser install on the runner: the image holds the browsers.
- A site that **reaches no other host while its specs run** (turn analytics, error reporting, remote scripts and a framework's own start-up requests such as telemetry off in the gate's config, or a failure cannot be judged), and **no site build that fetches from the internet** (`next/font/google` downloads its fonts when the site compiles, and the seal refuses it like any other host: self-host the fonts, or mock them in the gate's config).
- **A gate step whose environment holds nothing but that stack's values**: no production or preview key, token, password or connection string at the step or the job, and none in a `.env` file the site loads. A local secret the stack needs that is named like a credential (`SESSION_SECRET=local-only`) is declared with `--local-env SESSION_SECRET`; its value is still refused if it is a hosted project's JWT, a webhook, a connection string or address for another host, or a token whose shape names its service (a GitHub, Slack, npm or Supabase access token, a Stripe live key or an AWS access key id).
- **Specs that reach the site by its `baseURL`**.
- **A job of its own that is not a required check**, with `--soak`, so no required check ever waits on it.

What it costs each fix pull request whose changed tests include a spec, measured (the gate prints each part's time on its own line):

| Part | Measured |
| --- | --- |
| Pulling Playwright's image, once per machine that keeps it | 2.5 GB (amd64): 25 to 38 s on a GitHub-hosted Ubuntu runner (Docker 28.0.4, eight runs); 960 MB (arm64): 44 s on Docker Desktop, Apple M4 Pro |
| Moving the stack in and restarting it, then out and restarting it | a Supabase stack of five containers (database, auth, REST, API gateway, mail catcher), its three ports relayed: 33 s in and 33 s out on a GitHub-hosted runner (CI run 39390207639); 30 s in and 22 s out on Docker Desktop, Apple M4 Pro, before the relay; a one-container stand-in: about 1 s each way |
| Each Playwright call in the seal (container start plus the work) | on the catalog's fixture (a node server, one spec), GitHub-hosted runner, across eight runs: a listing about 0.5 to 2 s, a run about 1 to 2.5 s passing and 4 to 5.5 s failing on a 3 s timeout; the whole gate about 2 to 14 s |
| What the job already pays for this gate's runs: the stack's start, the site's start or build twice, the specs twice | `supabase start` 36 to 41 s and `supabase db reset` 33 to 35 s on a subscriber's self-hosted lane, whose images are preloaded (12 runs, 4 repositories); the five-container stack above, pulling its images: 128 s on the GitHub-hosted runner; a Next.js build 71 to 86 s each |

So the gate adds about a minute of its own (66 s of stack moves for Supabase on a hosted runner) plus the image pull where it is not cached, on top of the site's two starts and the specs. It runs in a job no required check waits on. The job runs on every pull request unless its workflow starts the stack only for a title of type `fix`.

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
- `tests/test_regression_playwright_network.py::test_the_listener_hears_a_container_that_is_not_sealed`
- `tests/test_regression_playwright_network.py::test_a_raw_node_socket_from_the_site_is_refused_and_counted`
- `tests/test_regression_playwright_network.py::test_a_different_node_first_on_path_with_an_empty_environment_is_refused`
- `tests/test_regression_playwright_network.py::test_this_machine_is_off_the_network_too`
- `tests/test_regression_playwright_network.py::test_a_page_s_webrtc_sends_nothing_off_the_network`
- `tests/test_regression_playwright_network.py::test_a_cloud_metadata_address_is_refused`
- `tests/test_regression_playwright_network.py::test_a_spec_s_own_proxy_is_refused`
- `tests/test_regression_playwright_stack.py::test_the_job_s_stack_is_reachable_from_the_site_cannot_reach_out_and_is_put_back`
- `tests/test_regression_playwright_stack.py::test_the_gate_s_other_tests_reach_the_stack_while_it_is_sealed`
- `tests/test_regression_playwright_stack.py::test_a_python_test_of_the_same_fix_reaches_the_sealed_stack_too`
- `tests/test_regression_playwright_stack.py::test_a_run_that_cannot_be_judged_still_puts_the_stack_back`
- `tests/test_regression_playwright_stack.py::test_the_relay_forwards_no_packet_and_holds_no_privilege`
- `tests/test_regression_playwright_refusals.py::test_a_fix_whose_only_tests_use_vitest_globals_is_judged_as_before`
- `tests/test_regression_playwright_refusals.py::test_a_target_that_is_not_this_machine_is_refused_before_anything_starts`
- `tests/test_regression_playwright_refusals.py::test_a_webserver_or_a_setup_project_pointed_elsewhere_is_refused`
- `tests/test_regression_playwright_refusals.py::test_a_project_with_no_baseurl_is_refused`
- `tests/test_regression_playwright_refusals.py::test_a_config_that_starts_no_site_here_is_refused`
- `tests/test_regression_playwright_refusals.py::test_a_production_credential_in_what_the_run_inherits_is_refused_before_playwright_starts`
- `tests/test_regression_playwright_refusals.py::test_a_site_whose_local_stack_is_not_running_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_a_stack_network_with_no_containers_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_no_playwright_installed_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_no_docker_cannot_be_judged`
- `tests/test_regression_playwright_refusals.py::test_the_run_s_image_is_playwright_s_own_for_the_project_s_version`
- `tests/test_regression_playwright_refusals.py::test_the_relay_publishes_each_port_where_the_stack_did`
- `tests/test_regression_playwright_refusals.py::test_a_fix_with_no_spec_never_starts_playwright`
- `tests/test_regression_playwright_refusals.py::test_a_fix_whose_only_tests_use_jest_globals_is_not_held_up_by_the_browser_path`
- `tests/test_regression_playwright_refusals.py::test_each_config_shape_loads_through_the_wrapper`
- `tests/test_local_only.py::test_what_the_job_s_own_local_stack_holds_is_local`
- `tests/test_local_only.py::test_what_reaches_somewhere_else_is_refused_even_when_declared_local`
- `tests/test_local_only.py::test_a_token_that_says_what_it_is_is_refused_under_any_name_and_never_printed`
- `tests/test_local_guard.py::test_node_refuses_every_other_address_and_name_before_anything_is_sent_and_records_each`
- `tests/test_local_guard.py::test_node_reaches_this_machine`
- `tests/test_local_guard.py::test_python_refuses_every_other_address_and_records_each`
- `tests/test_local_guard.py::test_python_reaches_this_machine_and_the_interpreters_own_sitecustomize_still_runs`
- `tests/test_local_guard.py::test_a_guard_inside_a_guarded_run_records_in_its_own_run`
- `tests/test_local_guard.py::test_a_credential_for_anywhere_else_is_refused_before_anything_starts_and_never_printed`
- `tests/test_local_guard.py::test_a_failure_in_a_run_that_refused_anything_cannot_be_judged`
- `tests/test_local_guard_gates.py::test_a_run_that_reaches_only_this_machine_is_judged_as_before`
- `tests/test_local_guard_gates.py::test_a_failure_in_a_run_that_reached_out_cannot_be_judged`
- `tests/test_local_guard_gates.py::test_a_credential_for_anywhere_else_stops_each_runner_before_it_starts`
- `tests/test_local_guard_gates.py::test_a_file_or_an_assignment_a_node_test_script_gives_its_process_is_checked_too`

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
- The seal is the kernel's: a run's containers have no route off their network, so nothing in them is trusted to cooperate (no proxy setting, no preload, no browser flag). What it records is for judging a failure, not for keeping anything in: an IPv6 address given as numbers is refused but not counted (Chromium's own IPv6 check counts there on every run), and so is whatever the stack's own containers try off the network (their raw traffic to this machine is the limit below). The committed tests run Chromium; a Firefox or WebKit project runs in the same image and network, untested here. The committed tests load a `.ts`, `.mts`, `.js`, `.mjs` and `.cjs` config through the wrapper, in CommonJS and ES module packages; Playwright 1.59 was checked by hand. Those that need Docker pull Playwright's image, so they run in the catalog's `Regression browser tests` check (job `regression-browser`) and skip without `VV_REGRESSION_SEALED`; the required `Unit tests` check (job `test`) runs only the refusals made before anything runs, which need no Docker.
- The stack's own containers keep Docker's default capabilities, and this machine keeps an interface on the sealed network (with no IPv4 address). So a root process in a stack container could send raw traffic to this machine itself, and what this machine then does with it is not tested here. The run's container, where the pull request's specs and site run, holds no capability and cannot.
- The stack's containers are restarted in the seal and again after it. A container whose state lives only in memory loses it; one that is not healthy again within the gate's `--timeout` is exit 2.
- A spec's failure that comes from the site's own server (a 500 the code before the fix throws) counts as the bug showing, like any failure that does not say something is missing.
- The unit runs' guard is a preload, not a seal: it refuses what Node's `net` module and Python's `socket` module connect to, in every process that keeps `NODE_OPTIONS` and `PYTHONPATH`. It does not refuse a Unix socket or named pipe (which a proxy can forward anywhere), curl, psql or another program that is neither, a database driver that opens its socket in native code (libpq under psycopg, Prisma's engine), a process started with an environment of its own or `python -I`, UDP from Node's `dgram` or a Python `sendto`, or a name lookup, whose query reaches the resolver ([the list](../GATE-CONTRACT.md#test-runs-reach-only-this-machine)). The step holding no credential for anywhere else, which the gate checks, is what covers those.
- A test that installs something (`npm ci`, `pip install`) has it refused: give it npm's cache, or a wheel directory with `PIP_NO_INDEX=1` and `PIP_FIND_LINKS`, as this repository's `ci.yml` does for its own pilot.
