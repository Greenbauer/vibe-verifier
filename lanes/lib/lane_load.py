"""hosts/<host>.yml parsed and validated into a Host (lane_model.py).

Every value the scripts get has passed the validation here, so none of them parses YAML or re-checks
a field. An invalid file raises ValueError with the reason.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from lane_model import DOCKER_SIZE, Dashboard, Host, Lane


RUNNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LABEL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# A lane name is also its system user, unit prefix and directory name, and the listener's and the
# health check's own pattern needs a leading letter.
LANE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# The listener's own name_prefix pattern (listener/main.go), so a prefix it would refuse at start is
# refused here first.
NAME_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
GH_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
# An App's login may end in [bot]; it is written into the lane user's hosts.yml as a YAML key.
GH_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?$")
HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
IMAGE_NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# systemd's size suffixes (MemoryMax=16G); docker's are lane_model.DOCKER_SIZE.
UNIT_SIZE = re.compile(r"^[1-9][0-9]*[KMGT]?$")
CPU_QUOTA = re.compile(r"^[1-9][0-9]*%$")
# An absolute path made of plain components: no spaces, nothing a unit file or a shell line could
# misread. A `..` component is refused separately.
ABS_PATH = re.compile(r"^(?:/[A-Za-z0-9._-]+)+$")
# The kernel's interface name limit; a lane's bridge is <name>0.
BRIDGE_MAX = 15

HOST_KEYS = {"hostname", "sysbox", "lanes"}
HOST_OPTIONAL_KEYS = {"dashboards"}
# A Vibe Verifier dashboard (bin/provision-dashboards.sh): a systemd instance whose account is
# vibe-dashboard-<name>, which useradd caps at 32 characters (Vibe Verifier docs/dashboard-host.md).
DASHBOARD_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
DASHBOARD_NAME_MAX = 17
DASHBOARD_KEYS = {"name", "lane", "port", "config"}
DASHBOARD_OPTIONAL_KEYS = {"bridge_account"}
UNIX_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
LANE_KEYS = {
    "name", "scope", "owner", "labels", "app", "slots", "name_prefix", "image", "preload", "slice",
    "container", "runtime_max_sec", "slot_disk_gb", "store_disk_gb", "check_ports",
}
SCOPE_KEYS = {"org": {"runner_group", "repos", "min_runners"}, "user": {"exclude"}}
QAE_KEYS = {"codex_store", "qae_concurrency"}
# The optional wait kind (an org lane's only): jobs that only poll for another job's result take its
# slots, which have a budget of their own and a smaller container, so a queue of them can never hold
# the ci slots the awaited job needs (listener/README.md, "Budget and admission").
WAIT_KEYS = {"label", "slots", "memory"}
# Keys a lane may leave out, with the value an absent one takes. warm_max_age_sec is how long a warm
# slot's unit may stay active before the listener recycles it (listener/README.md, "Warm recycle").
LANE_OPTIONAL_KEYS = {"warm_max_age_sec": 1200}


class _UniqueKeyLoader(yaml.SafeLoader):
    """safe_load that refuses a mapping key given twice; PyYAML keeps the last one silently."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(f"{key!r} is given twice (line {key_node.start_mark.line + 1})")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


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


def _repo_list(value: object, field: str, block: str, *, empty_ok: bool) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (not value and not empty_ok)
        or any(not isinstance(repo, str) or not REPO_NAME.fullmatch(repo) for repo in value)
    ):
        kind = "a list" if empty_ok else "a non-empty list"
        raise ValueError(f"{block}.{field} must be {kind} of names matching {REPO_NAME.pattern}")
    if len(set(value)) != len(value):
        raise ValueError(f"{block}.{field} must not contain duplicates")
    return tuple(value)


