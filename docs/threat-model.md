# Threat model

Vibe Verifier assumes pull-request content can be untrusted, including changes authored by software agents. Its job is not to make AI trustworthy; it is to keep the authority to issue a merge verdict outside AI-authored prose.

This document summarizes the threats the current architecture is designed to handle and the residual risks it deliberately leaves visible.

## Assets

The system protects the integrity of:

- the required GitHub merge signal,
- the verification policy that determines which gates run,
- review-receipt revision identity and the workflow context of acceptance evidence,
- workflow and action supply-chain pins,
- repository and CI credentials,
- browser/QAE evidence used to support acceptance verdicts,
- operator visibility into failures, missing evidence, and stale state.

## Trust boundaries

### Trusted for adjudication

- deterministic gate code from the pinned Vibe Verifier revision,
- Git/GitHub revision identity supplied by the workflow,
- workflow steps that create declared inputs after prerequisite steps succeed,
- GitHub-native branch/ruleset enforcement when configured,
- consumer-owned workflow configuration that explicitly declares infrastructure inputs such as the application URL.

### Untrusted or partially trusted

- pull-request source code,
- pull-request body and acceptance criteria,
- changed workflow/configuration files on the head branch,
- AI/model output,
- browser observations before deterministic validation,
- review comments not proven to come from the expected workflow identity,
- cached or stale observability data,
- provider availability and quota state.

## Threats and mitigations

