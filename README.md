# Vibe Verifier

Catch problems in any pull request before merging.

Vibe Verifier checks for leaked secrets, missing tests, risky GitHub Actions workflows,
and code that is getting harder to maintain. Choose the checks your project needs and
run them locally or in CI. Optional AI workflows review code and test your app in a browser.

## What it checks

Each check is called a **gate**. You choose which ones run.

| Check | What it catches |
|---|---|
| `gitleaks` | Secrets added in a pull request's commits |
| `new-source-has-test` | New source files without a matching test file or test import |
| `actionlint` | Errors in changed GitHub Actions workflows |
| `zizmor` | Security risks in changed workflows, such as unpinned actions and excessive permissions |
| `cognitive-complexity` | Changed files with more functions over the complexity limit |
| `max-file-lines` | New files over the line limit, or existing files over it that grew |
| `no-duplicate-package-json-keys` | Duplicate keys in `package.json` |
| `build-tools-in-devdependencies` | Build tools listed as runtime dependencies |
| `branch-name-length` | Branch names longer than your configured limit |

The source-file checks focus on JavaScript and TypeScript by default. The test-file
check looks for a naming or import match; it does not run tests or measure coverage.
Keep your existing build and test suite.

## Get started

You need Git and Python 3. Some checks download pinned tools on first use;
the complexity check also needs Node.js and npm.

1. Create a `.vibe-verifier` file at the root of your project, with one check per line.
   For example, a JavaScript or TypeScript project could start with:

   ```text
   gitleaks
   no-duplicate-package-json-keys
   new-source-has-test
   max-file-lines --max 500
   ```

2. Clone this repository and run the checks against your project.
   Replace `/path/to/your/project` with its location:

   ```bash
   git clone https://github.com/Greenbauer/vibe-verifier.git
   cd vibe-verifier
   bin/vibe-verifier run --repo /path/to/your/project --manifest /path/to/your/project/.vibe-verifier
   ```

   Results show which checks passed, found problems, or could not run.
   Add `--soak` to a check's line to try it without failing the run on findings.
   Errors that prevent a check from running still fail.

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
- **[Browser testing](harnesses/qae/README.md):** Walks the pull request's acceptance
  criteria in a real browser and saves screenshots and logs. Checks flag missing
  evidence, console errors, and failed requests outside your configured exceptions.

These workflows need Claude authentication; browser testing also needs an app it can
start or reach. The regular gates need no AI account. Runner and AI usage are subject
to your providers' plans.

## Keep checks up to date

For multiple repositories, `bin/vibe-verifier consumers --owner OWNER` reports outdated
pins and configuration differences using an admin `gh` login.
`bin/vibe-verifier apply-down --owner OWNER` previews version updates; after confirmation,
it opens pull requests for stale pins. It does not merge them.

## Contribute

Read [how to add a gate](docs/GATE-CONTRACT.md#adding-a-gate) and run the existing tests:

```bash
python3 -m unittest discover -s tests
```

For vulnerability reports, see [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