def _lane_label(label: object, field: str, block: str) -> str:
    """A lane label: outside the repository-runner routing labels (ci-*, vps, vps-N)."""
    if (
        not isinstance(label, str)
        or not LABEL.fullmatch(label)
        or label.startswith("ci-")
        or re.fullmatch(r"vps(?:-[0-9]+)?", label)
    ):
        raise ValueError(
            f"{block}.{field} must match {LABEL.pattern} outside the repository-runner routing "
            "labels (ci-*, vps, vps-N)"
        )
    return label


def _check_ports(value: object, block: str) -> tuple[int, ...]:
    """The host ports the lane's smoke proves a slot cannot reach (its --check)."""
    if (
        not isinstance(value, list)
        or not value
        or any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in value)
    ):
        raise ValueError(f"{block}.check_ports must be a non-empty list of ports in 1..65535")
    if len(set(value)) != len(value):
        raise ValueError(f"{block}.check_ports must not contain duplicates")
    return tuple(value)


def _warm_max_age(raw: dict, runtime_max_sec: int, warm_pool: bool, block: str) -> int:
    """warm_max_age_sec, or its default when absent: always a positive integer.

    Only a lane with a warm pool (an org lane with min_runners > 0) uses it: the listener recycles
    its warm slots at that age and the slot units' RuntimeMaxSec adds it, so there it must be below
    runtime_max_sec. A lane without a warm pool never recycles and keeps RuntimeMaxSec at
    runtime_max_sec, so there the value is inert and never checked against runtime_max_sec.
    """
    default = LANE_OPTIONAL_KEYS["warm_max_age_sec"]
    value = raw.get("warm_max_age_sec", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{block}.warm_max_age_sec must be a positive integer")
    if warm_pool and value >= runtime_max_sec:
        raise ValueError(
            f"{block}.warm_max_age_sec ({default} when absent) must be below runtime_max_sec "
            f"({runtime_max_sec}) on a lane with a warm pool"
        )
    return value


def _preload_entry(repo: object, directory: object, block: str) -> None:
    """One entry of the preload map: a repository name, and a plain relative path named `supabase`
    (the CLI finds <parent>/supabase/config.toml) or "-" for a repository with no project."""
    if not isinstance(repo, str) or not REPO_NAME.fullmatch(repo):
        raise ValueError(f"{block}.preload: {repo!r} must be a repository name matching {REPO_NAME.pattern}")
    if directory == "-":
        return
    parts = directory.split("/") if isinstance(directory, str) else []
    if not parts or any(part in ("", ".", "..") or not REPO_NAME.fullmatch(part) for part in parts):
        raise ValueError(f"{block}.preload.{repo}: {directory!r} must be a plain relative path, or \"-\"")
    if parts[-1] != "supabase":
        raise ValueError(f"{block}.preload.{repo}: {directory!r} is not a directory named supabase")


def _preload_covers(value: dict, repos: tuple[str, ...], block: str) -> None:
    """An org lane's map pairs every repository in `repos` with an entry, and an entry with a
    repository in `repos`, so a repository added to the lane cannot silently cold-pull."""
    for repo in repos:
        if repo not in value:
            raise ValueError(
                f"{block}.repos lists {repo} but preload has no entry for it: add {repo}: <supabase dir>, "
                f"or {repo}: \"-\" if it has no Supabase project"
            )
    for repo in value:
        if repo not in repos:
            raise ValueError(f"{block}.preload names {repo}, which is not in {block}.repos")


def _preload(value: object, scope: str, repos: tuple[str, ...], block: str) -> tuple[tuple[str, str], ...]:
    """Where each preloaded repository keeps its Supabase project, or "-" for none.

    Every entry is checked first (_preload_entry), then an org lane's map against its `repos`
    (_preload_covers); a user lane lists no repositories, so its map is the list.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{block}.preload must be a map of <repo>: <supabase dir> or \"-\"")
    for repo, directory in value.items():
        _preload_entry(repo, directory, block)
    if scope == "org":
        _preload_covers(value, repos, block)
    return tuple(sorted(value.items()))


def _lane_keys(raw: dict, scope: str, qae: bool, block: str) -> None:
    """The lane declares every key its scope and kinds need, and besides them only the optional ones."""
    other = "user" if scope == "org" else "org"
    misplaced = sorted(set(raw) & SCOPE_KEYS[other])
    if misplaced:
        raise ValueError(f"{block}: {', '.join(misplaced)} belong to {other} lanes only")
    if not qae and set(raw) & QAE_KEYS:
        raise ValueError(f"{block}: codex_store and qae_concurrency come with labels.qae")
    if scope == "user" and "wait" in raw:
        raise ValueError(f"{block}: wait belongs to org lanes only")
    required = LANE_KEYS | SCOPE_KEYS[scope] | (QAE_KEYS if qae else set())
    optional = set(LANE_OPTIONAL_KEYS) | ({"wait"} if scope == "org" else set())
    if not required <= set(raw) <= required | optional:
        raise ValueError(
            f"{block} must declare exactly " + ", ".join(sorted(required))
            + ", and may declare " + ", ".join(sorted(optional))
        )


def _labels(raw: object, block: str) -> tuple[str, str | None]:
    if not isinstance(raw, dict) or "ci" not in raw or not set(raw) <= {"ci", "qae"}:
        raise ValueError(f"{block}.labels must declare ci, and may declare qae")
    ci_label = _lane_label(raw["ci"], "labels.ci", block)
    qae_label = _lane_label(raw["qae"], "labels.qae", block) if "qae" in raw else None
    if ci_label == qae_label:
        raise ValueError(f"{block}.labels.ci and {block}.labels.qae must differ")
    return ci_label, qae_label


def _wait(raw: object, ci_label: str, qae_label: str | None, block: str) -> tuple[str | None, int, str | None]:
    """The lane's wait block as (label, slots, memory), or (None, 0, None) when it declares none."""
    if raw is None:
        return None, 0, None
    wait = _exact_keys(raw, WAIT_KEYS, "wait", block)
    label = _lane_label(wait["label"], "wait.label", block)
    for other, name in ((ci_label, "labels.ci"), (qae_label, "labels.qae")):
        if label == other:
            raise ValueError(f"{block}.wait.label must differ from {block}.{name}")
    return label, _positive_int(wait["slots"], "wait.slots", block), _matching(wait["memory"], DOCKER_SIZE, "wait.memory", block)


def _load_lane(raw: object, index: int) -> Lane:
    block = f"lanes[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{block} must be a map")
    name = _matching(raw.get("name"), LANE_NAME, "name", block)
    block = f"lanes.{name}"
    if len(name) + 1 > BRIDGE_MAX:
        raise ValueError(f"{block}: the bridge {name}0 exceeds the kernel's {BRIDGE_MAX}-character interface limit; shorten the name")
    scope = raw.get("scope")
    if scope not in SCOPE_KEYS:
        raise ValueError(f"{block}.scope must be org or user")
    ci_label, qae_label = _labels(raw.get("labels"), block)
    _lane_keys(raw, scope, qae_label is not None, block)
    slots = _positive_int(raw["slots"], "slots", block)
    repos = _repo_list(raw["repos"], "repos", block, empty_ok=False) if scope == "org" else ()
    app = _exact_keys(raw["app"], {"id", "installation_id", "key_file", "login"}, "app", block)
    slice_ = _exact_keys(raw["slice"], {"memory_max", "memory_high", "cpu_quota", "cpu_weight"}, "slice", block)
    container = _exact_keys(raw["container"], {"memory", "pids", "tmp_size"}, "container", block)
    runtime_max_sec = _positive_int(raw["runtime_max_sec"], "runtime_max_sec", block)
    min_runners = _int_between(raw["min_runners"], 0, slots, "min_runners", block) if scope == "org" else 0
    wait_label, wait_slots, wait_memory = _wait(raw.get("wait"), ci_label, qae_label, block)
    return Lane(
        name=name,
        scope=scope,
        owner=_matching(raw["owner"], GH_OWNER, "owner", block),
        runner_group=_matching(raw["runner_group"], RUNNER_NAME, "runner_group", block) if scope == "org" else "default",
        repos=repos,
        exclude=_repo_list(raw["exclude"], "exclude", block, empty_ok=True) if scope == "user" else (),
        ci_label=ci_label,
        qae_label=qae_label,
        app_id=_positive_int(app["id"], "app.id", block),
        app_installation_id=_positive_int(app["installation_id"], "app.installation_id", block),
        app_key_file=_absolute_path(app["key_file"], "app.key_file", block),
        app_login=_matching(app["login"], GH_LOGIN, "app.login", block),
        codex_store=_absolute_path(raw["codex_store"], "codex_store", block) if qae_label else None,
        slots=slots,
        min_runners=min_runners,
        qae_concurrency=_int_between(raw["qae_concurrency"], 1, slots, "qae_concurrency", block) if qae_label else 0,
        name_prefix=_matching(raw["name_prefix"], NAME_PREFIX, "name_prefix", block),
        image=_matching(raw["image"], IMAGE_NAME, "image", block),
        preload=_preload(raw["preload"], scope, repos, block),
        memory_max=_matching(slice_["memory_max"], UNIT_SIZE, "slice.memory_max", block),
        memory_high=_matching(slice_["memory_high"], UNIT_SIZE, "slice.memory_high", block),
        cpu_quota=_matching(slice_["cpu_quota"], CPU_QUOTA, "slice.cpu_quota", block),
        cpu_weight=_int_between(slice_["cpu_weight"], 1, 10000, "slice.cpu_weight", block),
        container_memory=_matching(container["memory"], DOCKER_SIZE, "container.memory", block),
        pids=_positive_int(container["pids"], "container.pids", block),
        tmp_size=_matching(container["tmp_size"], DOCKER_SIZE, "container.tmp_size", block),
        runtime_max_sec=runtime_max_sec,
        warm_max_age_sec=_warm_max_age(raw, runtime_max_sec, min_runners > 0, block),
        slot_disk_gb=_positive_int(raw["slot_disk_gb"], "slot_disk_gb", block),
        store_disk_gb=_positive_int(raw["store_disk_gb"], "store_disk_gb", block),
        check_ports=_check_ports(raw["check_ports"], block),
        wait_label=wait_label,
        wait_slots=wait_slots,
        wait_memory=wait_memory,
    )


