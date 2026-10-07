#!/usr/bin/env python3
"""Tests for lib/lane_load.py: what a host file must declare, and every refusal."""

from copy import deepcopy

import lane_fixtures
from lane_fixtures import DASH, ORG, TMP, USER, WAIT, check, host_file, refused, without
from lane_load import load_host

# ---- one lane's refusals ----------------------------------------------------------------------------
lane_invalid = [
    ({**ORG, "extra": 1}, "lanes.box-ci must declare exactly"),
    (without(ORG, "slot_disk_gb"), "lanes.box-ci must declare exactly"),
    (without(ORG, "min_runners"), "lanes.box-ci must declare exactly"),
    (without(ORG, "runner_group"), "lanes.box-ci must declare exactly"),
    (without(ORG, "repos"), "lanes.box-ci must declare exactly"),
    ({**ORG, "name": "Box CI"}, "lanes[0].name must match"),
    ({**ORG, "name": "box@ci"}, "lanes[0].name must match"),
    ({**ORG, "name": "9box"}, "lanes[0].name must match"),
    ({**ORG, "name": "abcdefghijklmno"}, "the bridge abcdefghijklmno0 exceeds the kernel's 15-character interface limit"),
    ({**ORG, "scope": "team"}, "lanes.box-ci.scope must be org or user"),
    ({**ORG, "owner": "-x"}, "lanes.box-ci.owner must match"),
    ({**ORG, "runner_group": ""}, "lanes.box-ci.runner_group must match"),
    ({**ORG, "name_prefix": "../x"}, "lanes.box-ci.name_prefix must match"),
    ({**ORG, "name_prefix": "box.lane"}, "lanes.box-ci.name_prefix must match"),
    ({**ORG, "labels": {"ci": "ci-general"}}, "lanes.box-ci.labels.ci must match"),
    ({**ORG, "labels": {"ci": "vps"}}, "repository-runner routing labels"),
    ({**ORG, "labels": {"ci": "box-ci", "qae": "vps-2"}}, "repository-runner routing labels"),
    ({**ORG, "labels": {"ci": "Box"}}, "lanes.box-ci.labels.ci must match"),
    ({**ORG, "labels": {"qae": "box-qae"}}, "lanes.box-ci.labels must declare ci, and may declare qae"),
    ({**ORG, "labels": {"ci": "box-ci", "build": "box-build"}}, "lanes.box-ci.labels must declare ci, and may declare qae"),
    ({**ORG, "labels": ["box-ci"]}, "lanes.box-ci.labels must declare ci, and may declare qae"),
    ({**ORG, "labels": {"ci": "box-ci", "qae": "box-ci"}}, "lanes.box-ci.labels.ci and lanes.box-ci.labels.qae must differ"),
    ({**ORG, "slots": 0}, "lanes.box-ci.slots must be a positive integer"),
    ({**ORG, "slots": True}, "lanes.box-ci.slots must be a positive integer"),
    ({**ORG, "slots": "5"}, "lanes.box-ci.slots must be a positive integer"),
    ({**ORG, "image": "Box:latest"}, "lanes.box-ci.image must match"),
    ({**ORG, "repos": []}, "lanes.box-ci.repos must be a non-empty list"),
    ({**ORG, "repos": ["owner/alpha"]}, "lanes.box-ci.repos must be a non-empty list"),
    ({**ORG, "repos": ["alpha", "alpha"]}, "lanes.box-ci.repos must not contain duplicates"),
    ({**ORG, "slice": without(ORG["slice"], "memory_high")}, "lanes.box-ci.slice must declare exactly"),
    ({**ORG, "slice": {**ORG["slice"], "memory_max": "16g"}}, "lanes.box-ci.slice.memory_max must match"),
    ({**ORG, "slice": {**ORG["slice"], "memory_high": "14 GB"}}, "lanes.box-ci.slice.memory_high must match"),
    ({**ORG, "slice": {**ORG["slice"], "cpu_quota": "8"}}, "lanes.box-ci.slice.cpu_quota must match"),
    ({**ORG, "slice": {**ORG["slice"], "cpu_weight": 0}}, "lanes.box-ci.slice.cpu_weight must be an integer in 1..10000"),
    ({**ORG, "slice": {**ORG["slice"], "cpu_weight": 10001}}, "lanes.box-ci.slice.cpu_weight must be an integer in 1..10000"),
    ({**ORG, "container": without(ORG["container"], "tmp_size")}, "lanes.box-ci.container must declare exactly"),
    ({**ORG, "container": {**ORG["container"], "memory": "4G"}}, "lanes.box-ci.container.memory must match"),
    ({**ORG, "container": {**ORG["container"], "pids": -1}}, "lanes.box-ci.container.pids must be a positive integer"),
    ({**ORG, "container": {**ORG["container"], "tmp_size": "2 g"}}, "lanes.box-ci.container.tmp_size must match"),
    ({**ORG, "runtime_max_sec": 0}, "lanes.box-ci.runtime_max_sec must be a positive integer"),
    ({**ORG, "warm_max_age_sec": 0}, "lanes.box-ci.warm_max_age_sec must be a positive integer"),
    ({**ORG, "warm_max_age_sec": -60}, "lanes.box-ci.warm_max_age_sec must be a positive integer"),
    ({**ORG, "warm_max_age_sec": True}, "lanes.box-ci.warm_max_age_sec must be a positive integer"),
    ({**ORG, "warm_max_age_sec": "1200"}, "lanes.box-ci.warm_max_age_sec must be a positive integer"),
    # With a warm pool (this lane keeps 2) it must be below runtime_max_sec, declared or default.
    ({**ORG, "warm_max_age_sec": 3600}, "lanes.box-ci.warm_max_age_sec (1200 when absent) must be below runtime_max_sec (3600) on a lane with a warm pool"),
    ({**ORG, "warm_max_age_sec": 4000}, "lanes.box-ci.warm_max_age_sec (1200 when absent) must be below runtime_max_sec (3600) on a lane with a warm pool"),
    ({**ORG, "runtime_max_sec": 900}, "lanes.box-ci.warm_max_age_sec (1200 when absent) must be below runtime_max_sec (900) on a lane with a warm pool"),
    ({**ORG, "min_runners": 0, "warm_max_age_sec": 0}, "lanes.box-ci.warm_max_age_sec must be a positive integer"),
    ({**ORG, "slot_disk_gb": 1.5}, "lanes.box-ci.slot_disk_gb must be a positive integer"),
    ({**ORG, "store_disk_gb": 0}, "lanes.box-ci.store_disk_gb must be a positive integer"),
    ({**ORG, "min_runners": -1}, "lanes.box-ci.min_runners must be an integer in 0..3"),
    ({**ORG, "min_runners": 4}, "lanes.box-ci.min_runners must be an integer in 0..3"),
    ({**ORG, "min_runners": True}, "lanes.box-ci.min_runners must be an integer in 0..3"),
    ({**ORG, "min_runners": "2"}, "lanes.box-ci.min_runners must be an integer in 0..3"),
    ({**ORG, "qae_concurrency": 0}, "lanes.box-ci.qae_concurrency must be an integer in 1..3"),
    ({**ORG, "qae_concurrency": 4}, "lanes.box-ci.qae_concurrency must be an integer in 1..3"),
    (without(ORG, "codex_store"), "lanes.box-ci must declare exactly"),
    (without(ORG, "qae_concurrency"), "lanes.box-ci must declare exactly"),
    ({**ORG, "labels": {"ci": "box-ci"}}, "codex_store and qae_concurrency come with labels.qae"),
    ({**ORG, "app": {}}, "lanes.box-ci.app must declare exactly id, installation_id, key_file, login"),
    ({**ORG, "app": without(ORG["app"], "login")}, "lanes.box-ci.app must declare exactly"),
    ({**ORG, "app": {**ORG["app"], "extra": 1}}, "lanes.box-ci.app must declare exactly"),
    ({**ORG, "app": "/etc/box-ci/app.pem"}, "lanes.box-ci.app must declare exactly"),
    ({**ORG, "app": {**ORG["app"], "id": 0}}, "lanes.box-ci.app.id must be a positive integer"),
    ({**ORG, "app": {**ORG["app"], "id": "123456"}}, "lanes.box-ci.app.id must be a positive integer"),
    ({**ORG, "app": {**ORG["app"], "installation_id": True}}, "lanes.box-ci.app.installation_id must be a positive integer"),
    ({**ORG, "app": {**ORG["app"], "key_file": "etc/box-ci/app.pem"}}, "lanes.box-ci.app.key_file must match"),
    ({**ORG, "app": {**ORG["app"], "key_file": "/etc/box ci/app.pem"}}, "lanes.box-ci.app.key_file must match"),
    ({**ORG, "app": {**ORG["app"], "key_file": "/etc/../root/app.pem"}}, "lanes.box-ci.app.key_file must not contain a .. component"),
    ({**ORG, "app": {**ORG["app"], "login": "lane bot"}}, "lanes.box-ci.app.login must match"),
    ({**ORG, "codex_store": "var/lib/box-ci/codex"}, "lanes.box-ci.codex_store must match"),
    ({**ORG, "codex_store": "/var/lib/box-ci/../codex"}, "lanes.box-ci.codex_store must not contain a .. component"),
    ({**ORG, "check_ports": []}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": "22"}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": [0]}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": [65536]}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": [True]}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": ["22"]}, "lanes.box-ci.check_ports must be a non-empty list of ports in 1..65535"),
    ({**ORG, "check_ports": [22, 22]}, "lanes.box-ci.check_ports must not contain duplicates"),
    # The preload map: a plain relative path named supabase, and for an org lane one entry per
    # repository in repos and none outside it.
    ({**ORG, "preload": ["alpha"]}, "lanes.box-ci.preload must be a map"),
    ({**ORG, "preload": {"alpha": "supabase/db", "beta.web": "-"}}, "lanes.box-ci.preload.alpha: 'supabase/db' is not a directory named supabase"),
    ({**ORG, "preload": {"alpha": "../supabase", "beta.web": "-"}}, "lanes.box-ci.preload.alpha: '../supabase' must be a plain relative path"),
    ({**ORG, "preload": {"alpha": "/supabase", "beta.web": "-"}}, "lanes.box-ci.preload.alpha: '/supabase' must be a plain relative path"),
    ({**ORG, "preload": {"alpha": "./supabase", "beta.web": "-"}}, "lanes.box-ci.preload.alpha: './supabase' must be a plain relative path"),
    ({**ORG, "preload": {"alpha": 7, "beta.web": "-"}}, "lanes.box-ci.preload.alpha: 7 must be a plain relative path"),
    ({**ORG, "preload": {"alpha": "supabase"}}, "lanes.box-ci.repos lists beta.web but preload has no entry for it"),
    ({**ORG, "preload": {"alpha": "supabase", "beta.web": "-", "gamma": "-"}}, "lanes.box-ci.preload names gamma, which is not in lanes.box-ci.repos"),
    ({**ORG, "preload": {"a/b": "-"}}, "lanes.box-ci.preload: 'a/b' must be a repository name"),
    # Scope-specific keys.
    ({**ORG, "exclude": []}, "lanes.box-ci: exclude belong to user lanes only"),
    # The wait block.
    ({**ORG, "wait": {"label": "box-wait", "slots": 5}}, "lanes.box-ci.wait must declare exactly label, memory, slots"),
    ({**ORG, "wait": {**WAIT, "extra": 1}}, "lanes.box-ci.wait must declare exactly label, memory, slots"),
    ({**ORG, "wait": "box-wait"}, "lanes.box-ci.wait must declare exactly label, memory, slots"),
    ({**ORG, "wait": {**WAIT, "label": "box-ci"}}, "lanes.box-ci.wait.label must differ from lanes.box-ci.labels.ci"),
    ({**ORG, "wait": {**WAIT, "label": "box-qae"}}, "lanes.box-ci.wait.label must differ from lanes.box-ci.labels.qae"),
    ({**ORG, "wait": {**WAIT, "label": "ci-wait"}}, "lanes.box-ci.wait.label must match"),
    ({**ORG, "wait": {**WAIT, "label": "vps"}}, "repository-runner routing labels"),
    ({**ORG, "wait": {**WAIT, "label": "Box-Wait"}}, "lanes.box-ci.wait.label must match"),
    ({**ORG, "wait": {**WAIT, "slots": 0}}, "lanes.box-ci.wait.slots must be a positive integer"),
    ({**ORG, "wait": {**WAIT, "slots": True}}, "lanes.box-ci.wait.slots must be a positive integer"),
    ({**ORG, "wait": {**WAIT, "slots": "5"}}, "lanes.box-ci.wait.slots must be a positive integer"),
    ({**ORG, "wait": {**WAIT, "memory": "1G"}}, "lanes.box-ci.wait.memory must match"),
    ({**ORG, "wait": {**WAIT, "memory": 1024}}, "lanes.box-ci.wait.memory must match"),
]
for index, (block, marker) in enumerate(lane_invalid):
    refused(host_file(block), marker, f"org lane case {index}: {marker}")

