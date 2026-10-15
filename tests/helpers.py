"""Black-box helpers: every test drives a gate or the runner through its command line,
because the command line is the contract."""
import binascii
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_VARIABLES = ("GITHUB_BASE_REF", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_REF_TYPE",
                "VIBE_VERIFIER_BASE_REF", "GITHUB_STEP_SUMMARY", "VIBE_VERIFIER_MANIFEST", "VIBE_VERIFIER_QUALIFIER")


def clean_env(extra=None):
    """CI sets these for the repository under test, and the runner sets the last two for the gate that runs
    this suite (coverage-not-lost, regression-test-fails-on-base); a temp repo, or a gate a test runs, must
    not inherit them."""
    env = {key: value for key, value in os.environ.items() if key not in CI_VARIABLES}
    env.update(extra or {})
    return env


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=clean_env())


def write(repo, files):
    for relative, text in files.items():
        path = Path(repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit(repo, files, message="change"):
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_repo(test, files, initial_branch="main"):
    repo = tempfile.mkdtemp(prefix="vv-")
    test.addCleanup(shutil.rmtree, repo, True)
    git(repo, "init", "-q", "-b", initial_branch)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, files, "base")
    return repo


def install_fixture(deps):
    """`npm ci` of a fixture's package.json and lockfile, copied into `deps`, and proof that the Vitest it installed loads.

    npm exits 0 when the download of an optional package fails and installs the rest without a word.
    Vite's native binding is one (a package per platform), and without it Vitest does not load: a CI
    run on 2026-10-14 got that install and failed every test of the class. A second `npm ci` fetches
    what the first left out; a Vitest that still does not load fails the class with the reason."""
    for _ in range(2):
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=deps, check=True,
                       capture_output=True, text=True)
        loads = subprocess.run(["node", "--input-type=module", "--eval", 'await import("vitest/node");'], cwd=deps,
                               capture_output=True, text=True)
        if loads.returncode == 0:
            return
    raise RuntimeError("npm ci exited 0 twice and the fixture's Vitest still does not load:\n" + loads.stderr)


def gate(name, repo, *args, env=None):
    script = ROOT / "gates" / (name.replace("-", "_") + ".py")
    return subprocess.run([sys.executable, str(script), "--repo", str(repo), *args],
                          capture_output=True, text=True, env=clean_env(env))


def runner(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "bin" / "vibe-verifier"), *args],
                          capture_output=True, text=True, env=clean_env(env))


def png(width, height=1):
    """A valid, blank RGB PNG of this size, as bytes: what a browser screenshot or a design export is."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    rows = (b"\x00" + b"\x00" * 3 * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
