#!/usr/bin/env python3
"""Tests for lib/lanes.py, the command line the bash scripts read a host file through, on the two
host files the kit carries: the example (examples/hosts/example.yml) and the tests' own fixture
(tests/fixtures/hosts/vps-1.yml), an org lane with the wait kind and no QAE."""

import json
import re
import subprocess
import sys

import yaml

import lane_fixtures
from lane_fixtures import ROOT, TMP, check
from lane_load import load_host
from lane_render import collector_config, listener_config

# ---- the example host file --------------------------------------------------------------------------
example = load_host(ROOT / "examples/hosts/example.yml")
gb, ac = example.lane("greenbauer-ci"), example.lane("acme-ci")
check((example.hostname, example.sysbox_image, example.sysbox_disk_gb) == ("worker-1", "/var/lib/runner-lanes/sysbox.img", 20), "the example machine's name and Sysbox filesystem")
check((gb.scope, gb.owner, gb.ci_label, gb.qae_label, gb.name_prefix, gb.image, gb.exclude) == ("user", "Greenbauer", "greenbauer-ci", "greenbauer-qae", "worker1-ci", "greenbauer-ci-runner", ("scratch",)), "the example's greenbauer-ci lane")
check((gb.app_id, gb.app_installation_id, gb.app_key_file, gb.app_login, gb.codex_store) == (1000001, 20000001, "/etc/greenbauer-ci/app.pem", "example-lane-app", "/var/lib/greenbauer-ci/codex"), "greenbauer-ci's App, key and Codex store")
check((gb.user, gb.home, gb.slots, gb.slot_runtime_max_sec, gb.preload) == ("greenbauer-ci", "/var/lib/greenbauer-ci/home", 6, 3600, ()), "greenbauer-ci's user and home, and it preloads nothing")
check((ac.scope, ac.owner, ac.runner_group, ac.repos, ac.kinds) == ("org", "acme", "acme-ci", ("webapp", "docs"), ("ci", "qae", "wait")), "the example's acme-ci lane is an org lane with QAE and the wait kind")
check((ac.min_runners, ac.slot_runtime_max_sec, ac.preload) == (1, 4800, (("docs", "-"), ("webapp", "supabase"))), "acme-ci keeps a warm pool of 1 and preloads one repository's project")
check((gb.slice, gb.top_slice, ac.slice, ac.top_slice) == ("greenbauer-ci.slice", "greenbauer.slice", "acme-ci.slice", "acme.slice"), "the two lanes write distinct slice units")
fixtures = ROOT / "tests/fixtures/listener"
check(listener_config(gb) == (fixtures / "greenbauer-ci.json").read_text(encoding="utf-8"), "greenbauer-ci's listener config, byte for byte")
check(listener_config(ac) == (fixtures / "acme-ci.json").read_text(encoding="utf-8"), "acme-ci's listener config, byte for byte")

# ---- the fixture host file: an org lane with the wait kind and no QAE -------------------------------
vps = load_host(ROOT / "tests/fixtures/hosts/vps-1.yml")
ob = vps.lane("orbit-ci")
check((vps.hostname, vps.sysbox_image, vps.sysbox_disk_gb, [lane.name for lane in vps.lanes]) == ("vps-1", "/var/lib/orbit-ci/sysbox.img", 40, ["orbit-ci"]),
      "vps-1 runs the one lane orbit-ci and adopts the Sysbox image that lane mounts")
check((ob.scope, ob.owner, ob.runner_group, ob.repos) == ("org", "orbit-labs", "orbit-ci", ("api", "ci", "web")),
      "orbit-ci serves every repository of its runner group")
check((ob.kinds, ob.ci_label, ob.qae_label, ob.codex_store, ob.qae_concurrency) == (("ci", "wait"), "orbit-ci", None, None, 0),
      "orbit-ci runs CI and the wait kind: no qae kind and no Codex store")
check((ob.wait_label, ob.wait_slots, ob.wait_memory, ob.kind_slots("ci"), ob.template("wait")) == ("orbit-ci-wait", 8, "1g", 8, "orbit-ci-wait@.service"),
      "orbit-ci's wait kind: [self-hosted, orbit-ci-wait], 8 slots of 1g that are not part of the 8 ci slots")
check((ob.app_id, ob.app_installation_id, ob.app_key_file, ob.app_login) == (1000003, 20000003, "/etc/orbit-ci/app.pem", "orbit-lane-app"),
      "orbit-ci authenticates as its App with its key file")
check(dict(ob.preload) == {"api": "packages/database/supabase", "ci": "-", "web": "supabase"},
      "orbit-ci preloads each repository's Supabase project, one from packages/database, and none for ci")
