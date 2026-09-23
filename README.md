# Vibe Verifier

Vibe Verifier helps you assess whether vibe-coded changes are ready to merge.
You can use it on any pull request.

## What it checks

Each check is called a **gate**. You choose which ones run.

| Check | What it catches |
|---|---|
| `gitleaks` | Secrets added in a pull request's commits |
| `new-source-has-test` | New source files without a matching test filename or relative import from a test |
| `actionlint` | Errors in changed GitHub Actions workflows |
| `zizmor` | Security risks in changed workflows, such as unpinned actions and excessive permissions |
| `cognitive-complexity` | New files with functions over the complexity limit, or changed files with more of them |
| `max-file-lines` | New files over the line limit, or existing files over it that grew |
| `no-duplicate-package-json-keys` | Duplicate keys or invalid JSON in `package.json` |
| `build-tools-in-devdependencies` | Known development packages listed as runtime dependencies |
| `branch-name-length` | Branch names longer than your configured limit |

The source-file checks focus on JavaScript and TypeScript by default. The test-file
check looks for a matching test filename or relative import; it does not run tests
or measure coverage. Keep your existing build and test suite.

## Get started

You need Git and Python 3. Some checks download tools on first use; automatic
downloads support macOS (Intel or Apple silicon) and Linux x86-64.
For the complexity check, use Node.js 24 and npm.

1. Create a `.vibe-verifier` file at the root of your project, with one check per line.
   For example, a JavaScript or TypeScript project could start with:

   ```text
   gitleaks
   no-duplicate-package-json-keys
   new-source-has-test
   max-file-lines --max 500
   ```

2. In your project, check out the branch you want to verify and commit the changes
   you want checked. The checks use Git history, so uncommitted edits are not fully
   checked. Make sure the branch you plan to merge into is available locally.

   Clone Vibe Verifier separately and run it against your project.
   Replace `/path/to/your/project` with its location and `main` with the branch
   you plan to merge into:

   ```bash
   git clone https://github.com/Greenbauer/vibe-verifier.git
   cd vibe-verifier
   bin/vibe-verifier run \
     --repo "/path/to/your/project" \
     --manifest "/path/to/your/project/.vibe-verifier" \
     --base-ref main
   ```

   Results show which checks passed, found problems, or could not run.
   When first adding a check, put `--soak` on its line to report problems without
   blocking on them. Errors that prevent a check from running still fail.
   An existing check keeps the settings from the base branch until a settings
   change is merged.

3. To run on GitHub pull requests, copy
   [`consumer/vibe-verifier.yml`](consumer/vibe-verifier.yml) into your project's
   `.github/workflows/vibe-verifier.yml`. Replace the all-zero placeholder on its
   last line with the full commit SHA from `git rev-parse HEAD` in your Vibe Verifier clone.
   Commit the workflow and `.vibe-verifier` together, then make `vibe-verifier-ok`
   a required check in your branch rules if it should block merging.

See the [configuration guide](docs/GATE-CONTRACT.md) for check options, comparison
branches, exit codes, and GitHub setup details.

## Optional AI checks

- **[Code review](harnesses/review/README.md):** Reviews a pull request against your
  repository's `CLAUDE.md` rules. The check requires a completed review of the current
  commit and no unresolved review threads.
- **[Browser testing](harnesses/qae/README.md):** Uses AI to try the behavior described
  in the pull request's acceptance criteria, saving screenshots and logs for review.
  Checks flag missing evidence, recorded console errors, and failed requests to your
  app outside your configured exceptions. The explorer runs on Claude by default, or on Codex through a self-hosted runner that holds a ChatGPT login (`harnesses/qae/explore-codex.yml`).

These workflows need Claude authentication; browser testing also needs an app it can
start or reach. The regular checks need no AI account. CI and AI usage may incur
charges under your providers' plans.

## Keep checks up to date

Run `git fetch origin` in your Vibe Verifier clone before checking for updates.
For multiple projects, see [how to check versions and open update pull requests](docs/GATE-CONTRACT.md#subscribing-in-ci).
An organization can instead subscribe every repository through one CI repository of its own, which
its rulesets require on each pull request; see [wrappers](docs/GATE-CONTRACT.md#wrappers).

## Contribute

Read [how to add a gate](docs/GATE-CONTRACT.md#adding-a-gate) and run the existing tests:

```bash
python3 -m unittest discover -s tests
```

For vulnerability reports, see [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