user_invalid = [
    ({**USER, "min_runners": 0}, "lanes.own-ci: min_runners belong to org lanes only"),
    ({**USER, "runner_group": "x", "repos": ["a"]}, "lanes.own-ci: repos, runner_group belong to org lanes only"),
    (without(USER, "exclude"), "lanes.own-ci must declare exactly"),
    ({**USER, "exclude": "fleet"}, "lanes.own-ci.exclude must be a list"),
    ({**USER, "exclude": ["owner/fleet"]}, "lanes.own-ci.exclude must be a list"),
    ({**USER, "exclude": ["fleet", "fleet"]}, "lanes.own-ci.exclude must not contain duplicates"),
    ({**USER, "preload": {"app": "supabase/x"}}, "lanes.own-ci.preload.app: 'supabase/x' is not a directory named supabase"),
    ({**USER, "wait": WAIT}, "lanes.own-ci: wait belongs to org lanes only"),
]
for index, (block, marker) in enumerate(user_invalid):
    refused(host_file(block), marker, f"user lane case {index}: {marker}")
check(load_host(host_file({**USER, "preload": {"app": "supabase", "site": "apps/db/supabase", "docs": "-"}})).lanes[0].preload
      == (("app", "supabase"), ("docs", "-"), ("site", "apps/db/supabase")), "a user lane's preload map is its list, in any order")

