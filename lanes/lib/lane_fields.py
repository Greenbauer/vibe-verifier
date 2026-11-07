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
