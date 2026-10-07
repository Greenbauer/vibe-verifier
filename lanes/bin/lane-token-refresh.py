#!/usr/bin/env python3
"""Refresh a lane user's GitHub App installation token from the lane's root-only App key.

  lane-token-refresh.py --app-id N --installation-id N --key-file /etc/<lane>/app.pem \
      --hosts-out <lane home>/.config/gh/hosts.yml --owner <lane user> --gh-user <App login>

mints ONE installation token (an hour's life) and writes it to --hosts-out as gh's hosts.yml,
atomically, 0600, owned by --owner (and any directory it has to create, 0700, too), under the
login --gh-user. Every input is a flag. <lane>-token-refresh.timer (bin/provision-lane.sh) runs it
as root every 10 minutes, which leaves the token's hour ample margin for a missed run. Needs
python3-jwt and python3-cryptography (bin/provision-host.sh installs both).
"""
import argparse
import json
import os
import pwd
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request


def make_jwt(app_id: str, pem: str) -> str:
    import jwt as pyjwt  # lazy: keeps the module importable (e.g. in CI/tests) without PyJWT installed
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": int(app_id)}
    tok = pyjwt.encode(payload, pem, algorithm="RS256")
    return tok.decode() if isinstance(tok, bytes) else tok


def mint(jwt_token: str, install_id: str) -> dict:
    """Exchange the App JWT for an installation access token. Raises urllib HTTPError on failure."""
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{install_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "runners-lane-token-refresh",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def make_dirs(d: str, owner: tuple[int, int]) -> None:
    """Create d (0700: a token sink dir must not be world-listable); chown what it creates to owner."""
    missing = []
    while d and not os.path.isdir(d):
        missing.append(d)
        d = os.path.dirname(d)
    for path in reversed(missing):
        os.mkdir(path, 0o700)
        os.chown(path, *owner)


def write_atomic(path: str, content: str, owner: tuple[int, int]) -> None:
    d = os.path.dirname(path)
    make_dirs(d, owner)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.chown(tmp, *owner)  # before the rename, so the path never names a file its reader cannot open
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def hosts_yml(install_token: str, user: str) -> str:
    return (
        "github.com:\n"
        "    users:\n"
        f"        {user}:\n"
        f"            oauth_token: {install_token}\n"
        f"    user: {user}\n"
        "    git_protocol: https\n"
        f"    oauth_token: {install_token}\n"
    )


# A GitHub login (an App's login may end in [bot]); it is written into hosts.yml as a YAML key.
GH_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?$")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh a lane user's GitHub App installation token.")
    parser.add_argument("--app-id", type=int, required=True, help="the App's id")
    parser.add_argument("--installation-id", type=int, required=True, help="the installation to mint for")
    parser.add_argument("--key-file", required=True, help="the App private key (PEM)")
    parser.add_argument("--hosts-out", required=True, help="the gh hosts.yml to write")
    parser.add_argument("--owner", required=True, help="the user who owns --hosts-out")
    parser.add_argument("--gh-user", required=True, help="the App login hosts.yml records")
    args = parser.parse_args(argv)
    if not GH_LOGIN.fullmatch(args.gh_user):
        parser.error(f"--gh-user must be a GitHub login, got {args.gh_user!r}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        entry = pwd.getpwnam(args.owner)
    except KeyError:
        sys.stderr.write(f"no such user: {args.owner}\n")
        return 2
    with open(args.key_file) as f:
        pem = f.read()
    try:
        data = mint(make_jwt(str(args.app_id), pem), str(args.installation_id))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"github exchange failed: {e.code} {e.reason}\n")
        sys.stderr.write(e.read().decode("utf-8", "replace") + "\n")
        return 2
    write_atomic(args.hosts_out, hosts_yml(data["token"], args.gh_user), (entry.pw_uid, entry.pw_gid))
    print(f"OK: {args.hosts_out} refreshed for {args.owner} (token expires_at={data.get('expires_at', '?')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
