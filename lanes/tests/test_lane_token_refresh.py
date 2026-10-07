#!/usr/bin/env python3
"""Tests for bin/lane-token-refresh.py: one installation token from a lane's key file into its user's hosts.yml.

The network, the JWT and os.chown are stubbed; file sinks point at a tempdir. Loads the script by path
(main() is __main__-guarded).
"""
import importlib.util
import os
import pwd
import stat
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "bin/lane-token-refresh.py"))
_SPEC = importlib.util.spec_from_file_location("lane_token_refresh", _SCRIPT)
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)

passed = failed = 0


def check(desc: str, cond: bool) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✓ {desc}")
    else:
        failed += 1
        print(f"✗ {desc}")


def run(argv, *, pem="-----BEGIN FAKE KEY-----\n"):
    """main(argv) with the key file, mint, JWT and chown stubbed; returns what happened."""
    tmp = tempfile.mkdtemp(prefix="lanetok-")
    key = os.path.join(tmp, "app.pem")
    with open(key, "w") as fh:
        fh.write(pem)
    os.mkdir(os.path.join(tmp, "home"))  # the lane user's home exists; .config/gh below it does not
    hosts = os.path.join(tmp, "home", ".config", "gh", "hosts.yml")
    argv = [a.replace("{key}", key).replace("{hosts}", hosts) for a in argv]
    seen = {"jwt": [], "mint": [], "chown": []}

    def fake_jwt(app_id, pem_text):
        seen["jwt"].append((app_id, pem_text))
        return "fake.jwt.token"

    def fake_mint(jwt_token, install_id):
        seen["mint"].append((jwt_token, install_id))
        return {"token": f"ghs_fake_{install_id}", "expires_at": "2026-01-01T00:00:00Z"}

    saved = (m.make_jwt, m.mint, m.os.chown)
    m.make_jwt, m.mint = fake_jwt, fake_mint
    m.os.chown = lambda path, uid, gid: seen["chown"].append((path, uid, gid))
    try:
        try:
            rc = m.main(argv)
        except SystemExit as exc:
            rc = exc.code
    finally:
        m.make_jwt, m.mint, m.os.chown = saved
    return rc, hosts, seen


me = pwd.getpwuid(os.getuid())
rc, hosts, seen = run(["--app-id", "234567", "--installation-id", "8765432", "--key-file", "{key}",
                       "--hosts-out", "{hosts}", "--owner", me.pw_name, "--gh-user", "lane-bot"])
check("a refresh exits 0", rc == 0)
check("the JWT is made from --app-id and the key file's contents", seen["jwt"] == [("234567", "-----BEGIN FAKE KEY-----\n")])
check("one installation token is minted, for --installation-id", seen["mint"] == [("fake.jwt.token", "8765432")])
expected = (
    "github.com:\n    users:\n        lane-bot:\n            oauth_token: ghs_fake_8765432\n"
    "    user: lane-bot\n    git_protocol: https\n    oauth_token: ghs_fake_8765432\n"
)
check("--hosts-out holds gh's hosts.yml shape, labelled with --gh-user", os.path.isfile(hosts) and open(hosts).read() == expected)
check("--hosts-out is 0600", os.path.isfile(hosts) and stat.S_IMODE(os.stat(hosts).st_mode) == 0o600)
gh_dir, config_dir = os.path.dirname(hosts), os.path.dirname(os.path.dirname(hosts))
home = os.path.dirname(config_dir)
check("the directories it creates are 0700", all(stat.S_IMODE(os.stat(d).st_mode) == 0o700 for d in (config_dir, gh_dir)))
chowned = [path for path, uid, gid in seen["chown"] if (uid, gid) == (me.pw_uid, me.pw_gid)]
check("every created directory and the file are chowned to --owner, the file before its rename",
      len(chowned) == len(seen["chown"]) == 3 and chowned[:2] == [config_dir, gh_dir]
      and os.path.dirname(chowned[2]) == gh_dir and os.path.basename(chowned[2]).startswith(".hosts.yml."))
check("a directory that already existed is not chowned", home not in chowned)
check("the write is atomic: no temp file is left beside hosts.yml", os.listdir(gh_dir) == ["hosts.yml"])

# A second run rewrites the file in place; the existing directories are not touched again.
rc, _hosts, seen = run(["--app-id", "234567", "--installation-id", "42", "--key-file", "{key}", "--hosts-out", hosts,
                        "--owner", me.pw_name, "--gh-user", "lane-bot"])
check("a refresh rewrites the existing file with the new token", rc == 0 and "ghs_fake_42" in open(hosts).read())
check("a refresh chowns only the new file", len(seen["chown"]) == 1)

# Refusals: every input is a flag, and none has a fallback.
rc, _hosts, seen = run(["--app-id", "234567", "--key-file", "{key}"])
check("partial arguments are refused (argparse exit 2) before any mint", rc == 2 and not seen["mint"])
full = ["--app-id", "234567", "--installation-id", "42", "--key-file", "{key}", "--hosts-out", "{hosts}", "--owner", me.pw_name]
rc, missing_hosts, seen = run(full)
check("without --gh-user it is refused (no fallback label) before any mint or write", rc == 2 and not seen["mint"] and not os.path.exists(missing_hosts))
for bad in ("lane bot", "lane-bot\n    oauth_token: x", "-lane"):
    rc, missing_hosts, seen = run(full + ["--gh-user", bad])
    check(f"a --gh-user that is not a GitHub login ({bad!r}) is refused before any mint or write",
          rc == 2 and not seen["mint"] and not os.path.exists(missing_hosts))
rc, missing_hosts, seen = run(["--app-id", "234567", "--installation-id", "42", "--key-file", "{key}", "--hosts-out", "{hosts}",
                               "--owner", "no-such-user-for-this-test", "--gh-user", "lane-bot"])
check("an unknown --owner is refused before any mint or write", rc == 2 and not seen["mint"] and not os.path.exists(missing_hosts))
check("an App login with [bot] is a login", m.GH_LOGIN.fullmatch("lane-bot[bot]") is not None)
check("every input is a flag: the script reads no environment", "environ" not in open(_SCRIPT).read())

print(f"test_lane_token_refresh: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
