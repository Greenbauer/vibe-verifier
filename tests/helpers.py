"""Black-box helpers: every test drives a gate or the runner through its command line,
because the command line is the contract."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_VARIABLES = ("GITHUB_BASE_REF", "GITHUB_HEAD_REF", "GITHUB_REF_NAME", "GITHUB_REF_TYPE",
                "VIBE_VERIFIER_BASE_REF", "GITHUB_STEP_SUMMARY")


def clean_env(extra=None):
    """CI sets these for the repository under test; a temp repo must not inherit them."""
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


def gate(name, repo, *args, env=None):
    script = ROOT / "gates" / (name.replace("-", "_") + ".py")
    return subprocess.run([sys.executable, str(script), "--repo", str(repo), *args],
                          capture_output=True, text=True, env=clean_env(env))


def runner(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "bin" / "vibe-verifier"), *args],
                          capture_output=True, text=True, env=clean_env(env))
