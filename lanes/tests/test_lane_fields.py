#!/usr/bin/env python3
"""Tests for lib/lane_fields.py: each field check returns the value it accepts and names the field it refuses."""

import re

import lane_fixtures  # noqa: F401  (puts lib/ on the import path)
from lane_fields import _absolute_path, _exact_keys, _int_between, _matching, _positive_int
from lane_fixtures import check


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
