#!/usr/bin/env python3
"""Tests for lib/lane_model.py: what a lane's name and settings derive (its user, paths, kinds, units,
slices, Codex stores and RuntimeMaxSec), read from lanes that lib/lane_load.py built."""

import lane_fixtures
from lane_fixtures import ORG, USER, WAIT, check, host_file, without
from lane_load import load_host
from lane_model import docker_size_bytes

# ---- an org lane ------------------------------------------------------------------------------------
host = load_host(host_file(ORG, USER))
org, user = host.lanes
check((host.hostname, host.sysbox_image, host.sysbox_disk_gb) == ("box-1", "/var/lib/runners/sysbox.img", 20), "the host's name and Sysbox filesystem are read")
check((org.name, org.scope, org.owner, org.runner_group, org.repos) == ("box-ci", "org", "example", "box-ci", ("alpha", "beta.web")), "an org lane's scope, owner, runner group and repositories")
check((org.ci_label, org.qae_label, org.kinds) == ("box-ci", "box-qae", ("ci", "qae")), "an org lane may run QAE: labels.qae adds the qae kind")
check((org.app_id, org.app_installation_id, org.app_key_file, org.app_login) == (123456, 7654321, "/etc/box-ci/app.pem", "example-bot"), "the lane's own App: id, installation, key file and login")
check((org.user, org.state_dir, org.home, org.bridge) == ("box-ci", "/var/lib/box-ci", "/var/lib/box-ci/home", "box-ci0"), "the lane's user is its name, with home <state>/home, and its bridge is <name>0")
check((org.min_runners, org.slots, org.qae_concurrency, org.codex_store) == (2, 3, 1, "/var/lib/box-ci/codex"), "warm pool, budget, QAE concurrency and Codex store")
check(org.preload == (("alpha", "supabase"), ("beta.web", "-")) and org.preload_text == "alpha supabase\nbeta.web -\n", "the preload map, sorted, and its canonical text")
check((org.memory_max, org.memory_high, org.cpu_quota, org.cpu_weight) == ("16G", "14G", "800%", 50), "the slice limits")
check((org.container_memory, org.pids, org.tmp_size) == ("4g", 2048, "2g"), "the container limits")
check((org.runtime_max_sec, org.slot_disk_gb, org.store_disk_gb, org.check_ports) == (3600, 10, 40, (22, 54321, 54322)), "the runtime, disks and check ports")
# store_fs is optional: xfs when absent, which is what every lane had before the key.
check(org.store_fs == "xfs", "a lane that declares no store_fs has an xfs store")
check(load_host(host_file({**ORG, "store_fs": "btrfs"})).lanes[0].store_fs == "btrfs" and load_host(host_file({**ORG, "store_fs": "xfs"})).lanes[0].store_fs == "xfs", "a declared store_fs, btrfs or xfs, is used")
check(load_host(host_file({**USER, "store_fs": "btrfs"})).lanes[0].store_fs == "btrfs", "a user lane may declare store_fs too")
# warm_max_age_sec is optional (1200 when absent). With a warm pool the slot unit's RuntimeMaxSec is
# it plus runtime_max_sec, so a slot about to be recycled still gives a job the whole budget.
check((org.warm_max_age_sec, org.slot_runtime_max_sec) == (1200, 4800), "a warm-pool lane's RuntimeMaxSec is warm_max_age_sec + runtime_max_sec")
cold = load_host(host_file({**ORG, "min_runners": 0})).lanes[0]
check((cold.warm_max_age_sec, cold.slot_runtime_max_sec) == (1200, 3600), "without a warm pool RuntimeMaxSec is runtime_max_sec alone")
short = load_host(host_file({**ORG, "warm_max_age_sec": 600})).lanes[0]
check((short.warm_max_age_sec, short.slot_runtime_max_sec) == (600, 4200), "a declared warm_max_age_sec is used")
cold_short = load_host(host_file({**ORG, "min_runners": 0, "runtime_max_sec": 900})).lanes[0]
check((cold_short.warm_max_age_sec, cold_short.slot_runtime_max_sec) == (1200, 900), "without a warm pool the key is inert: a short budget loads with it absent")
cold_declared = load_host(host_file({**ORG, "min_runners": 0, "runtime_max_sec": 900, "warm_max_age_sec": 5000})).lanes[0]
check((cold_declared.warm_max_age_sec, cold_declared.slot_runtime_max_sec) == (5000, 900), "without a warm pool a declared value above the budget is accepted as it is")
for bound in (0, 3):
    check(load_host(host_file({**ORG, "min_runners": bound})).lanes[0].min_runners == bound, f"min_runners {bound} is within 0..slots")
check(org.template("ci") == "box-ci-ci@.service" and org.template("qae") == "box-ci-qae@.service", "one slot template per kind")
check(org.unit("ci", 1) == "box-ci-ci@1.service" and org.unit("qae", 3) == "box-ci-qae@3.service", "slot units per kind and instance")
check(org.always_on_template == "box-ci@.service", "the retiring always-on template is named for Phase L")
for outside in (0, 4):
    try:
        org.unit("ci", outside)
        check(False, f"unit({outside}) is refused")
    except ValueError as error:
        check("outside 1..3" in str(error), f"unit({outside}) is refused")
