"""Pinned external tools a wired gate may depend on.

A gate that needs a binary the standard library cannot replace declares it here: one version,
one sha256 per platform, one release URL. Resolution order, all fail closed with CannotRun:

1. a binary of that name on PATH whose `version` output is exactly the pinned version;
2. the cached pinned binary under $VIBE_VERIFIER_TOOLS (default ~/.cache/vibe-verifier/tools);
3. a download of the pinned release asset, verified against its sha256 before anything is
   extracted, then cached. No network, an unsupported platform, or a digest that does not match
   is "could not run" (exit 2), never a pass.

The pin lives in the catalog, so a consumer takes a new tool version the way it takes a new gate:
by bumping its action pin.
"""
import hashlib
import io
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

from _contract import CannotRun

TOOLS = {
    "gitleaks": {
        "version": "8.30.1",
        "url": "https://github.com/gitleaks/gitleaks/releases/download/v{version}/{asset}",
        # from gitleaks_8.30.1_checksums.txt on the release, read 2026-09-21
        "assets": {
            ("Linux", "x86_64"): ("gitleaks_{version}_linux_x64.tar.gz",
                                  "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
            ("Darwin", "arm64"): ("gitleaks_{version}_darwin_arm64.tar.gz",
                                  "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
            ("Darwin", "x86_64"): ("gitleaks_{version}_darwin_x64.tar.gz",
                                   "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
        },
        "version_args": ["version"],
    },
    "actionlint": {
        "version": "1.7.12",
        "url": "https://github.com/rhysd/actionlint/releases/download/v{version}/{asset}",
        # from actionlint_1.7.12_checksums.txt on the release, read 2026-09-21
        "assets": {
            ("Linux", "x86_64"): ("actionlint_{version}_linux_amd64.tar.gz",
                                  "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"),
            ("Darwin", "arm64"): ("actionlint_{version}_darwin_arm64.tar.gz",
                                  "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f"),
            ("Darwin", "x86_64"): ("actionlint_{version}_darwin_amd64.tar.gz",
                                   "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644"),
        },
        "version_args": ["-version"],
    },
    "zizmor": {
        "version": "1.30.1",
        "url": "https://github.com/zizmorcore/zizmor/releases/download/v{version}/{asset}",
        # The release publishes no checksum file. These digests were computed on 2026-09-21 from the
        # release assets, each of which `gh attestation verify --owner zizmorcore` traced to
        # zizmorcore/zizmor at refs/tags/v1.30.1 before the digest was written down.
        "assets": {
            ("Linux", "x86_64"): ("zizmor-x86_64-unknown-linux-gnu.tar.gz",
                                  "e65324f4430c2717591937edcec90ccbefaf14c174f8ec9415e03ca875b46e1a"),
            ("Darwin", "arm64"): ("zizmor-aarch64-apple-darwin.tar.gz",
                                  "e28d22b087f9ebb8d99da6e740d348c930f559961c7c3f12badda54f882195a2"),
            ("Darwin", "x86_64"): ("zizmor-x86_64-apple-darwin.tar.gz",
                                   "10e6b18b11ea07e515a16f0f0518c7b07527bc9977c1fd5698181ce7f3554202"),
        },
        "version_args": ["--version"],
    },
    "ast-grep": {
        "version": "0.45.3",
        "url": "https://github.com/ast-grep/ast-grep/releases/download/{version}/{asset}",
        # The release publishes no checksum file and no attestation (gh attestation verify: 404). These
        # are the sha256 digests GitHub recorded for each release asset at upload (the API's `digest`),
        # read 2026-10-05 and matched by hashing each downloaded asset before they were written down.
        "assets": {
            ("Linux", "x86_64"): ("app-x86_64-unknown-linux-gnu.zip",
                                  "f8ac830881339d1edee6b2652f54798c0f4da5a827f2db38a08ee31117783ce8"),
            ("Darwin", "arm64"): ("app-aarch64-apple-darwin.zip",
                                  "6d2279dea5bea2ad79c66ea93f5fe54ba926e398a8a26de76c56db68fe59eac6"),
            ("Darwin", "x86_64"): ("app-x86_64-apple-darwin.zip",
                                   "b2ffd26f42810340326a9e8a084bdc3647a8795c1a3f21fc06bd7bef3c7c5b2c"),
        },
        "version_args": ["--version"],
    },
}


def cache_dir():
    return os.environ.get("VIBE_VERIFIER_TOOLS") or os.path.join(os.path.expanduser("~"), ".cache", "vibe-verifier", "tools")


def _reports_version(binary, tool):
    try:
        out = subprocess.run([binary, *tool["version_args"]], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    first = (out.stdout.strip() or out.stderr.strip()).splitlines()[0] if (out.stdout.strip() or out.stderr.strip()) else ""
    return out.returncode == 0 and re.search(r"(?<![0-9.])%s(?![0-9.])" % re.escape(tool["version"]), first) is not None


def _member(archive, asset, name):
    """The bytes of the file called `name` in a .zip or .tar.gz release asset, or None."""
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            member = next((m for m in bundle.infolist() if not m.is_dir() and os.path.basename(m.filename) == name), None)
            return bundle.read(member) if member else None
    with tarfile.open(fileobj=archive, mode="r:gz") as bundle:
        member = next((m for m in bundle.getmembers() if m.isfile() and os.path.basename(m.name) == name), None)
        return bundle.extractfile(member).read() if member else None


def _download(name, tool, target):
    key = (platform.system(), platform.machine())
    if key not in tool["assets"]:
        raise CannotRun("%s %s has no pinned build for %s %s; install it on PATH" % (name, tool["version"], *key))
    asset, digest = tool["assets"][key]
    asset = asset.format(version=tool["version"])
    url = tool["url"].format(version=tool["version"], asset=asset)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError as error:
        raise CannotRun("cannot write the tool cache %s: %s" % (os.path.dirname(target), error))
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
    except (OSError, ValueError) as error:
        raise CannotRun("could not fetch %s %s (%s): %s; install it on PATH or set VIBE_VERIFIER_TOOLS to a cache that has it"
                        % (name, tool["version"], url, error))
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise CannotRun("%s does not match its pinned sha256 (got %s, pinned %s); refusing to run it" % (asset, actual, digest))
    payload = _member(io.BytesIO(data), asset, name)
    if payload is None:
        raise CannotRun("%s holds no file named %s" % (asset, name))
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(target))
    with os.fdopen(handle, "wb") as out:
        out.write(payload)
    os.chmod(temp, os.stat(temp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temp, target)


NODE_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


def ensure_node_tool(name):
    """The install directory of a node toolchain pinned by tools/<name>/package-lock.json.

    Installed once per lockfile digest into the cache with `npm ci --ignore-scripts`, so every
    package version and tarball integrity comes from the committed lockfile, never from what the
    registry says today; a local package the lockfile links (`file:`) is copied from the catalog with
    it, and changes the cache key only through its version in the lockfile. No node or npm on PATH,
    or an install that fails, is CannotRun.
    """
    source = os.path.join(NODE_TOOLS, name)
    with open(os.path.join(source, "package-lock.json"), "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()[:12]
    target = os.path.join(cache_dir(), "%s-%s" % (name, digest))
    if os.path.isfile(os.path.join(target, "node_modules", ".package-lock.json")):
        return target
    if not shutil.which("node") or not shutil.which("npm"):
        raise CannotRun("%s needs node and npm on PATH" % name)
    try:
        os.makedirs(target, exist_ok=True)
        for item in os.listdir(source):
            if os.path.isdir(os.path.join(source, item)):  # a local package the lockfile links, such as a stub
                shutil.copytree(os.path.join(source, item), os.path.join(target, item), dirs_exist_ok=True)
            else:
                shutil.copy(os.path.join(source, item), os.path.join(target, item))
    except OSError as error:
        raise CannotRun("cannot write the tool cache %s: %s" % (target, error))
    result = subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=target,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("npm ci for %s failed: %s" % (name, (result.stderr.strip() or "no output").splitlines()[-1]))
    return target


def ensure_python_tool(name, python=None):
    """The interpreter of a venv holding the Python toolchain pinned by tools/<name>/requirements.txt.

    The venv is made from `python` (default: python3 on PATH) and installed once per requirements
    digest and interpreter version (a compiled wheel is built for one CPython) into the cache with
    `pip install --require-hashes --only-binary :all: --no-deps`, so every package is a wheel whose
    sha256 the committed file lists, never a source build and never what the index serves today. No
    such interpreter with venv and pip, or an install that fails, is CannotRun.
    """
    source = os.path.join(NODE_TOOLS, name, "requirements.txt")
    with open(source, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()[:12]
    requested = python or "python3"
    python = shutil.which(requested)
    if not python:
        raise CannotRun("%s needs %s on PATH" % (name, requested))
    version = subprocess.run([python, "-c", "import sys; print('%d%d' % sys.version_info[:2])"],
                             capture_output=True, text=True).stdout.strip()
    target = os.path.join(cache_dir(), "%s-%s-py%s" % (name, digest, version))
    interpreter = os.path.join(target, "bin", "python")
    if os.path.isfile(os.path.join(target, ".installed")):
        return interpreter
    shutil.rmtree(target, ignore_errors=True)  # a half-made venv from an install that failed
    for step in ([python, "-m", "venv", target],
                 [interpreter, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--require-hashes",
                  "--only-binary", ":all:", "--no-deps", "-r", source]):
        try:
            result = subprocess.run(step, capture_output=True, text=True)
        except OSError as error:
            raise CannotRun("installing %s failed: %s" % (name, error))
        if result.returncode != 0:
            raise CannotRun("installing %s failed (%s): %s" % (name, " ".join(step[1:4]),
                                                             (result.stderr.strip() or "no output").splitlines()[-1]))
    open(os.path.join(target, ".installed"), "w").close()
    return interpreter


def _stop(process):
    """Kill a tool's whole process group (its test runner workers are in it) and reap it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:  # the group already ended
        pass
    process.wait()  # not communicate(): a signal may have interrupted it mid-read, and nothing can write now


def run_tool(command, cwd, timeout, name, env=None, budget=None):
    """(exit code, combined output) of a tool run in a session of its own, so its whole process group can be
    killed. That also keeps a SIGTERM or SIGINT sent to the gate's group (a Ctrl-C) from reaching it: both are
    caught here and kill it. Past `timeout` seconds the group is killed and the gate cannot run; `budget` is
    the --timeout the message names when `timeout` is what is left of it."""
    def interrupted(signum, _frame):
        raise CannotRun("stopped by %s before %s finished; no verdict" % (signal.Signals(signum).name, name))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    process = None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", start_new_session=True)
        output, _ = process.communicate(timeout=max(timeout, 1))
    except subprocess.TimeoutExpired:
        _stop(process)
        raise CannotRun("%s did not finish within --timeout %ds; no verdict on any file. Raise --timeout or "
                        "--max-files on the base manifest, or split the pull request" % (name, budget or timeout))
    except BaseException:
        if process:
            _stop(process)
        raise
    return process.returncode, output


def ensure(name):
    """The path of the pinned tool, resolving PATH, then the cache, then a verified download."""
    tool = TOOLS[name]
    on_path = shutil.which(name)
    if on_path and _reports_version(on_path, tool):
        return on_path
    cached = os.path.join(cache_dir(), "%s-%s" % (name, tool["version"]), name)
    if not (os.path.isfile(cached) and _reports_version(cached, tool)):
        _download(name, tool, cached)
        if not _reports_version(cached, tool):
            raise CannotRun("the downloaded %s does not report version %s" % (name, tool["version"]))
    return cached
