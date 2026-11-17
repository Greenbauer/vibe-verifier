#!/usr/bin/env python3
"""Tests for a host file's engine pin (lane_fields._engine): the catalog commit a machine runs."""

import lane_fixtures
from lane_fixtures import ORG, TMP, check, host_file, refused
from lane_load import load_host

SHA = "0123456789abcdef0123456789abcdef01234567"
check(load_host(host_file(ORG, top={"engine": SHA})).engine == SHA, "a host file may pin the catalog commit the machine runs")
check(load_host(host_file(ORG)).engine == "", "a host file with no pin loads with an empty engine")
for index, value in enumerate((SHA[:12], SHA.upper(), "main", "refs/heads/main", " " + SHA, 1234, True, [SHA])):
    refused(host_file(ORG, top={"engine": value}), "host.engine must be a full 40-character lowercase commit id",
            f"an engine pin that is not a full lowercase commit id is refused, case {index}")
digits = TMP / "digits.yml"
digits.write_text(host_file(ORG).read_text() + "engine: 1234567890123456789012345678901234567890\n", encoding="utf-8")
refused(digits, "quoted", "an unquoted all-digit pin, which YAML reads as a number, is refused with a hint to quote it")

lane_fixtures.finish("test_lane_engine")
