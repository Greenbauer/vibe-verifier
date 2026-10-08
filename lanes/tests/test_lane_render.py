#!/usr/bin/env python3
"""Tests for lib/lane_render.py: the listener's config, and a dashboard's files and settings."""

import json

import lane_fixtures
from lane_fixtures import DASH, ORG, ROOT, USER, WAIT, check, host_file, without
from lane_load import load_host
from lane_render import collector_config, dashboard_config, dashboard_settings, listener_config

org, user = load_host(host_file(ORG, USER)).lanes
short = load_host(host_file({**ORG, "warm_max_age_sec": 600})).lanes[0]
uwarm = load_host(host_file({**USER, "warm_max_age_sec": 900})).lanes[0]
ci_only = load_host(host_file(without({**ORG, "labels": {"ci": "box-ci"}}, "codex_store", "qae_concurrency"))).lanes[0]
waiting = load_host(host_file({**ORG, "wait": WAIT})).lanes[0]
three = load_host(host_file({**USER, "qae_concurrency": 3})).lanes[0]

# ---- the listener config ----------------------------------------------------------------------------
check(json.loads(listener_config(short))["warm_max_age_sec"] == 600, "a declared warm_max_age_sec reaches the listener's config")
check(json.loads(listener_config(uwarm))["warm_max_age_sec"] == 900, "a user lane's inert warm_max_age_sec still reaches the listener's config")
oc = json.loads(listener_config(org))
check((oc["scope"], oc["github_url"], oc["runner_group"]) == ("org", "https://github.com/example", "box-ci"), "an org lane's listener serves its runner group")
check(oc["kinds"]["ci"] == {"set_name": "box-ci", "labels": ["self-hosted", "box-ci"]}, "an org ci set carries self-hosted and its label, so runs-on: [self-hosted, <label>] matches")
check(oc["kinds"]["qae"] == {"set_name": "box-qae", "labels": ["box-qae"], "requires_files": ["/var/lib/box-ci/codex/auth.json"]}, "an org qae set: its label alone, so a bare self-hosted job never lands on the Codex login, and no slot until that login exists")
check(oc["budget"] == {"slots": 3, "min_runners": 2, "qae_concurrency": 1} and "repos" not in oc, "an org lane's budget carries its warm pool and QAE concurrency, and no repository loop")
check(oc["app"] == {"client_id": "123456", "installation_id": 7654321, "key_file": "/etc/box-ci/app.pem"}, "the App ids come from the lane block")
wc = json.loads(listener_config(waiting))
check(wc["kinds"]["wait"] == {"set_name": "box-wait", "labels": ["self-hosted", "box-wait"], "slots": 5, "container_memory_bytes": 1024**3},
      "an org wait set carries self-hosted and its label, and its own budget and container size, in that key order")
check(wc["budget"] == oc["budget"] and "slots" not in wc["kinds"]["ci"] and wc["admission"] == oc["admission"], "the wait kind leaves budget.slots and the ci admission as they were")
check("wait" not in oc["kinds"], "a lane without a wait block has no wait kind in its listener config")
uc = json.loads(listener_config(user))
check(uc["kinds"]["ci"]["labels"] == ["own-ci"] and uc["kinds"]["qae"]["labels"] == ["own-qae"], "a user lane's sets carry their one label each, so a bare self-hosted job matches neither")
check(uc["repos"] == {"exclude": ["fleet"], "refresh_sec": 600} and uc["runner_group"] == "default" and uc["budget"]["min_runners"] == 0, "a user lane lists its installation every 10 minutes")
check("qae" not in json.loads(listener_config(ci_only))["kinds"] and json.loads(listener_config(ci_only))["budget"]["qae_concurrency"] == 0, "a ci-only lane's listener has no qae kind")
moved = json.loads(listener_config(user, key_file="/k/app.pem", codex_store="/k/codex"))
check((moved["app"]["key_file"], moved["kinds"]["qae"]["requires_files"]) == ("/k/app.pem", ["/k/codex/auth.json"]), "the provisioner's paths reach the config and nothing else changes")
check(json.loads(listener_config(three))["kinds"]["qae"]["requires_files"] == ["/var/lib/own-ci/codex/auth.json", "/var/lib/own-ci/codex-2/auth.json", "/var/lib/own-ci/codex-3/auth.json"],
      "the listener waits for each qae instance's own store's login, the n-th file for instance n")
check(json.loads(listener_config(three, codex_store="/k/codex"))["kinds"]["qae"]["requires_files"] == ["/k/codex/auth.json", "/k/codex-2/auth.json", "/k/codex-3/auth.json"],
      "the provisioner's store path names every instance's store")

# ---- a dashboard's files and settings ---------------------------------------------------------------
dhost = load_host(host_file(ORG, USER, top={"dashboards": [DASH, {**without(DASH, "bridge_account"), "name": "own", "lane": "own-ci", "port": 8766, "config": {"version": 1, "owner": "someone"}}]}))
box, own = dhost.dashboards
check(json.loads(dashboard_config(box)) == {**DASH["config"], "telemetry_file": "/var/lib/vibe-dashboard/box/telemetry.json"}, "dashboard.json is the declared config plus this machine's telemetry file")
every = load_host(host_file(ORG, top={"dashboards": [{**DASH, "config": {**DASH["config"], "repositories": "all"}}]})).dashboards[0]
check(json.loads(dashboard_config(every))["repositories"] == "all", "repositories: all is written through as the string")
check(json.loads(collector_config(dhost, box)) == {"version": 1, "owner": "Example", "host": {
    "label": "box-1 box-ci", "listener_config_path": "/etc/box-ci/listener.json", "lane_name": "box-ci",
    "workspace_path": "/var/lib/box-ci", "codex_home": "/var/lib/box-ci/codex"}},
    "the collector samples the lane's listener and state filesystem, and the first QAE store's login")
ci_dash = load_host(host_file({**without(ORG, "codex_store", "qae_concurrency"), "labels": {"ci": "box-ci"}}, top={"dashboards": [DASH]})).dashboards[0]
check(json.loads(collector_config(dhost, ci_dash))["host"]["codex_home"] == "/nonexistent", "a lane without QAE has no Codex login: its quota reads from a path that never holds one")
check(dashboard_settings(box) == {"NAME": "box", "ACCOUNT": "vibe-dashboard-box", "PORT": 8765, "LANE": "box-ci", "BRIDGE_ACCOUNT": "box-forward",
                                  "APP_ID": 123456, "APP_INSTALLATION_ID": 7654321, "APP_KEY_FILE": "/etc/box-ci/app.pem", "APP_LOGIN": "example-bot"},
      "the provisioner's settings: the dashboard's names and its lane's App")

lane_fixtures.finish("test_lane_render")