def _unique(lanes: tuple[Lane, ...], what: str, values_of) -> None:
    """No two lanes share one of these values (each lane yields its own, None for none)."""
    owner_of: dict[str, tuple[int, str]] = {}
    for index, lane in enumerate(lanes):
        for value in values_of(lane):
            if value is None:
                continue
            key = value.lower() if what == "label" else value
            if key in owner_of and owner_of[key][0] != index:
                raise ValueError(f"lanes {owner_of[key][1]} and {lane.name} share the {what} {value}")
            owner_of[key] = (index, lane.name)


def _check_across(lanes: tuple[Lane, ...]) -> None:
    """What the lanes of one machine may not share.

    A shared label would route one lane's jobs to the other, a shared name prefix would let one
    lane's registration sweep delete the other's runners, a shared image name would let one lane's
    image build prune the other's tags (each keeps only the tags whose store it holds), a shared
    key file or Codex store (any QAE instance's) would hand one lane's credentials to the other, and
    a shared slice unit would be rewritten by both lanes and deleted by either one's --remove (GitHub
    compares labels without case).
    """
    _unique(lanes, "name", lambda lane: (lane.name,))
    _unique(lanes, "label", lambda lane: (lane.ci_label, lane.qae_label, lane.wait_label))
    _unique(lanes, "name_prefix", lambda lane: (lane.name_prefix,))
    _unique(lanes, "image", lambda lane: (lane.image,))
    _unique(lanes, "app.key_file", lambda lane: (lane.app_key_file,))
    _unique(lanes, "codex_store", lambda lane: lane.codex_stores)
    _unique(lanes, "slice unit", lambda lane: (lane.slice, lane.top_slice))


