# Feature map

One file per feature of this catalog: its gates, its harnesses, its command-line subcommands and its runner-lane kit. The format and the rules for creating, updating and retiring a feature are in [docs/feature-map.md](../feature-map.md). The `feature-map` line in `.vibe-verifier` fails a pull request that adds a gate or a subcommand no feature lists, lists one that no longer exists, or leaves a source glob or a test anchor dangling.

| Feature | Gates and subcommands |
|---|---|
| [Running the subscribed gates](runner.md) | `run`, `list` |
| [Inventory and pin propagation](consumers.md) | `consumers`, `apply-down` |
| [Secret scanning](secrets.md) | `gitleaks` |
| [Workflow lint and audit](workflow-lint.md) | `actionlint`, `zizmor` |
| [New source has a test](test-presence.md) | `new-source-has-test` |
| [Changed-code mutation](mutation.md) | `changed-code-mutation` |
| [Complexity and file-length ratchets](ratchets.md) | `cognitive-complexity`, `max-file-lines` |
| [Repository rules and the generated rules block](repo-rules.md) | `repo-rules`, `rules-doc` |
| [Universal checks](universal-checks.md) | `universal-checks` |
| [package.json hygiene](package-json.md) | `no-duplicate-package-json-keys`, `build-tools-in-devdependencies` |
| [Branch name length](branch-name.md) | `branch-name-length` |
| [Feature map drift](feature-map.md) | `feature-map` |
| [Browser acceptance verification (QAE)](qae-harness.md) | `acceptance-verdict`, `qae-artifacts`, `criteria`, `features`, `qae-inputs`, `qa-review`, `tool` |
| [Revision-bound AI review](review-harness.md) | `review-receipt` |
| [Pull request sync](pull-sync.md) | none: `actions/pull-sync` is an action a workflow runs |
| [Self-hosted runner lanes](runner-lanes.md) | none: `lanes/bin/*` are scripts a machine's operator and its timers run |
