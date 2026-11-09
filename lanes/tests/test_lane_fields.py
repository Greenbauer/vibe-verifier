#!/usr/bin/env python3
"""Tests for lib/lane_fields.py: each field check returns the value it accepts and names the field it refuses."""

import re

import lane_fixtures  # noqa: F401  (puts lib/ on the import path)
from lane_fields import _absolute_path, _exact_keys, _int_between, _matching, _npm_registry, _positive_int, _store_fs
from lane_fixtures import ORG, check, host_file
from lane_load import load_host


def refusal(call):
    try:
        call()
    except ValueError as error:
        return str(error)
    return None


check(_positive_int(3, "slots", "lanes.box") == 3, "a positive integer is returned")
check(refusal(lambda: _positive_int(True, "slots", "lanes.box")) == "lanes.box.slots must be a positive integer", "a boolean is not an integer")
check(_int_between(5, 1, 5, "qae_concurrency", "lanes.box") == 5, "the upper bound is inside the range")
check(refusal(lambda: _int_between(6, 1, 5, "qae_concurrency", "lanes.box")) == "lanes.box.qae_concurrency must be an integer in 1..5", "above the range is refused with the range")
check(_matching("box-ci", re.compile(r"^[a-z-]+$"), "name", "lanes[0]") == "box-ci", "a matching string is returned")
check(refusal(lambda: _matching(7, re.compile(r"^[a-z-]+$"), "name", "lanes[0]")) == "lanes[0].name must match ^[a-z-]+$", "a non-string is refused with the pattern")
check(_absolute_path("/var/lib/box-ci/codex", "codex_store", "lanes.box") == "/var/lib/box-ci/codex", "a plain absolute path is returned")
check(refusal(lambda: _absolute_path("/var/lib/../etc", "codex_store", "lanes.box")) == "lanes.box.codex_store must not contain a .. component", "a .. component is refused")
check(refusal(lambda: _absolute_path("var/lib", "codex_store", "lanes.box")).startswith("lanes.box.codex_store must match"), "a relative path is refused")
check(_exact_keys({"a": 1, "b": 2}, {"a", "b"}, "app", "lanes.box") == {"a": 1, "b": 2}, "a map with exactly the keys is returned")
check(refusal(lambda: _exact_keys({"a": 1}, {"a", "b"}, "app", "lanes.box")) == "lanes.box.app must declare exactly a, b", "a missing key is refused, naming every key")

check(_store_fs("btrfs", "lanes.box") == "btrfs", "store_fs btrfs is returned")
check(refusal(lambda: _store_fs("ext4", "lanes.box")) == "lanes.box.store_fs must be one of xfs, btrfs (xfs when absent)", "another filesystem is refused")

NPM_REFUSAL = "lanes.box.npm_registry must be an https URL with no query, credentials or spaces"
check(_npm_registry(None, "lanes.box") is None, "an absent npm_registry leaves the package managers' default")
check([_npm_registry(url, "lanes.box") for url in ("https://registry.yarnpkg.com", "https://registry.yarnpkg.com/", "https://npm.example.com:8443/repository/npm/")]
      == ["https://registry.yarnpkg.com", "https://registry.yarnpkg.com", "https://npm.example.com:8443/repository/npm"],
      "npm_registry is kept without its trailing slash, with a port and a path when it has them")
for bad in ("http://registry.yarnpkg.com", "registry.yarnpkg.com", "https://user:pw@registry.example.com/", "https://registry.example.com/?x=1",
            "https://registry.example.com/ -e X=1", "https://registry.yarnpkg.com/\n", "", True, ["https://registry.yarnpkg.com"]):
    check(refusal(lambda: _npm_registry(bad, "lanes.box")) == NPM_REFUSAL, f"npm_registry {bad!r} is refused")
check(load_host(host_file({**ORG, "npm_registry": "https://registry.yarnpkg.com/"})).lanes[0].npm_registry == "https://registry.yarnpkg.com"
      and load_host(host_file(ORG)).lanes[0].npm_registry is None, "a host file's npm_registry reaches the lane; without it the lane has none")

lane_fixtures.finish("test_lane_fields")