def _load_dashboard(raw: object, index: int, lanes: tuple[Lane, ...]) -> Dashboard:
    """One dashboard: what the provisioner derives its files from. The dashboard's own loader checks
    the rest of config on the machine before the file goes live (bin/provision-dashboards.sh)."""
    block = f"dashboards[{index}]"
    if not isinstance(raw, dict) or not DASHBOARD_KEYS <= set(raw) <= DASHBOARD_KEYS | DASHBOARD_OPTIONAL_KEYS:
        raise ValueError(f"{block} must declare exactly {', '.join(sorted(DASHBOARD_KEYS))}, and may declare bridge_account")
    name = _matching(raw["name"], DASHBOARD_NAME, "name", block)
    block = f"dashboards.{name}"
    if len(name) > DASHBOARD_NAME_MAX:
        raise ValueError(f"{block}: the account vibe-dashboard-{name} exceeds useradd's 32 characters; use at most {DASHBOARD_NAME_MAX}")
    by_name = {lane.name: lane for lane in lanes}
    if not isinstance(raw["lane"], str) or raw["lane"] not in by_name:
        raise ValueError(f"{block}.lane must be one of this machine's lanes ({', '.join(by_name)})")
    lane = by_name[raw["lane"]]
    bridge = _matching(raw["bridge_account"], UNIX_USER, "bridge_account", block) if "bridge_account" in raw else None
    config = raw["config"]
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError(f"{block}.config must be a dashboard configuration with version 1")
    owner = config.get("owner")
    if not isinstance(owner, str) or owner.casefold() != lane.owner.casefold():
        # The telemetry comes from the lane's listener, which serves one owner, and the read token
        # from the lane's App installation, which lives on that owner's account.
        raise ValueError(f"{block}.config.owner must be the owner of its lane {lane.name}, {lane.owner}")
    if "telemetry_file" in config:
        raise ValueError(f"{block}.config must not set telemetry_file: it is always /var/lib/vibe-dashboard/{name}/telemetry.json")
    return Dashboard(name=name, lane=lane, port=_int_between(raw["port"], 1, 65535, "port", block),
                     bridge_account=bridge, config=config)


