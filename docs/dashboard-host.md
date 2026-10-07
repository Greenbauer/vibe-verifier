# Run a dashboard on a Linux host

This runbook runs one or more [dashboards](dashboard.md) on an Ubuntu 24.04 amd64 host under systemd,
with everything they need coming from this repository: the web service, a read-token refresh, a local
telemetry feed, and a follower that moves the host to newly merged code. The unit templates are in
[`dashboard/systemd/`](../dashboard/systemd/). Each dashboard is a systemd instance `NAME` of at most
17 characters, so its account name `vibe-dashboard-NAME` fits `useradd`'s limit of 32.

On a machine that runs [runner lanes](../lanes/README.md#dashboards), `lanes/bin/provision-dashboards.sh`
performs this runbook from the machine's host file, with a loopback guard of its own, and the
follower below then keeps the dashboards' checkout current.

## What runs

| Unit | Runs as | When | Writes |
|---|---|---|---|
| `vibe-dashboard@NAME.service` | `vibe-dashboard-NAME` | always, on `127.0.0.1:PORT` | nothing: no writable path in its sandbox |
| `vibe-dashboard-token@NAME.timer` | root | every 10 minutes | `/var/lib/vibe-dashboard/NAME/gh/hosts.yml` |
| `vibe-dashboard-telemetry@NAME.timer` | root | every 30 seconds | `/var/lib/vibe-dashboard-state/NAME/collector.json` (root only) and `/var/lib/vibe-dashboard/NAME/telemetry.json` |
| `vibe-dashboard-follow.service` | root | every 60 seconds | the checkout at `/opt/vibe-verifier` |

The web service runs with an empty capability set, no new privileges, a read-only file system, private
`/tmp` and devices, IPv4, IPv6 and Unix sockets only, 512 MiB of memory, one CPU and 256 tasks, from an
`env -i` environment. The task limit leaves room for a refresh: it runs four `gh` calls at once, each
with about fifteen threads, beside the web server's own. At 64 the service ran out, and a page request
that arrived during a refresh got no thread and failed. The root units keep the same file-system sandbox but not an empty capability set:
they read the dashboard account's 0600 files and the CI lane's, and hand files to that account. Every
unit's command line starts from `env -i` with `PYTHONNOUSERSITE=1`, so no inherited variable or user
site directory reaches it.

## Prerequisites this kit leaves to the host

These stay as the host has them configured; the kit neither creates nor checks them.

- **HTTPS route.** The dashboard binds only to loopback. A private reverse proxy, such as Tailscale
  Serve, terminates HTTPS on an address only its users can reach and forwards to `127.0.0.1:PORT` with
  the forwarded headers described under [optional private HTTPS proxy](dashboard.md#optional-private-https-proxy).
  Set `proxy_origin` in the dashboard configuration to that exact origin.
- **SSH bridge.** When the dashboard runs inside a guest and the HTTPS route lives on another machine,
  a forward-only SSH tunnel carries the route's traffic to the guest's loopback port. Restrict its key
  to that one forward, with no shell, agent or other forwarding.
- **Loopback guard.** Any local process can connect to a loopback port, and the dashboard trusts direct
  loopback reads. A packet-filter rule (for example nftables, matching the socket owner) should let only
  root, the dashboard account and the bridge account connect to `PORT`.
- **Network policy.** The private network's access rules decide which people and devices reach the HTTPS
  route. Grant the route's port to exactly the people who should see this owner's pull requests.

## 1. Accounts, directories and code

```sh
NAME=example
useradd --system --user-group --no-create-home --home-dir /var/lib/vibe-dashboard/$NAME \
  --shell /usr/sbin/nologin vibe-dashboard-$NAME
install -d -m 0755 -o root -g root /etc/vibe-dashboard/$NAME /var/lib/vibe-dashboard/$NAME/gh
install -d -m 0700 -o root -g root /var/lib/vibe-dashboard-state/$NAME
git clone https://github.com/Greenbauer/vibe-verifier.git /opt/vibe-verifier
```

Run all of it as root with the default `umask 022`. The checkout is root's and readable by everyone, so
the dashboard accounts run code they cannot change. The directories that receive a token or telemetry
belong to root and only root can write them: a directory the dashboard account could write would let it
swap a file between root writing and renaming it, and the helpers refuse such a directory.

## 2. Private configuration

| File | Owner, mode | Holds |
|---|---|---|
| `/etc/vibe-dashboard/NAME/dashboard.json` | `vibe-dashboard-NAME`, 0600 | the [dashboard configuration](dashboard.md#configuration-contract); `telemetry_file` is `/var/lib/vibe-dashboard/NAME/telemetry.json`, or absent without a CI lane on this host |
| `/etc/vibe-dashboard/NAME/host.env` | root, 0644 | `PORT`, `APP`, `INSTALLATION`, `LOGIN` (below) |
| `/etc/vibe-dashboard/NAME/app.pem` | root, 0600 | the GitHub App's private key |
| `/etc/vibe-dashboard/NAME/collector.json` | root, 0600 | the collector's [local-mode configuration](dashboard-collector.md#configuration), for this host's lane |

`host.env` takes one `KEY=value` per line, with comments only on their own lines:

```sh
# The loopback port; repeat it in the follower unit.
PORT=8765
# The GitHub App's client ID, or its numeric App ID.
APP=Iv23liExampleClient
# The App's installation on the dashboard owner's account.
INSTALLATION=12345678
# The login gh records for the token.
LOGIN=example-app[bot]
```

## 3. The GitHub App and its token

Install a GitHub App on the dashboard owner's account, for at least the configured repositories, with
these repository permissions set to read: Actions, Administration, Checks, Contents, Metadata, Pull
requests and Commit statuses. Administration is what lists the status checks classic branch protection
requires. Keep its private key only in `app.pem`.

Every ten minutes the token timer runs `bin/vibe-dashboard-host token`. `openssl` signs the App's JWT
from the key file, so the key never enters the Python process, and the installation token it exchanges
the JWT for is requested for exactly the configuration's `repositories` and exactly those seven read
permissions. The helper refuses a token whose granted permissions or repositories differ from the
request in any way, and refuses a key file another user can read. It writes the token as
`gh`'s `hosts.yml` (`oauth_token`, `user` from `LOGIN`, `git_protocol: https`), mode 0600, owned by
the dashboard account, replacing the old file in one rename. It also writes `config.yml` next to
it (`version: "1"`, mode 0644, owned by root, no token and no settings). gh 2.93 exits before any
API call when that file is missing, because it tries to create it and this directory is not writable
by the dashboard account. A token lives one hour, so ten-minute
refreshes ride out several missed runs. Because the repository list comes from the dashboard's own
configuration, which its account owns, only root and that account can widen the token, and the web
service's sandbox cannot write the file.

## 4. Telemetry

Every 30 seconds the telemetry timer runs `bin/vibe-dashboard-host telemetry`. It runs the
[collector](dashboard-collector.md) in local mode, which samples this machine in-process (no SSH, no
`sudo`) into root-only state that keeps seven days of samples. It then publishes that snapshot to the
dashboard's `telemetry_file` without the `samples` history: the dashboard never reads those samples
and refuses a telemetry file over 2 MiB, and a week of 30-second samples is about 3.8 MiB. Before the
new file replaces the old one, the dashboard's own reader must accept it; otherwise the old file stays
and the unit fails. The published file is the collector snapshot only: there is no runner-registration
inventory and no agent-runtime record. Without a CI lane on the host, leave the telemetry timer off and
`telemetry_file` out.

## 5. Install the units

```sh
install -m 0644 /opt/vibe-verifier/dashboard/systemd/* /etc/systemd/system/
# One --unit vibe-dashboard@NAME.service PORT pair per dashboard on this host:
editor /etc/systemd/system/vibe-dashboard-follow.service
systemctl daemon-reload
systemctl start vibe-dashboard-token@$NAME.service vibe-dashboard-telemetry@$NAME.service
systemctl enable --now vibe-dashboard@$NAME.service vibe-dashboard-token@$NAME.timer \
  vibe-dashboard-telemetry@$NAME.timer vibe-dashboard-follow.service
curl -fsS http://127.0.0.1:8765/api/dashboard | head -c 300
```

`bin/vibe-dashboard-host` exits 0 when it wrote its file, 1 when the telemetry sample failed (the
published file then carries the stale status), and 2 when it refused and wrote nothing. Follow it with
`journalctl -u 'vibe-dashboard-*'` and `systemctl list-timers 'vibe-dashboard-*'`.

## 6. Updates

The follower makes every merge to `main` go live once `main`'s checks pass. Each minute it fetches
`main`, which costs no API request. When `main` points at a commit it has not deployed and not recorded
as bad, it reads that commit's check runs from GitHub's public API, without a credential, and moves on
only when every check run (the latest per check) completed as `success`, `skipped` or `neutral` and at
least one succeeded. No checks yet, a pending, failed or cancelled check, or an unreadable listing all
mean wait.

After fast-forwarding, a change to the server's Python (`dashboard/` outside `dashboard/static/`, or
`bin/vibe-dashboard`) restarts the listed units, and each must answer `GET /api/dashboard` on its
port with HTTP 200 and `"version": 1` within 30 seconds. If one does not, the follower resets the
checkout to the previous commit, restarts the units again, and records the commit in
`/opt/vibe-verifier/.git/vibe-dashboard-bad-commit`; it is not tried again until `main` moves past it.
Changes under `dashboard/static/` or to documentation go live without a restart. The timers run the
new helper code on their next tick, and a change to the follower re-runs it.

Unauthenticated API requests share 60 an hour per address, and GitHub counts a `304 Not Modified` as
free only for a request that carries an `Authorization` header, so the follower keeps no ETags.
Instead it asks at most once a minute and only while a new commit is undecided, and when GitHub reports
the hourly limit spent it asks nothing until the reset GitHub gave.

Not applied automatically: a changed unit template (copy it again and `systemctl daemon-reload`), a
changed configuration or `host.env` (restart the unit), and a merged follower that cannot start, which
systemd keeps restarting on the new copy (reset `/opt/vibe-verifier` to the previous commit and restart
it). The health check covers the web units only; a broken helper shows as a failed unit in
`systemctl --failed`.

## Data lifecycle and removal

- `hosts.yml`: replaced every ten minutes, read only by `gh` inside the web service, expires an hour
  after minting whatever happens to the host.
- `telemetry.json`: replaced every 30 seconds; the root-only state behind it keeps at most seven days
  of samples, and the collector prunes older ones.
- The bad-commit record: written on a failed health check, ignored once `main` moves past it, removed
  with the checkout.
- Removal: `systemctl disable --now` the instance's units (and the follower once no dashboard is left),
  delete the unit files from `/etc/systemd/system` and `systemctl daemon-reload`, then remove
  `/etc/vibe-dashboard/NAME`, `/var/lib/vibe-dashboard/NAME` and `/var/lib/vibe-dashboard-state/NAME`,
  `userdel vibe-dashboard-NAME`, and `/opt/vibe-verifier` when nothing else uses it. Uninstall the
  GitHub App, or remove the repositories from its installation, when no dashboard needs it.

`tests/test_dashboard_host.py` covers the token request and its scope checks, the published files'
owner and mode, the telemetry projection, and the unit templates' sandbox lines.
`tests/test_dashboard_follow.py` covers the check-run gate, the rate-limit pause, the health check,
and the rollback. Neither runs on a systemd host; the units are verified by their text.