# ---- the host, and what its lanes may not share -----------------------------------------------------
refused(host_file(ORG, top={"extra": 1}), "must declare exactly hostname, lanes, sysbox", "an unknown host key is refused")
refused(host_file(ORG, top={"hostname": "box 1"}), "host.hostname must match", "a host name a shell line could misread is refused")
refused(host_file(ORG, top={"sysbox": {"image": "/var/lib/runners/sysbox.img"}}), "host.sysbox must declare exactly disk_gb, image", "the Sysbox filesystem needs its image and size")
refused(host_file(ORG, top={"sysbox": {"image": "sysbox.img", "disk_gb": 20}}), "host.sysbox.image must match", "the Sysbox image path is absolute")
refused(host_file(ORG, top={"sysbox": {"image": "/var/lib/sysbox.img", "disk_gb": 0}}), "host.sysbox.disk_gb must be a positive integer", "the Sysbox image has a size")
refused(host_file(top={"lanes": []}), "host.lanes must be a non-empty list", "a host runs at least one lane")
other = {**USER, "name": "other-ci", "labels": {"ci": "other-ci", "qae": "other-qae"}, "name_prefix": "other-lane", "image": "other-ci-runner",
         "app": {**USER["app"], "key_file": "/etc/other-ci/app.pem"}, "codex_store": "/var/lib/other-ci/codex"}