def load_host(path: str | Path) -> Host:
    """Parse and validate hosts/<host>.yml. Raises ValueError (or OSError) with the reason."""
    try:
        document = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not YAML: {exc}") from exc
    if not isinstance(document, dict) or not HOST_KEYS <= set(document) <= HOST_KEYS | HOST_OPTIONAL_KEYS:
        raise ValueError(f"{path} must declare exactly {', '.join(sorted(HOST_KEYS))}, and may declare dashboards")
    sysbox = _exact_keys(document["sysbox"], {"image", "disk_gb"}, "sysbox", "host")
    raw_lanes = document["lanes"]
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ValueError("host.lanes must be a non-empty list")
    lanes = tuple(_load_lane(raw, index) for index, raw in enumerate(raw_lanes))
    _check_across(lanes)
    raw_dashboards = document.get("dashboards", [])
    if not isinstance(raw_dashboards, list):
        raise ValueError("host.dashboards must be a list")
    dashboards = tuple(_load_dashboard(raw, index, lanes) for index, raw in enumerate(raw_dashboards))
    for what, values in (("name", [d.name for d in dashboards]), ("port", [d.port for d in dashboards])):
        if len(set(values)) != len(values):
            raise ValueError(f"two dashboards share a {what}")
    return Host(
        hostname=_matching(document["hostname"], HOSTNAME, "hostname", "host"),
        sysbox_image=_absolute_path(sysbox["image"], "sysbox.image", "host"),
        sysbox_disk_gb=_positive_int(sysbox["disk_gb"], "sysbox.disk_gb", "host"),
        lanes=lanes,
        dashboards=dashboards,
    )
