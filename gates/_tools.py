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
import stat
import subprocess
import tarfile
import tempfile
import urllib.request

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
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = next((m for m in archive.getmembers() if m.isfile() and os.path.basename(m.name) == name), None)
        if member is None:
            raise CannotRun("%s holds no file named %s" % (asset, name))
        payload = archive.extractfile(member).read()
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
    registry says today. No node or npm on PATH, or an install that fails, is CannotRun.
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
            shutil.copy(os.path.join(source, item), os.path.join(target, item))
    except OSError as error:
        raise CannotRun("cannot write the tool cache %s: %s" % (target, error))
    result = subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=target,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("npm ci for %s failed: %s" % (name, (result.stderr.strip() or "no output").splitlines()[-1]))
    return target


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
