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
the template; the consumer owns `runs-on` and how the site is built and started. Its three catalog
pins (`criteria@` and `qae-browser@` in explore, `gates@` in verify) are inventoried and bumped by
`consumers` and `apply-down` exactly like the stub's.

1. **explore** builds and starts the PR's site on the runner, reads the PR body with
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
2. **verify** downloads the artifacts, writes the two declared inputs (the PR body; the newest
   `acceptance-check:` comment posted by the explore job's own identity), and runs the
   [`acceptance-verdict`](../../gates/acceptance_verdict.py) gate through the composite action with
   the manifest [`manifest`](manifest) (`.vibe-verifier-qae` in the consumer).

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
- **Declared inputs, never operated infrastructure.** The harness needs a URL it can reach. The
  pilot starts the site on the runner because the repo's Vercel previews sit behind Vercel SSO with
  no automation bypass configured; a repo with a reachable preview passes its URL in instead.
- **A pull request with nothing to check says so.** The harness runs on every pull request, and the
  job carries no `if:`, because GitHub counts a skipped required check as satisfied. So a change
  with no rendered surface declares it: one criterion reading `- None: <why>`. Both gates pass on
  that, the explorer stops without writing, and a bare `- None` or an absent section still fails.
  Found the first time apply-down opened a pin-bump PR and the gate refused it, correctly.

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
   no request to `--site` answered 400 or worse or failed, outside `--allow-request` patterns.

Run against the pilot's real first-run artifacts with no allowlist it found three things: the
second step of AC2 had no screenshot, the analytics 404, and no network record. The consumer
declares what is environmental on the manifest line, and nothing else:

```
qae-artifacts --artifacts qae-artifacts --site http://localhost:3000 --allow-console '/_vercel/insights/script\.js' --allow-request '/_vercel/insights/script\.js'
```