check(len(load_host(host_file(ORG, USER, other)).lanes) == 3, "three lanes with distinct names, labels, prefixes, images, keys, stores and slices load")
across = [
    ({**other, "name": "own-ci"}, "lanes own-ci and own-ci share the name own-ci"),
    ({**other, "labels": {"ci": "other-ci", "qae": "own-qae"}}, "lanes own-ci and other-ci share the label own-qae"),
    ({**other, "labels": {"ci": "box-ci", "qae": "other-qae"}}, "lanes box-ci and other-ci share the label box-ci"),
    ({**other, "name_prefix": "own-lane"}, "lanes own-ci and other-ci share the name_prefix own-lane"),
    ({**without(other, "exclude"), "scope": "org", "runner_group": "other-ci", "repos": ["alpha"], "min_runners": 0, "preload": {"alpha": "-"}, "wait": {**WAIT, "label": "box-qae"}},
     "lanes box-ci and other-ci share the label box-qae"),
    ({**other, "image": "box-ci-runner"}, "lanes box-ci and other-ci share the image box-ci-runner"),
    ({**other, "app": {**other["app"], "key_file": "/etc/box-ci/app.pem"}}, "lanes box-ci and other-ci share the app.key_file /etc/box-ci/app.pem"),
    ({**other, "codex_store": "/var/lib/own-ci/codex"}, "lanes own-ci and other-ci share the codex_store /var/lib/own-ci/codex"),
    ({**other, "name": "own-xy"}, "lanes own-ci and own-xy share the slice unit own.slice"),
    ({**other, "name": "box"}, "lanes box-ci and box share the slice unit box.slice"),
]
for index, (block, marker) in enumerate(across):
    refused(host_file(ORG, USER, block), marker, f"across lanes, case {index}: {marker}")
