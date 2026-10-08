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
- `lanes/tests/lane-slot-test.sh::at the bound a cleanup deletes its copy in place and adds nothing to the trash: it says why, and frees the instance only once the copy is gone`
- `lanes/tests/lane-slot-test.sh::under pressure the reaper deletes three entries at a time: with seven waiting, three deletes run together and never more than three`
- `lanes/tests/lane-slot-test.sh::a helper replaced while the reaper deletes takes over between batches: no delete is cut short, and the new helper runs in the reaper's place with entries still waiting`
- `lanes/tests/lane-slot-test.sh::on a btrfs store prepare makes the job's store a snapshot of the preloaded subvolume, at the slot's usual path`
- `lanes/tests/lane-slot-test.sh::on an xfs store nothing changes: the reflink copy and the trash as before, and btrfs is never called`
- `lanes/tests/build-runner-image-test.sh::on a btrfs store the build creates the new store as a subvolume before the preload container mounts it`
- `lanes/tests/provision-lane-test.sh::with store_fs: btrfs on a lane whose store is XFS, --apply ends at an OPERATOR ACTION gate that names the one conversion command`
- `lanes/tests/provision-lane-test.sh::every carried store is a subvolume of the new store with the files and the mode it had, copied from the XFS image mounted read-only`
- `lanes/tests/provision-lane-test.sh::when the copied store takes no snapshot, the finished btrfs store is dropped, the XFS store put back and the listener restarted: the check is the last word`
- `lanes/tests/lane-firewall-test.sh::DOCKER-USER rejects the lane bridge to RFC1918, CGNAT and link-local`
- `lanes/tests/runner-entrypoint-test.sh::the JIT config is in no command's argv and in none of the entrypoint's output`
- `lanes/tests/test_lanes.py::an invalid file exits 3 with the reason`
- `lanes/listener/plan_test.go::the last free slot goes to an assigned qae job before ci's warm runner`

## Gotchas

- The kit is Linux-only: its tests need GNU userland, bash 5 and util-linux `flock`, so on a Mac run `lanes/tests/run-all.sh` in an Ubuntu container. The catalog's own `python3 -m unittest discover -s tests` does not run them; the `lanes` and `lanes-listener` CI jobs do.
- The names under "Legacy names" in the manual (the `KNOWN_CI_` environment prefix, the in-image paths, the firewall comment, the image label) are a contract with machines already running. Renaming one is a migration, not a refactor.
- A machine fast-forwards its engine checkout to this catalog's `main` within five minutes of every merge, and applies when the merge changed anything under `lanes/` (the trigger is that directory's tree id, not the commit). A change under `lanes/` reaches every machine that runs the kit that way, without a release step; a change anywhere else applies nothing.
- The slot units run `lanes/bin/lane-slot.sh` straight from the engine checkout, so a change to it is live for the next job the moment a machine pulls it, before `provision-lane.sh` has applied the same merge (and for as long as that apply fails). A helper change must work with the units, environment and directories the previous kit left: the store trash is made by the helper when it is missing, and a reaper unit it cannot start is a log line, not a failure. A process that outlives a pull is the other half: the store reaper never ends on a busy lane, so it replaces itself with the helper on disk after five minutes of work.
- The store trash is bounded on purpose (`TRASH_MAX` in `lanes/bin/lane-slot.sh`, the unit's slot count). Unbounded, it let a live lane retire 2.1 to 2.8 store copies a minute while the disk deleted 1.5 to 2.3, and in an hour the store reached 90% of its inodes and four slots of five sat waiting in `prepare` (2026-10-07). At the bound a slot deletes its own copy in its stop path and takes no new job until that is done, which is what keeps a lane from outrunning its disk. Raising the bound makes resets look cheaper only until the store fills.
- A lane's store is XFS (a job's store is a reflink copy) or btrfs (a snapshot), and `lanes/bin/lane-slot.sh` and the image build follow the filesystem they find, never the host file's `store_fs`. So the key alone changes nothing on a lane that has a store: `--apply` converts no store, and `provision-lane.sh <host> <lane> --convert-store --apply` does, with the lane off its jobs meanwhile. The key must reach a machine after the kit that knows it, since an older kit refuses a host file with a key it does not know. The btrfs numbers in the manual are from a manual experiment (2026-10-08), not from this code, which no machine had run on a btrfs store when it merged: the tests stub `btrfs`, `mkfs.btrfs`, `mount` and `stat -f`.
- A host file belongs in the owner's private repository. Nothing under `lanes/` may name a real machine, organization or App: `lanes/examples/hosts/example.yml` and the test fixtures describe machines that do not exist.
