# QAE harness: an explorer that produces evidence, and a gate that decides

The LLM explores and produces artifacts. Deterministic code decides pass or fail. Its prose is
never the verdict.

Two consumers run it today: the pilot, a Next.js site started on the runner (first real run,
2026-09-18: 33 model turns, one step log and one screenshot per criterion, page snapshots and
console logs saved, a verdict comment whose anchors the gate resolved, `acceptance-verdict PASS`),
and a second site rendered from a WordPress API on the runner (since 2026-09-21; first complete run
28 turns, 3 criteria, both gates green).

## The shape

Two jobs on every pull request, in the consumer's own workflow. [`explore.yml`](explore.yml) is
the template; the consumer owns `runs-on` and how the site is built and started (or which
[reachable preview](#a-reachable-preview-instead-of-a-site-on-the-runner) is used instead), and that
step declares the site's URL as its `url` output. For a site behind a login, it writes
`qae-inputs/site.md` in that same step to tell the explorer how to sign in and what state the site
starts in (a throwaway account on a throwaway backend, never production). Its three catalog
pins (`criteria@` and `qae-browser@` in explore, `gates@` in verify) are inventoried and bumped by
`consumers` and `apply-down` exactly like the stub's.

1. **explore** builds and starts the PR's site on the runner (or resolves its preview), reads the PR body with
   `actions/criteria` and, only when it lists a criterion, runs `claude-code-action` with
   [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) as its browser. The prompt
   ([`prompt.md`](prompt.md)) tells the model to walk each criterion under the PR body's
   `## Acceptance criteria` heading, append one line per action to `qae-artifacts/qae/ACn.md`
   (`- step k: <what you did> -> <what you saw>`), save a screenshot per step, then write
   `qae-artifacts/verdict.md` with one `acceptance-check: ACn -- PASS|FAIL -- ... (qae/ACn.md::<step line>)`
   per criterion and post it as a PR comment. A step after the model fails the job if anything
   outside `qae-artifacts/` and `qae-inputs/` changed on the runner; the config paths
   `claude-code-action` itself resets to the base branch before the model runs (`CLAUDE.md`,
   `.claude/`, `.mcp.json` and a few more) are excused only while they still match the base.
   Everything under `qae-artifacts/` is uploaded, always.
2. **verify** downloads the artifacts, writes the three declared inputs (the PR body; the newest
   `acceptance-check:` comment posted by the explore job's own identity; the site URL the explore
   job declared, in `qae-inputs/site-url`), and runs the
   [`acceptance-verdict`](../../gates/acceptance_verdict.py) gate through the composite action with
   the manifest [`manifest`](manifest) (`.vibe-verifier-qae` in the consumer).

## A reachable preview instead of a site on the runner

The site step has two shapes, and both end by writing `url=<the site>` to `$GITHUB_OUTPUT`. The
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
bypass token.

## Rules the harness obeys, each from a real run

- **Anchors resolve or the PASS is refused.** A step line the model did not write cannot be cited.
- **Only the explore job's identity may supply a verdict.** The verify job filters comments by
  the login `gh pr comment` posts as with the workflow token (`github-actions[bot]`). Without that
  filter any commenter could paste a PASS anchored to a real step line. Found by review on the
  pilot PR.
- **The write scope is enforced, not requested.** "Write only under qae-artifacts/" in the prompt is
  a suggestion; the scope step is the rule. Found by review on the pilot PR. The action's own
  reset of `CLAUDE.md` and friends to the base branch is not a write, and a PR that changes one of
  them shows it as changed on the runner: the step compares those paths to the base instead of
  failing on sight. Found on the second consumer's first pull request.
- **Nothing to explore costs no model session.** A PR that declares `- None: <why>`, or lists no
  criteria at all, skips the explorer: the first because there is nothing for a browser to check,
  the second because the verdict gate fails it whatever the explorer does. The skip is decided by
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
  verify job, which writes `qae-inputs/site-url` for `qae-artifacts --site-file`. It is a step output
  and not a file in the artifact because the explorer writes under `qae-artifacts/`, and it must not
  choose which site the gate judges. The pilot starts the site on the runner because the repo's
  Vercel previews sit behind Vercel SSO with no automation bypass configured; a repo with a reachable
  preview resolves its URL instead ([above](#a-reachable-preview-instead-of-a-site-on-the-runner)).
- **A pull request with nothing to check says so.** The harness runs on every pull request, and the
  job carries no `if:`, because GitHub counts a skipped required check as satisfied. So a change
  with no rendered surface declares it: one criterion reading `- None: <why>`. Both gates pass on
  that, the explorer stops without writing, and a bare `- None` or an absent section still fails.
  Found the first time apply-down opened a pin-bump PR and the gate refused it, correctly.

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
  its `config.toml`. One runner is one store is one job at a time. `actions/qae-codex` checks the
  store is a ChatGPT login with a refresh token and never writes it. A repository whose pull requests
  go quiet copies [`codex-keepalive.yml`](codex-keepalive.yml) too: Codex refreshes a store that is
  about eight days old during any run, and the weekly exec keeps that happening.
- **The model holds no GitHub token either.** The explorer step is given no secret and the checkout
  has none, so the model cannot post, push, or read a credential. The workflow posts
  `qae-artifacts/verdict.md` itself after the model finishes, which is why step 4 of the prompt
  reads "do not post anything" in this lane, and why the verify job's author filter still holds.
- **Three settings a non-interactive Codex run needs**, each found on the first spike (2026-09-23,
  zack.land on a laptop): `--sandbox danger-full-access`, because under `workspace-write` Codex
  cancels the browser's navigate and run-code calls client-side while screenshots and snapshots still
  answer, so a run looks alive and never loads a page; `approval_policy = "never"`, because nobody can
  answer a prompt; and stdin closed, because `codex exec` otherwise waits on it. The sandbox is not
  the guard here: the write-scope step is, exactly as in the Claude lane, and the runner user is
  dedicated to this work.
- **What the runner needs.** Linux x64 with a dedicated unprivileged runner user, the Playwright
  system packages (`playwright install-deps chromium`, once, as root), node for `actions/setup-node`,
  and the label `qae-codex`. Register one runner per repository; a personal account has no shared
  runner pool. Keep it off any box that must stay credential-free. Pointed at a
  [reachable preview](#a-reachable-preview-instead-of-a-site-on-the-runner), the runner installs and
  builds nothing of the app: it drives the browser against the preview's URL.

## The adjudicator: the artifacts decide, not the prose

On the pilot's first run the explorer PASSed a criterion that said "loads without console errors"
while the console held a 404 for Vercel's analytics script (absent when the site runs locally),
reasoning that the error was environmental; on the second run it wrote FAIL for the same 404. That
is the model judging meaning, and not the same way twice. So the second gate in the manifest,
[`qae-artifacts`](../../gates/qae_artifacts.py), reads what playwright-mcp and the explorer saved
and refuses on structural facts:

1. every `- step k:` line in a step log has a non-empty `qae/ACn-step-k.png`;
2. no `[ERROR]` in any `console-*.log` outside `--allow-console` patterns;
3. the session log exists (`--save-session`) and shows a `browser_navigate`;
4. a `browser_network_requests` result exists (the prompt asks for one after each criterion), and
   no request to the site under test (`--site-file`, the URL the explore job declared, or a fixed
   `--site`) answered 400 or worse or failed, outside `--allow-request` patterns.

Run against the pilot's real first-run artifacts with no allowlist it found three things: the
second step of AC2 had no screenshot, the analytics 404, and no network record. The consumer
declares what is environmental on the manifest line, and nothing else:

```
qae-artifacts --artifacts qae-artifacts --site-file qae-inputs/site-url --allow-console '/_vercel/insights/script\.js' --allow-request '/_vercel/insights/script\.js'
```

A manifest copied before the site URL was declared reads `--site http://localhost:3000`, and keeps
working: `--site` judges a fixed URL, `--site-file` the declared one, and a line takes one or the other.
