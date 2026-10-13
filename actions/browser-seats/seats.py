#!/usr/bin/env python3
"""Check a preview's seats and hand them to a parallel browser suite (README.md, "The contract").

Reads a JSON list of seats, seat 0 first. A seat is an object of role -> {"email", "password"}, and
every seat names the same roles. Writes two step outputs: `workers`, the number of seats (one
browser-suite worker per seat), and `extra-seats`, seats 1 and up as compact JSON, the value a suite
reads to give worker k seat k. Every password, and the extra-seats value when it holds any, is
masked before anything else is written. A malformed list fails: a suite handed a broken seat would
put two workers on one account, which is the collision seats exist to prevent.

    python3 seats.py --file seats.json [--email-domain example.test] [--max-seats 8]
"""

from __future__ import annotations

import argparse
import json
import os
import sys


class SeatError(ValueError):
    """A seat list a suite must not run on; the message names the seat and never a password."""


def check_login(number: int, role: str, login: object, email_domain: str) -> str:
    """The login's email, lower-cased, once it has an email and a password on the domain."""
    email = login.get("email") if isinstance(login, dict) else None
    password = login.get("password") if isinstance(login, dict) else None
    if not isinstance(email, str) or not email.strip():
        raise SeatError(f"seat {number}'s {role} has no email")
    if not isinstance(password, str) or not password:
        raise SeatError(f"seat {number}'s {role} has no password")
    if email_domain and not email.lower().endswith("@" + email_domain.lower()):
        raise SeatError(f"seat {number}'s {role} is not an @{email_domain} login")
    return email.strip().lower()


def check(seats: object, email_domain: str = "", max_seats: int = 8) -> list[dict]:
    """The seats, unchanged, once every rule in the README's contract holds."""
    if not isinstance(seats, list) or not seats:
        raise SeatError("the seats must be a non-empty JSON list, seat 0 first")
    if len(seats) > max_seats:
        raise SeatError(f"{len(seats)} seats, over the {max_seats} allowed (max-seats)")
    if not all(isinstance(seat, dict) and seat for seat in seats):
        raise SeatError("every seat must be an object of role -> login")
    roles = sorted(seats[0])
    owner: dict[str, int] = {}
    for number, seat in enumerate(seats):
        if sorted(seat) != roles:
            raise SeatError(f"seat {number} has roles {sorted(seat)}, seat 0 has {roles}")
        for role, login in seat.items():
            email = check_login(number, role, login, email_domain)
            if email in owner:
                raise SeatError(f"seat {number}'s {role} is the same login as seat {owner[email]}'s")
            owner[email] = number
    return seats


def outputs(seats: list[dict]) -> tuple[list[str], dict[str, str]]:
    """The values to mask, and the step outputs."""
    extra = json.dumps(seats[1:], separators=(",", ":"))
    masks = [login["password"] for seat in seats for login in seat.values()]
    if seats[1:]:
        masks.append(extra)
    return masks, {"workers": str(len(seats)), "extra-seats": extra}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", required=True, help="the JSON list of seats, seat 0 first")
    parser.add_argument("--email-domain", default="", help="every login must be on this domain")
    parser.add_argument("--max-seats", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        with open(args.file, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        print("::error::the seats file is missing or is not JSON")
        return 1
    try:
        seats = check(raw, args.email_domain, args.max_seats)
    except SeatError as error:
        print(f"::error::{error}")
        return 1
    masks, values = outputs(seats)
    for value in masks:
        print(f"::add-mask::{value}", flush=True)
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)
    print(f"{values['workers']} seat(s): one browser-suite worker each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