check((ob.slots, ob.min_runners, ob.warm_max_age_sec, ob.slot_runtime_max_sec, ob.name_prefix, ob.image) == (8, 2, 1200, 4800, "vps1-ci", "orbit-ci-runner"),
      "orbit-ci budgets 8 slots with a warm pool of 2, under its registration prefix and image name")
check((ob.memory_max, ob.memory_high, ob.cpu_quota, ob.cpu_weight) == ("20G", "18G", "1200%", 50), "orbit-ci's slice limits")
check((ob.container_memory, ob.pids, ob.tmp_size, ob.runtime_max_sec, ob.slot_disk_gb, ob.store_disk_gb, ob.check_ports) == ("4g", 2048, "2g", 3600, 10, 40, (22, 54321, 54322)),
      "orbit-ci's container limits, runtime, disks and check ports")
check((ob.user, ob.home, ob.bridge, ob.slice, ob.top_slice) == ("orbit-ci", "/var/lib/orbit-ci/home", "orbit-ci0", "orbit-ci.slice", "orbit.slice"),
      "orbit-ci's user, home, bridge, slice and parent slice derive from its name")
check(listener_config(ob) == (fixtures / "orbit-ci.json").read_text(encoding="utf-8"), "orbit-ci's listener config, byte for byte")

# ---- their dashboards -------------------------------------------------------------------------------
CI_ROLES = [{"id": "ci-qae", "name": "QAE", "role": "qae", "workflow_role": "explorer"}]
dashes = {d.name: d for d in example.dashboards + vps.dashboards}
check([(d.name, d.lane.name, d.port, d.bridge_account) for d in example.dashboards] == [("greenbauer", "greenbauer-ci", 8765, "dashboard-forward"), ("acme", "acme-ci", 8766, None)],
      "the example serves greenbauer on 8765 behind a bridge account and acme on 8766 without one")
check([(d.name, d.lane.name, d.port, d.bridge_account) for d in vps.dashboards] == [("orbit", "orbit-ci", 8766, None)], "vps-1 serves orbit on 8766, with no bridge account")
check(all(d.config["agents"] == CI_ROLES for d in dashes.values()), "every dashboard's agents are the QAE roster")
check({name: (d.config["owner"], len(d.config["repositories"]), d.config["proxy_origin"]) for name, d in dashes.items()} == {
    "greenbauer": ("Greenbauer", 1, "https://worker-1.example.ts.net:8443"), "acme": ("acme", 2, "https://worker-1.example.ts.net:8444"),
    "orbit": ("orbit-labs", 3, "https://vps-1.example.ts.net:8444")}, "each dashboard keeps its owner, repositories and HTTPS route")
check(json.loads(collector_config(vps, dashes["orbit"]))["host"] == {"label": "vps-1 orbit-ci", "listener_config_path": "/etc/orbit-ci/listener.json",
      "lane_name": "orbit-ci", "workspace_path": "/var/lib/orbit-ci", "codex_home": "/nonexistent"}, "orbit's telemetry samples orbit-ci, which has no Codex login")
for path in sorted((ROOT / "examples/hosts").glob("*")) + sorted((ROOT / "tests/fixtures/hosts").glob("*")):
    try:
        load_host(path)
        loads = True
    except (OSError, ValueError):
        loads = False
    # bin/runners-pull.sh reads the host config name from /etc/runners-host with this pattern.
    check(loads and path.suffix == ".yml" and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", path.stem)),
          f"{path.relative_to(ROOT)} validates, under a name /etc/runners-host can hold")


# ---- the CLI ----------------------------------------------------------------------------------------
def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "lib/lanes.py"), *args], capture_output=True, text=True)