refused(host_file(ORG, {**USER, "qae_concurrency": 2}, {**other, "codex_store": "/var/lib/own-ci/codex-2"}),
        "lanes own-ci and other-ci share the codex_store /var/lib/own-ci/codex-2",
        "across lanes, no lane's Codex store is another lane's second QAE instance's store")
dup = TMP / "dup.yml"
dup.write_text(host_file(ORG).read_text().replace("  slots: 3\n", "  slots: 3\n  slots: 4\n"), encoding="utf-8")
refused(dup, "'slots' is given twice", "a key given twice is refused (PyYAML would keep the last one silently)")
refused(host_file(ORG, top={"lanes": "box-ci"}), "host.lanes must be a non-empty list", "lanes is a list")

# ---- dashboards -------------------------------------------------------------------------------------
check(load_host(host_file(ORG)).dashboards == (), "a host may declare no dashboards")
dhost = load_host(host_file(ORG, USER, top={"dashboards": [DASH, {**without(DASH, "bridge_account"), "name": "own", "lane": "own-ci", "port": 8766, "config": {"version": 1, "owner": "someone"}}]}))
box, own = dhost.dashboards
check((box.name, box.lane.name, box.port, box.bridge_account, own.bridge_account) == ("box", "box-ci", 8765, "box-forward", None), "a dashboard names its lane, port and optional bridge account")
check((box.account, box.telemetry_file) == ("vibe-dashboard-box", "/var/lib/vibe-dashboard/box/telemetry.json"), "its account and telemetry file are the host kit's names")
dash_refusals = [
    ({**DASH, "extra": 1}, "dashboards[0] must declare exactly config, lane, name, port, and may declare bridge_account"),
    (without(DASH, "port"), "dashboards[0] must declare exactly"),
    ({**DASH, "name": "Box"}, "dashboards[0].name must match"),
    ({**DASH, "name": "a-very-long-name-x"}, "the account vibe-dashboard-a-very-long-name-x exceeds useradd's 32 characters"),
    ({**DASH, "lane": "nowhere"}, "dashboards.box.lane must be one of this machine's lanes (box-ci)"),
    ({**DASH, "lane": ["box-ci"]}, "dashboards.box.lane must be one of this machine's lanes (box-ci)"),
    ({**DASH, "port": 0}, "dashboards.box.port must be an integer in 1..65535"),
    ({**DASH, "port": "8765"}, "dashboards.box.port must be an integer in 1..65535"),
    ({**DASH, "bridge_account": "Box Forward"}, "dashboards.box.bridge_account must match"),
    ({**DASH, "config": []}, "dashboards.box.config must be a dashboard configuration with version 1"),
    ({**DASH, "config": {**DASH["config"], "version": 2}}, "dashboards.box.config must be a dashboard configuration with version 1"),
    ({**DASH, "config": {**DASH["config"], "owner": "someone-else"}}, "dashboards.box.config.owner must be the owner of its lane box-ci, example"),
    ({**DASH, "config": {**DASH["config"], "telemetry_file": "/tmp/t.json"}}, "must not set telemetry_file: it is always /var/lib/vibe-dashboard/box/telemetry.json"),
]
for index, (block, marker) in enumerate(dash_refusals):
    refused(host_file(ORG, top={"dashboards": [block]}), marker, f"dashboard refusal {index}: {marker}")
refused(host_file(ORG, top={"dashboards": [DASH, {**DASH, "port": 8766}]}), "two dashboards share a name", "two dashboards cannot share a name")
refused(host_file(ORG, top={"dashboards": [DASH, {**DASH, "name": "other"}]}), "two dashboards share a port", "two dashboards cannot share a port")
refused(host_file(ORG, top={"dashboards": {"box": DASH}}), "host.dashboards must be a list", "dashboards is a list")
check(len(load_host(host_file(ORG, top={"dashboards": [{**DASH, "name": "a-name-of-17-char"}]})).dashboards) == 1, "a 17-character name fits: vibe-dashboard-<name> is 32")
check(deepcopy(ORG) == ORG, "the fixtures were not mutated")

lane_fixtures.finish("test_lane_load")
