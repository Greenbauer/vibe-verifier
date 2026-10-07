# Self-hosted runner lanes

The kit in `lanes/` provisions self-hosted GitHub Actions runner lanes on a machine its owner runs: one disposable Sysbox container per CI job, started by a scale-set listener. The kit is the engine; which machine, organization, GitHub App and repositories a lane serves is a host file in the owner's private repository.

## Surfaces

- source: `lanes/**`

## Reach

On an Ubuntu 24.04 machine, clone this catalog to `/opt/runner-lanes` and your private values repository (with `hosts/<host>.yml` at its root, as `lanes/examples/hosts/example.yml` shows) to `/opt/runner-lanes-config`, then run `lanes/bin/provision-host.sh <host> --apply` and `lanes/bin/provision-lane.sh <host> <lane> --apply`. The manual is [`lanes/README.md`](../../lanes/README.md).

## Verify

- `lanes/tests/runners-pull-test.sh::when only the values HEAD moved it applies the machine again and records the pair`
- `lanes/tests/runners-pull-test.sh::a catalog commit outside lanes/ is fetched and fast-forwarded to: the engine's commit moves and the kit's tree does not`
- `lanes/tests/runners-pull-test.sh::a failed lane apply exits non-zero and records nothing`
- `lanes/tests/runners-pull-test.sh::the engine checkout is fetched with no ssh command or key: it is public`
- `lanes/tests/provision-host-test.sh::the gate names the values repository by the origin of the machine's values checkout, and where that checkout is`
- `lanes/tests/provision-lane-test.sh::the slot template has no restart delay and no [Install]: nothing but the listener starts it`
- `lanes/tests/provision-lane-test.sh::adoption keeps every unit name: nothing added, nothing renamed`
- `lanes/tests/lane-slot-test.sh::prepare refuses, touching nothing, when the listener wrote no job for the instance`
- `lanes/tests/lane-slot-test.sh::cleanup renames the job's store copy into the trash instead of deleting it: whole, under a name that carries the second and the instance`
- `lanes/tests/lane-slot-test.sh::a wait slot's prepare makes no copy of the preloaded store: its store is an empty directory, mode 0700, and the store's room is never read for it`
- `lanes/tests/lane-slot-test.sh::the reaper deletes one entry at a time, oldest first: one rm for each entry, naming that entry alone by its path inside the trash`
- `lanes/tests/lane-firewall-test.sh::DOCKER-USER rejects the lane bridge to RFC1918, CGNAT and link-local`
- `lanes/tests/runner-entrypoint-test.sh::the JIT config is in no command's argv and in none of the entrypoint's output`
- `lanes/tests/test_lanes.py::an invalid file exits 3 with the reason`
- `lanes/listener/plan_test.go::the last free slot goes to an assigned qae job before ci's warm runner`

## Gotchas

- The kit is Linux-only: its tests need GNU userland, bash 5 and util-linux `flock`, so on a Mac run `lanes/tests/run-all.sh` in an Ubuntu container. The catalog's own `python3 -m unittest discover -s tests` does not run them; the `lanes` and `lanes-listener` CI jobs do.
- The names under "Legacy names" in the manual (the `KNOWN_CI_` environment prefix, the in-image paths, the firewall comment, the image label) are a contract with machines already running. Renaming one is a migration, not a refactor.
- A machine fast-forwards its engine checkout to this catalog's `main` within five minutes of every merge, and applies when the merge changed anything under `lanes/` (the trigger is that directory's tree id, not the commit). A change under `lanes/` reaches every machine that runs the kit that way, without a release step; a change anywhere else applies nothing.
- The slot units run `lanes/bin/lane-slot.sh` straight from the engine checkout, so a change to it is live for the next job the moment a machine pulls it, before `provision-lane.sh` has applied the same merge (and for as long as that apply fails). A helper change must work with the units, environment and directories the previous kit left: the store trash is made by the helper when it is missing, and a reaper unit it cannot start is a log line, not a failure.
- A host file belongs in the owner's private repository. Nothing under `lanes/` may name a real machine, organization or App: `lanes/examples/hosts/example.yml` and the test fixtures describe machines that do not exist.