ci_only = load_host(host_file(without({**ORG, "labels": {"ci": "box-ci"}}, "codex_store", "qae_concurrency"))).lanes[0]
check(ci_only.kinds == ("ci",) and ci_only.qae_label is None and ci_only.codex_store is None and ci_only.qae_concurrency == 0, "without labels.qae a lane runs ci only")
try:
    ci_only.template("qae")
    check(False, "a ci-only lane refuses the qae kind")
except ValueError as error:
    check("not one of ci" in str(error), "a ci-only lane refuses the qae kind")
# The optional wait kind: its own label, template, budget and container memory.
check((ci_only.wait_label, ci_only.wait_slots, ci_only.wait_memory, ci_only.kind_slots("ci")) == (None, 0, None, 3), "a lane without a wait block has no wait kind and its ci budget is slots")
try:
    ci_only.kind_slots("wait")
    check(False, "a lane without a wait block refuses the wait kind")
except ValueError as error:
    check("not one of ci" in str(error), "a lane without a wait block refuses the wait kind")
waiting = load_host(host_file({**ORG, "wait": WAIT})).lanes[0]
check((waiting.kinds, waiting.wait_label, waiting.wait_slots, waiting.wait_memory) == (("ci", "qae", "wait"), "box-wait", 5, "1g"), "an org lane's wait block adds the wait kind after ci and qae")
check((waiting.kind_slots("ci"), waiting.kind_slots("qae"), waiting.kind_slots("wait")) == (3, 3, 5), "the wait kind's budget is its own: wait.slots, not slots")
check((waiting.label("wait"), waiting.template("wait"), waiting.unit("wait", 5)) == ("box-wait", "box-ci-wait@.service", "box-ci-wait@5.service"), "the wait kind has its own label, slot template and instances 1..wait.slots")
for kind, outside, bound in (("wait", 6, "1..5"), ("ci", 4, "1..3")):
    try:
        waiting.unit(kind, outside)
        check(False, f"unit({kind}, {outside}) is refused")
    except ValueError as error:
        check(f"outside {bound}" in str(error), f"unit({kind}, {outside}) is outside its kind's own range {bound}")
wait_only = load_host(host_file(without({**ORG, "labels": {"ci": "box-ci"}, "wait": WAIT}, "codex_store", "qae_concurrency"))).lanes[0]
check(wait_only.kinds == ("ci", "wait"), "a lane may run ci and wait without qae")
check((org.slice, org.top_slice) == ("box-ci.slice", "box.slice"), "systemd nests dashed slices, so the weight also sits on the root-level parent")
undashed = load_host(host_file({**ORG, "name": "boxci"})).lanes[0]
check(undashed.slice == "boxci.slice" and undashed.top_slice is None, "an undashed lane has no parent slice")

# ---- a user lane ------------------------------------------------------------------------------------
check((user.scope, user.owner, user.runner_group, user.repos, user.exclude) == ("user", "Someone", "default", (), ("fleet",)), "a user lane lists no repositories, excludes some, and uses the default group")
check((user.min_runners, user.slot_runtime_max_sec, user.kinds) == (0, 3600, ("ci", "qae")), "a user lane keeps no warm pool")
check(load_host(host_file({**USER, "exclude": []})).lanes[0].exclude == (), "an empty exclude list is valid")
check(load_host(host_file({**USER, "qae_concurrency": 4})).lanes[0].qae_concurrency == 4, "qae_concurrency may use every slot")
uwarm = load_host(host_file({**USER, "warm_max_age_sec": 900})).lanes[0]
check((uwarm.warm_max_age_sec, uwarm.slot_runtime_max_sec) == (900, 3600), "a user lane's warm_max_age_sec is inert")

# ---- Codex stores and docker sizes -----------------------------------------------------------------
three = load_host(host_file({**USER, "qae_concurrency": 3})).lanes[0]
check(three.codex_stores == ("/var/lib/own-ci/codex", "/var/lib/own-ci/codex-2", "/var/lib/own-ci/codex-3"),
      "one Codex store per QAE instance: the first keeps codex_store itself, instance n gets the sibling <codex_store>-<n>")
check(ci_only.codex_stores == () and user.codex_stores == ("/var/lib/own-ci/codex",), "a ci-only lane has no Codex store, and qae_concurrency 1 keeps the one store it had")
check([docker_size_bytes(size) for size in ("4g", "512m", "2048k", "1000")] == [4 * 1024**3, 512 * 1024**2, 2048 * 1024, 1000], "docker sizes are read as docker run --memory reads them")
try:
    docker_size_bytes("4G")
    check(False, "a systemd-style size is not a docker size")
except ValueError as error:
    check("is not a docker size" in str(error), "a systemd-style size is not a docker size")

lane_fixtures.finish("test_lane_model")
