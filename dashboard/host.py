"""Root helpers that keep a Linux dashboard host's private inputs fresh.

Linux-only (Ubuntu 24.04, systemd): bin/vibe-dashboard-host runs these as root from a root-owned
checkout, on the timers in dashboard/systemd/. The web service never runs this module, and never
reads the App key or the collector's raw state. Runbook: docs/dashboard-host.md.

- token: mint a GitHub App installation token narrowed to the dashboard's repositories and to read
  permissions, and write it as the dashboard user's gh hosts.yml.
- telemetry: sample this machine with the collector's local mode into root-only state, then publish
  the dashboard's telemetry file without the raw sample history.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pwd
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import collector
from .config import load_config
from .telemetry import read_telemetry

READ_PERMISSIONS = dict.fromkeys(
    ("actions", "checks", "contents", "metadata", "pull_requests", "statuses"), "read")
ACCESS_TOKENS = "https://api.github.com/app/installations/%d/access_tokens"
# A client ID (Iv1.0123abcd, Iv23li...) or a numeric App ID; GitHub accepts either as the JWT issuer.
APP = re.compile(r"[A-Za-z0-9.]{1,64}\Z")
# A GitHub login, an App's ending in [bot]. Both it and the token are written into YAML unquoted.
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?\Z")
# An installation token: ghs_ plus 36 characters before 2026, and since then about 390 characters that
# also hold "." and "-" (measured 2026-10-06). Neither breaks a plain YAML scalar after a first
# character that cannot start a YAML sequence or other syntax.
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,4095}\Z")
MAX_RESPONSE = 1024 * 1024
# The owner of the trusted paths. Tests, which cannot run as root, substitute their own uid.
ROOT_UID = 0


class HostError(RuntimeError):
    """A failure safe to print: never a token, a key, or a response body."""


def _account(user: str) -> tuple[int, int]:
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        raise HostError("no such user: %s" % user) from None
    return account.pw_uid, account.pw_gid


def _root_secret(path: Path) -> None:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o077:
        raise HostError("%s must be a regular file only root can read" % path)


def publish(path: Path, data: bytes, owner: tuple[int, int], validate=None) -> None:
    """Atomically replace path with data, mode 0600, owned by owner (the dashboard user).

    The directory must be root's and writable by no one else, so the reader cannot swap the file
    between root writing and renaming it; mode and owner are set on the open descriptor, never by
    path. validate, when given, sees the finished draft while root still owns it (the dashboard's
    readers accept only files their own user owns) and must return true, or the old file stays."""
    directory = path.parent
    info = os.stat(directory, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
        raise HostError("%s must be a directory only root can write" % directory)
    fd, draft = tempfile.mkstemp(prefix="." + path.name + ".", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        if validate is not None and not validate(Path(draft)):
            raise HostError("refusing to publish an invalid %s; the previous file stays" % path.name)
        os.fchown(fd, *owner)
        os.replace(draft, path)
    except BaseException:
        os.unlink(draft)
        raise
    finally:
        os.close(fd)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app: str, key: Path, now: int | None = None) -> str:
    """An RS256 App JWT that openssl signs, so the private key never enters this process. The claims
    match the previous deploy tooling: issued a minute early for clock drift, expiring inside
    GitHub's ten-minute limit."""
    now = int(time.time()) if now is None else now
    claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": int(app) if app.isdigit() else app}
    signed = _b64(b'{"alg":"RS256","typ":"JWT"}') + "." + _b64(json.dumps(claims, separators=(",", ":")).encode())
    try:
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=signed.encode(),
                                   capture_output=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        raise HostError("openssl could not sign the App JWT") from None
    return signed + "." + _b64(signature)


def mint(jwt: str, installation: int, scope: dict, opener=urllib.request.urlopen) -> object:
    """POST the narrowing scope for an installation access token; the parsed JSON response."""
    request = urllib.request.Request(ACCESS_TOKENS % installation, data=json.dumps(scope).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json", "User-Agent": "vibe-dashboard-host",
                                              "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with opener(request, timeout=15) as response:
            return json.loads(response.read(MAX_RESPONSE + 1))
    except urllib.error.HTTPError as error:
        error.close()
        raise HostError("GitHub refused the token exchange (HTTP %d)" % error.code) from None
    except (OSError, ValueError):
        raise HostError("the GitHub token exchange is unavailable") from None


def granted_token(data: object, repositories: tuple[str, ...]) -> str:
    """The token, only when GitHub granted exactly the requested read permissions on exactly the
    configured repositories. Anything wider, narrower or missing fails closed."""
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not TOKEN.fullmatch(data["token"]):
        raise HostError("GitHub returned no usable token")
    if data.get("permissions") != READ_PERMISSIONS:
        raise HostError("GitHub granted permissions other than the requested reads")
    granted = data.get("repositories")
    if (not isinstance(granted, list) or not all(isinstance(row, dict) for row in granted)
            or sorted(str(row.get("full_name")).casefold() for row in granted)
            != sorted(name.casefold() for name in repositories)):
        raise HostError("GitHub scoped the token to repositories other than the configured ones")
    return data["token"]


def hosts_yml(token: str, login: str) -> str:
    return "github.com:\n    oauth_token: %s\n    user: %s\n    git_protocol: https\n" % (token, login)


def refresh_token(config_path: str, user: str, app: str, installation: int, key: Path, login: str,
                  output: Path, opener=urllib.request.urlopen) -> None:
    """Mint the dashboard's read token from its own repository list and write its gh hosts.yml."""
    if not APP.fullmatch(app) or not LOGIN.fullmatch(login) or installation < 1:
        raise HostError("the App, installation or login is not a GitHub identifier")
    owner = _account(user)
    config = load_config(config_path, uid=owner[0])
    _root_secret(key)
    scope = {"repositories": [name.split("/", 1)[1] for name in config.repositories],
             "permissions": READ_PERMISSIONS}
    token = granted_token(mint(app_jwt(app, key), installation, scope, opener), config.repositories)
    publish(output, hosts_yml(token, login).encode(), owner)


def refresh_telemetry(config_path: str, user: str, collector_path: str, state: Path) -> bool:
    """Collect into root-only state, then publish the dashboard's telemetry file. False when the
    sample failed; the published file then still carries the stale status the collector recorded."""
    owner = _account(user)
    config = load_config(config_path, uid=owner[0])
    if config.telemetry_file is None:
        raise HostError("the dashboard configuration names no telemetry_file")
    collected = collector.refresh(collector.load_config(collector_path), state)
    snapshot = json.loads(state.read_text(encoding="utf-8"))
    # The state keeps seven days of raw samples (20,160 at a 30-second interval, about 3.8 MiB). The
    # dashboard never reads them, and it refuses a telemetry file over 2 MiB, which the full history
    # passes after about three and a half days, so only the current sections are published.
    published = json.dumps({**snapshot, "samples": []}, separators=(",", ":"), sort_keys=True) + "\n"

    def readable(draft: Path) -> bool:
        return read_telemetry(dataclasses.replace(config, telemetry_file=draft)).get("available") is True

    publish(config.telemetry_file, published.encode(), owner, validate=readable)
    return collected
