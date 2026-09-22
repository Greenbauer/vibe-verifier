# Security

Vibe Verifier is a catalog of CI gates and harness workflows that other repositories pin by commit.
A vulnerability here is anything that lets a pull request pass a gate it should fail, lets a gate or
harness run code it did not pin, or leaks a token through a workflow the catalog ships.

## Reporting

Report privately through GitHub: the **Security** tab of this repository, then **Report a
vulnerability**. Do not open a public issue or pull request for a security problem. Give the gate or
workflow file, the commit you tested, and the steps that reproduce it.

This project has one maintainer and no response-time guarantee. Reports get an acknowledgement, a
fix on `main` when confirmed, and a note in the pull request that lands it. Consumers pick the fix up
by bumping their pins (`bin/vibe-verifier apply-down`).

## Supported versions

Only `main`. Consumers pin a commit; a fix is a new commit to pin.
