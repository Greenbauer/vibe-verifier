"""What the lane configuration tests share: two lane blocks, a host file writer and the checks.

Importing it puts lib/ on the path, so a test imports the module it covers after this one.
"""

from pathlib import Path
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from lane_load import load_host  # noqa: E402

passed = failed = 0


def check(cond: bool, desc: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"✓ {desc}")
    else:
        failed += 1
        print(f"✗ {desc}")


def finish(name: str) -> None:
    """Print the test file's totals and exit non-zero when a check failed."""
    print(f"{name}: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


TMP = Path(tempfile.mkdtemp())
ORG = {
    "name": "box-ci",
    "scope": "org",
    "owner": "example",
    "runner_group": "box-ci",
    "repos": ["alpha", "beta.web"],
    "labels": {"ci": "box-ci", "qae": "box-qae"},
    "app": {"id": 123456, "installation_id": 7654321, "key_file": "/etc/box-ci/app.pem", "login": "example-bot"},
    "codex_store": "/var/lib/box-ci/codex",
    "slots": 3,
    "min_runners": 2,
    "qae_concurrency": 1,
    "name_prefix": "box-lane",
    "image": "box-ci-runner",
    "preload": {"alpha": "supabase", "beta.web": "-"},
    "slice": {"memory_max": "16G", "memory_high": "14G", "cpu_quota": "800%", "cpu_weight": 50},
    "container": {"memory": "4g", "pids": 2048, "tmp_size": "2g"},
    "runtime_max_sec": 3600,
    "slot_disk_gb": 10,
    "store_disk_gb": 40,
    "check_ports": [22, 54321, 54322],
}
USER = {
    "name": "own-ci",
    "scope": "user",
    "owner": "Someone",
    "labels": {"ci": "own-ci", "qae": "own-qae"},
    "exclude": ["fleet"],
    "app": {"id": 234567, "installation_id": 8765432, "key_file": "/etc/own-ci/app.pem", "login": "someone-bot"},
    "codex_store": "/var/lib/own-ci/codex",
    "slots": 4,
    "qae_concurrency": 1,
    "name_prefix": "own-lane",
    "image": "own-ci-runner",
    "preload": {},
    "slice": {"memory_max": "8G", "memory_high": "7G", "cpu_quota": "400%", "cpu_weight": 30},
    "container": {"memory": "4g", "pids": 2048, "tmp_size": "2g"},
    "runtime_max_sec": 3600,
    "slot_disk_gb": 6,
    "store_disk_gb": 20,
    "check_ports": [22, 3101, 54329],
}
# The optional wait kind: jobs that only poll for another job's result get their own label, template,
# budget (instances 1..wait.slots, none counted against slots) and container memory.
WAIT = {"label": "box-wait", "slots": 5, "memory": "1g"}
DASH = {
    "name": "box", "lane": "box-ci", "port": 8765, "bridge_account": "box-forward",
    "config": {"version": 1, "owner": "Example", "repositories": ["example/alpha"], "proxy_origin": "https://box.example:8443"},
}


def without(block: dict, *keys: str) -> dict:
    return {key: value for key, value in block.items() if key not in keys}


def host_file(*lanes: dict, top: dict | None = None) -> Path:
    doc = {"hostname": "box-1", "sysbox": {"image": "/var/lib/runners/sysbox.img", "disk_gb": 20}, "lanes": list(lanes)}
    doc.update(top or {})
    path = TMP / "host.yml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def refused(path: Path, marker: str, desc: str) -> None:
    try:
        load_host(path)
    except ValueError as error:
        check(marker in str(error), f"{desc} (refused: {error})")
        return
    check(False, f"{desc} (it loaded)")
