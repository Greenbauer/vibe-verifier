#!/usr/bin/env python3
"""A machine's lane configuration, hosts/<host>.yml, as the bash scripts read it.

lane_load.py parses and validates the file into the values of lane_model.py, and lane_render.py
renders what each command below prints; every value a script gets has passed that validation, so
none of them parses YAML or re-checks a field.

  lanes.py lanes <host.yml>                    the lane names, one per line
  lanes.py host-env <host.yml>                 the machine's settings, KEY=value lines
  lanes.py env <host.yml> <lane>               one lane's settings, KEY=value lines
  lanes.py listener-config <host.yml> <lane> [--key-file PATH] [--codex-store PATH]
                                               the lane's scale-set listener config (JSON)
  lanes.py preload <host.yml> <lane>           the lane's preload map, "<repo> <dir>" lines, sorted
  lanes.py dashboards <host.yml>               the Vibe Verifier dashboard names, one per line
  lanes.py dashboard-env <host.yml> <name>     one dashboard's settings, KEY=value lines
  lanes.py dashboard-config <host.yml> <name>  its dashboard.json
  lanes.py dashboard-collector <host.yml> <name>
                                               its telemetry collector's local-mode collector.json

An invalid file exits 3 with the reason on stderr; bad usage exits 2.
"""

from __future__ import annotations

import argparse
import sys

from lane_load import load_host
from lane_render import (
    collector_config,
    dashboard_config,
    dashboard_settings,
    host_settings,
    lane_settings,
    listener_config,
)


def _settings_text(values: dict[str, object]) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


# What each command prints, from the validated host and the command's parsed arguments.
COMMANDS = {
    "lanes": lambda host, args: "".join(f"{lane.name}\n" for lane in host.lanes),
    "host-env": lambda host, args: _settings_text(host_settings(host)),
    "env": lambda host, args: _settings_text(lane_settings(host, host.lane(args.lane))),
    "preload": lambda host, args: host.lane(args.lane).preload_text,
    "dashboards": lambda host, args: "".join(f"{dashboard.name}\n" for dashboard in host.dashboards),
    "dashboard-env": lambda host, args: _settings_text(dashboard_settings(host.dashboard(args.dashboard))),
    "dashboard-config": lambda host, args: dashboard_config(host.dashboard(args.dashboard)),
    "dashboard-collector": lambda host, args: collector_config(host, host.dashboard(args.dashboard)),
    "listener-config": lambda host, args: listener_config(host.lane(args.lane), key_file=args.key_file, codex_store=args.codex_store),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lanes.py", description="Read a machine's lane configuration.")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("lanes", "host-env", "dashboards"):
        sub.add_parser(command).add_argument("host_yml")
    for command in ("env", "preload"):
        p = sub.add_parser(command)
        p.add_argument("host_yml")
        p.add_argument("lane")
    for command in ("dashboard-env", "dashboard-config", "dashboard-collector"):
        p = sub.add_parser(command)
        p.add_argument("host_yml")
        p.add_argument("dashboard")
    p = sub.add_parser("listener-config")
    p.add_argument("host_yml")
    p.add_argument("lane")
    p.add_argument("--key-file")
    p.add_argument("--codex-store")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        out = COMMANDS[args.command](load_host(args.host_yml), args)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{args.host_yml}: {exc}\n")
        return 3
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