| Threat | Mitigation | Residual risk |
|---|---|---|
| **PR weakens its own gate policy** | When a base manifest exists, the runner uses the union of base/head gates and the base arguments for gates present on both sides. Removing a gate, adding `--soak`, or raising a threshold does not weaken the current PR's judgment; nor does editing, deleting or renaming a `repo-rules` rule file the base has, or narrowing a feature's source globs to drop it from the QAE re-walk. | A direct consumer PR may still modify its workflow file unless GitHub-native protection or a wrapper is used. |
| **PR disables or rewrites the required workflow** | Organization wrappers can place the required workflow outside the target repository and require it through a GitHub ruleset. | Direct consumer mode depends on repository rules/merge discipline for workflow immutability. |
| **AI reviewer fails but CI goes green** | Review failure prevents a new receipt and fails the review job. The receipt gate rejects an input with no matching head SHA. | An existing receipt for the same head can still satisfy the receipt gate; require the review job too if its current failure must block. A rate or usage limit proven by every configured reviewer account is a deliberate exception: the job passes with a warning and a `limited` receipt, so that head merges without a model review unless the operator waits for the limit to reset and re-runs it. |
| **New commit lands after a successful review** | The receipt names the reviewed head SHA. A receipt for an older revision does not satisfy the current head. | A new review is required even when the new commit is trivial unless the harness can establish its scoped no-change/delta condition. |
| **Fake review receipt or QAE verdict is posted manually** | The review verify job selects comments from `github-actions[bot]`, and the review gate also checks the receipt SHA against the event head. The QAE verify job reads no comment: its verdict is the file each explorer left in the same run's evidence artifact. | The account filter does not authenticate a particular workflow or run, so another workflow using that identity is inside the review's trust boundary. The QAE verdict file is written by the explorer, a model: the gates check its anchors and the evidence, not its judgment. |
| **Prompt injection through repository files** | Review instructions such as `CLAUDE.md` and `.claude/**` are pinned to the base branch before model execution. Model tools are restricted. | The model can still misunderstand malicious source text; its output remains evidence rather than authority. |
| **Prompt injection through acceptance criteria** (the PR body's, and a ticket's the consumer supplies) | The Claude lane restricts file/comment tools. Both lanes check Git-visible changes after execution; the workflow supplies the site URL used by the verifier, and the digest of each design reference a criterion names, so the gate refuses an evidence copy the explorer replaced. | Codex runs unsandboxed. The URL scopes request checks but does not prove the explorer visited that site. Whether the built screen matches a reference is the explorer's judgment; the gate holds only that the comparison step exists against the supplied image. Direct-consumer workflow changes remain head-controlled. |
| **A dependency update's code runs beside the explorer's model token** | A consumer that lets Dependabot pull requests reach the explorer installs, builds and serves the site in a container given the tracked files read-only and no secret, and only then adds the token to the Dependabot secret store ([Dependency updates](../harnesses/qae/README.md#dependency-updates)). | The template's default site step runs on the runner, where an install script or the server can read the explorer step's environment; that is every dependency change a person pushes, too. A container shares the runner's kernel. |
| **Model modifies the repository while testing** | Claude has path-scoped file tools; both lanes fail the explore job for disallowed changes reported by Git status. | This is not filesystem isolation: ignored files and allowed input/artifact/scaffolding paths are excluded, and Codex can access the runner account's files. The separately running verify job does not itself require explore success. |
| **Model claims browser success without supporting evidence** | Acceptance verdicts require one line per criterion and anchors that resolve to repository files or the run's artifacts. Missing anchors are rejected. | Resolved evidence proves the referenced observation exists, not that every possible behavioral edge case was exercised. |
| **Explorer chooses a different application than the verifier expects** | The workflow's site step declares the URL and passes it separately to the gate as the prefix used to filter recorded requests. | The gate does not assert a visit or request to that URL. The consumer must protect the workflow and ensure the URL serves the intended revision. |
| **Protected preview secret leaks through URL/logs** | The Codex QAE path can exchange a Vercel bypass secret for a cookies-only Playwright storage state and redact cookie values from browser tool output. | An unsandboxed runner process with filesystem access may still read its own storage-state file; the raw secret must remain outside model-visible inputs. |
| **A login the explorer types leaks into evidence** | A site step hands the explorer only a NAME per credential through `actions/qae-codex`'s `secrets-file`; playwright-mcp types the value and redacts it from tool results, the action replaces every value and its JSON-escaped and URL-encoded forms in each non-image artifact file before the verdict is posted or uploaded, and `codex exec --ephemeral` keeps no session rollout on the runner. | Values in images or compressed files are not rewritten; the unsandboxed explorer can still read the value file on its runner, so a login must be a throwaway test seat. |
| **Required job is skipped and GitHub counts it as satisfied** | Verify jobs use `if: always()` to run after prerequisite failures; the gate job has no skip condition. `None: <why>` explicitly declares that no browser check is needed; otherwise only a PR with no criteria whose every changed path (both names of a rename) is CI configuration or unrendered documentation skips the gates, and the review comment states that skip. | Running verification after an explore failure does not make that failure an input to the gate. Consumers must require the producer job as well as verification when both must succeed. |
| **A gate runs the pull request's own code** | Only `changed-code-mutation` does: it runs the project's tests against the changed source, as the project's CI already does. It holds no token, reads no secret, and installs nothing for the project; the job it runs in should hold no secrets either. For Python it adds mutmut's packages to the end of the project's interpreter path and installs nothing into the project's environment. A suite that fails at the head, an unsupported Vitest, no pytest, or a run past its bounds is exit `2`. | A test can be written to notice the mutation run itself and kill every mutant. That test is in the diff, where review reads it; the score is evidence about tests the pull request controls, not proof of their quality. |
| **Gate crashes or lacks enough history/input to decide** | Exit `2` means cannot run. Aggregate verification treats it as failure and `--soak` does not mask it. | Operational failures can block merging until corrected. |
| **Action dependency changes underneath a consumer** | Canonical consumer workflows pin actions by full commit SHA; native `sha_pinning_required` and selected-action policies can further constrain execution. | Native repository settings live outside the repository tree and require separate inventory/administration. |
| **Consumer remains on stale Vibe Verifier code** | `consumers` inventories pins; confirmed `apply-down` plans open pin-bump PRs and can directly update wrapper ruleset pins. | Workflow updates still need merging; ruleset updates take effect through the separately confirmed API write. |
| **Observability reports fabricated certainty from missing data** | Dashboard data carries source timestamps and partial/stale/unknown states. Missing numeric usage is not converted to zero. | The dashboard is diagnostic; it is not a source of merge authority. |
| **A merge runs on a dashboard host without its operator** | The Linux follower deploys only a `main` commit whose GitHub check runs all passed, into a root-owned checkout the dashboard accounts cannot write. It health-checks each dashboard after a restart; when one fails it resets to the previous commit and records the failed one so it is not retried. The web service holds only an hour-long read token, minted for the configured repositories, or for every repository of the owner the installation can reach when `repositories` is `all`. | `main`'s branch protection is the real gate: a change that passes CI and the health check but misbehaves runs until the next merge, and a change to the root helpers runs as root on their next tick. |

## Revision-binding limits

The review gate compares the receipt SHA with the pull-request event's head SHA. QAE instead downloads artifacts from its workflow run and checks anchors against that checkout or those artifacts. Its verdict is a file in that same run's evidence, so a verdict from another run or another head is never read. Each job still fetches the mutable PR body separately, and when one explorer of a run is run again the others' evidence is an earlier attempt's of the same run and head.

The default QAE checkout is GitHub's pull-request merge revision. Preview consumers choose their own URL-resolution step. The gate validates the declared URL's format and uses it as a string prefix for recorded request checks; it does not attest the deployed commit, require a request to that site, or judge whether an observation proves the criterion. The consumer must establish those connections.

A workflow-issued review receipt records successful execution, a no-change refresh, or a `limited` run in which no review happened, not exhaustive semantic coverage. The review workflow currently collects only the first 100 review threads; unresolved threads beyond that page are outside its count. Comment pagination for receipts and QAE verdicts is separate and covers all returned pages.

## Direct-consumer versus wrapper trust

There are two materially different deployment shapes.

### Direct consumer

The repository contains its own `.vibe-verifier` and workflow stub. Base-manifest semantics prevent a pull request from weakening its own gate arguments, but the workflow file itself is still head-controlled on a normal `pull_request` run.

Use GitHub-native protections and operator merge discipline when that boundary matters.

### Organization wrapper

The target repository contains neither the required workflow nor its gate list. A wrapper repository supplies both, and an organization ruleset requires the wrapper workflow on the target pull request.

This is the stronger model for repositories where an agent should not be able to modify the verification mechanism that judges it.

See [Wrappers](GATE-CONTRACT.md#wrappers).

## Supply-chain assumptions

Vibe Verifier relies on the integrity of:

- GitHub Actions and the selected runner,
- the pinned Vibe Verifier commit,
- pinned third-party actions/tool versions,
- downloaded tools whose versions are controlled by the catalog,
- GitHub's identity, check-run, comment, and ruleset APIs.

The project reduces accidental drift with SHA pins, selected-action policies, catalog inventory, and explicit pin propagation. It does not defend against a compromise of GitHub itself, the maintainer account, an authorized organization administrator, or a maliciously released Vibe Verifier commit that an operator intentionally pins.

## Provider and model assumptions

AI providers are treated as fallible evidence producers. A provider can time out, hit quota, stop early, hallucinate, or misunderstand the code. The architecture responds by refusing to treat model completion text as the merge credential.

The deterministic layer can verify provenance and evidence shape, but it cannot prove that a model exhaustively found every defect or that a browser exploration covered every meaningful behavior.

## Secrets

Pure gates require no secrets or network access. Wired/agent harnesses should receive only the credentials they need for the current job and should keep those credentials out of model-readable inputs whenever possible.

Repository owners remain responsible for:

- runner isolation,
- secret scope and rotation,
- provider authentication,
- protected-preview configuration,
- GitHub organization/repository permissions.

The pull sync action is the one catalog component that needs a token able to write a pull request's branch. It cannot be the workflow's `GITHUB_TOKEN` (an update made with it starts no workflow), so it is a GitHub App's or a bot's, and it should be scoped to the repositories it syncs, with write access to contents and pull requests. No model sees it: the engine is deterministic code that sends two kinds of write, GitHub's "update branch" and one label. Its identity becomes the actor of the workflows each update starts, so a workflow that refuses bot actors has to name it.

Report vulnerabilities through the process in [`SECURITY.md`](../SECURITY.md).

## Out of scope

The threat model does not attempt to defend against:

- a malicious maintainer deliberately changing and merging trusted Vibe Verifier code,
- compromise of GitHub or the selected CI runner host,
- compromise of credentials already available to an authorized job,
- defects in third-party tools that produce incorrect findings,
- complete semantic correctness of model review or browser exploration,
- application vulnerabilities outside the checks and acceptance criteria that were actually run.

The intended guarantee is narrower: **the configured deterministic contracts must pass on the supplied inputs before their checks report success.** Exact-head review receipts, account-filtered comments, same-run QAE artifacts, and consumer-declared application URLs have different provenance strengths. Required-job configuration and the limits above determine how those checks protect merging.