example_yml = str(ROOT / "examples/hosts/example.yml")
out = cli("lanes", example_yml)
check(out.returncode == 0 and out.stdout == "greenbauer-ci\nacme-ci\n", "lanes prints the lane names in file order")
out = cli("host-env", example_yml)
check(out.returncode == 0 and out.stdout == "HOST_NAME=worker-1\nSYSBOX_IMAGE=/var/lib/runner-lanes/sysbox.img\nSYSBOX_GB=20\nLANES=greenbauer-ci acme-ci\n", "host-env prints the machine's settings")
env = dict(line.split("=", 1) for line in cli("env", example_yml, "acme-ci").stdout.splitlines())
check(env["SCOPE"] == "org" and env["KINDS"] == "ci qae wait" and env["GROUP"] == "acme-ci" and env["REPOS"] == "webapp docs" and env["EXCLUDE"] == "", "env prints an org lane's scope, kinds, group and repositories")
check(env["LANE_USER"] == "acme-ci" and env["LANE_HOME"] == "/var/lib/acme-ci/home" and env["BRIDGE"] == "acme-ci0" and env["SLOT_RUNTIME_MAX"] == "4800", "env prints the derived user, home, bridge and RuntimeMaxSec")
check(env["APP_LOGIN"] == "acme-lane-app" and env["CODEX_STORE"] == "/var/lib/acme-ci/codex" and env["TOP_SLICE"] == "acme.slice", "env prints the App login, Codex store and parent slice")
check(env["CODEX_STORES"] == "/var/lib/acme-ci/codex /var/lib/acme-ci/codex-2" and env["QAE_CONCURRENCY"] == "2", "env prints every QAE instance's Codex store: at qae_concurrency 2, the first store and <store>-2")
check((env["WAIT_LABEL"], env["WAIT_SLOTS"], env["WAIT_MEMORY"]) == ("acme-wait", "2", "1g"), "env prints the wait kind of a lane that also runs QAE")
check(all("\n" not in value for value in env.values()) and all(key.isupper() or "_" in key for key in env), "every env line is KEY=value")
check(cli("preload", example_yml, "acme-ci").stdout == "docs -\nwebapp supabase\n" and cli("preload", example_yml, "greenbauer-ci").stdout == "", "preload prints the canonical map")
check(json.loads(cli("listener-config", example_yml, "greenbauer-ci", "--key-file", "/k/app.pem").stdout)["app"]["key_file"] == "/k/app.pem", "listener-config takes the provisioner's key path")
vps_yml = str(ROOT / "tests/fixtures/hosts/vps-1.yml")
out = cli("host-env", vps_yml)
check(out.returncode == 0 and out.stdout == "HOST_NAME=vps-1\nSYSBOX_IMAGE=/var/lib/orbit-ci/sysbox.img\nSYSBOX_GB=40\nLANES=orbit-ci\n", "host-env prints vps-1's settings")
env = dict(line.split("=", 1) for line in cli("env", vps_yml, "orbit-ci").stdout.splitlines())
check(env["KINDS"] == "ci wait" and env["QAE_LABEL"] == "" and env["CODEX_STORE"] == "" and env["QAE_CONCURRENCY"] == "0" and env["SLOT_RUNTIME_MAX"] == "4800", "env prints an org lane with ci and wait kinds and no QAE settings")
check((env["WAIT_LABEL"], env["WAIT_SLOTS"], env["WAIT_MEMORY"], env["SLOTS"], env["CONTAINER_MEMORY"]) == ("orbit-ci-wait", "8", "1g", "8", "4g"), "env prints the wait kind's label, slots and memory apart from the lane's slots and container memory")
env = dict(line.split("=", 1) for line in cli("env", example_yml, "greenbauer-ci").stdout.splitlines())
check((env["WAIT_LABEL"], env["WAIT_SLOTS"], env["WAIT_MEMORY"]) == ("", "0", ""), "a lane without a wait block prints no wait label, 0 wait slots and no wait memory")
check(env["STORE_FS"] == "xfs", "env prints the store's filesystem, xfs for a lane that declares none")
env = dict(line.split("=", 1) for line in cli("env", str(lane_fixtures.host_file({**lane_fixtures.ORG, "store_fs": "btrfs"})), "box-ci").stdout.splitlines())
check(env["STORE_FS"] == "btrfs", "env prints a declared btrfs store")
out = cli("dashboards", example_yml)
check(out.returncode == 0 and out.stdout == "greenbauer\nacme\n", "dashboards prints the dashboard names in file order")
env = dict(line.split("=", 1) for line in cli("dashboard-env", vps_yml, "orbit").stdout.splitlines())
check((env["ACCOUNT"], env["PORT"], env["LANE"], env["BRIDGE_ACCOUNT"], env["APP_ID"], env["APP_KEY_FILE"]) == ("vibe-dashboard-orbit", "8766", "orbit-ci", "", "1000003", "/etc/orbit-ci/app.pem"),
      "dashboard-env prints a dashboard's account, port, lane, bridge and its lane's App")
check(json.loads(cli("dashboard-config", example_yml, "acme").stdout)["telemetry_file"] == "/var/lib/vibe-dashboard/acme/telemetry.json", "dashboard-config prints dashboard.json")
check(json.loads(cli("dashboard-collector", example_yml, "greenbauer").stdout)["host"]["codex_home"] == "/var/lib/greenbauer-ci/codex", "dashboard-collector prints collector.json")
out = cli("dashboard-env", example_yml, "nope")
check(out.returncode == 3 and "no dashboard named 'nope'" in out.stderr, "an unknown dashboard exits 3")
out = cli("env", example_yml, "nope")
check(out.returncode == 3 and "no lane named 'nope'" in out.stderr, "an unknown lane exits 3")
bad = TMP / "bad.yml"
bad.write_text(yaml.safe_dump({"hostname": "x"}), encoding="utf-8")
out = cli("lanes", str(bad))
check(out.returncode == 3 and "must declare exactly" in out.stderr, "an invalid file exits 3 with the reason")
check(cli("bogus").returncode == 2, "an unknown command exits 2")

lane_fixtures.finish("test_lanes")
