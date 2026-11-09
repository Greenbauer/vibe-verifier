"""The field checks lane_load.py applies to a host file's values: each returns the value, or raises
ValueError naming the field and what it must be.
"""

from __future__ import annotations

import re

# An absolute path made of plain components: no spaces, nothing a unit file or a shell line could
# misread. A `..` component is refused separately.
ABS_PATH = re.compile(r"^(?:/[A-Za-z0-9._-]+)+$")


def _positive_int(value: object, field: str, block: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{block}.{field} must be a positive integer")
    return value


def _int_between(value: object, low: int, high: int, field: str, block: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{block}.{field} must be an integer in {low}..{high}")
    return value


def _matching(value: object, pattern: re.Pattern[str], field: str, block: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{block}.{field} must match {pattern.pattern}")
    return value


def _absolute_path(value: object, field: str, block: str) -> str:
    path = _matching(value, ABS_PATH, field, block)
    if ".." in path.split("/"):
        raise ValueError(f"{block}.{field} must not contain a .. component")
    return path


def _exact_keys(value: object, keys: set[str], field: str, block: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{block}.{field} must declare exactly {', '.join(sorted(keys))}")
    return value


STORE_FILESYSTEMS = ("xfs", "btrfs")
# An https origin with an optional path, and no query, credentials or whitespace: the provisioner
# writes it into a systemd ExecStart= line as one word.
NPM_REGISTRY = re.compile(r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*/?$")


def _store_fs(value: object, block: str) -> str:
    """store_fs (xfs when absent): the filesystem bin/provision-lane.sh makes a new store with.

    The slot helper follows the store it finds, not this key, so the key never changes how a lane
    that is running treats its store; converting one is an explicit command (README.md).
    """
    if not isinstance(value, str) or value not in STORE_FILESYSTEMS:
        raise ValueError(f"{block}.store_fs must be one of {', '.join(STORE_FILESYSTEMS)} (xfs when absent)")
    return value


def _npm_registry(value: object, block: str) -> str | None:
    """npm_registry without its trailing slash, or None when absent (README.md, "The npm registry")."""
    if value is None:
        return None
    if not isinstance(value, str) or not NPM_REGISTRY.match(value):
        raise ValueError(f"{block}.npm_registry must be an https URL with no query, credentials or spaces")
    return value.rstrip("/")
