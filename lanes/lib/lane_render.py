"""What the scripts read from a validated Host (lane_model.py): the listener's config, the
KEY=value settings of a machine, a lane and a dashboard, and a dashboard's two JSON files.
"""

from __future__ import annotations

import json

from lane_model import Dashboard, Host, Lane, codex_stores, docker_size_bytes


# The collector needs a Codex home; a lane without QAE has no Codex login, so its quota reads
# "unavailable" from a path that never holds one.
NO_CODEX_HOME = "/nonexistent"
# What the listener's config fixes for every lane (listener/README.md): the memory a start keeps
# free beyond its container, how long an unused slot may idle, how often a user lane re-lists its
# App installation's repositories, and the runner's work folder (the image's).
LISTENER_RESERVE_BYTES = 2 * 1024**3
LISTENER_IDLE_STOP_SEC = 300
LISTENER_REPOS_REFRESH_SEC = 600
LISTENER_WORK_FOLDER = "/home/runner/_work"


def _kind_config(lane: Lane, kind: str, codex_store: str) -> dict[str, object]:
    """One kind's scale set. An org ci set carries self-hosted too, so `runs-on: [self-hosted, <label>]`
    matches; every other set carries its label alone, so a bare self-hosted job never reaches the
    Codex login stores a qae set mounts. A qae instance waits for its own store's login."""
    label = lane.label(kind)
    org_ci = lane.scope == "org" and kind == "ci"
    config: dict[str, object] = {"set_name": label, "labels": ["self-hosted", label] if org_ci else [label]}
    if kind == "qae":
        config["requires_files"] = [f"{store}/auth.json" for store in codex_stores(codex_store, lane.qae_concurrency)]
    return config


def _wait_kind_config(lane: Lane) -> dict[str, object]:
    """The wait kind's scale set: self-hosted and its label, like an org ci set, and its own budget
    and container size, which the listener counts apart from `budget.slots` (listener/README.md)."""
    return {
        "set_name": lane.wait_label,
        "labels": ["self-hosted", lane.wait_label],
        "slots": lane.wait_slots,
        "container_memory_bytes": docker_size_bytes(lane.wait_memory),
    }


def listener_config(lane: Lane, *, key_file: str | None = None, codex_store: str | None = None) -> str:
    """The scale-set listener's JSON config for a lane (listener/README.md, "Config").

    key_file and codex_store (the first QAE instance's store, from which the others are named)
    default to the lane's own paths; the lane provisioner passes the paths it manages (the same ones,
    except under its hermetic test).
    """
    store = codex_store or lane.codex_store or ""
    config: dict[str, object] = {
        "name": lane.name,
        "scope": lane.scope,
        "github_url": f"https://github.com/{lane.owner}",
        "runner_group": lane.runner_group,
        "app": {
            "client_id": str(lane.app_id),
            "installation_id": lane.app_installation_id,
            "key_file": key_file or lane.app_key_file,
        },
        "kinds": {kind: _wait_kind_config(lane) if kind == "wait" else _kind_config(lane, kind, store) for kind in lane.kinds},
    }
    if lane.scope == "user":
        config["repos"] = {"exclude": list(lane.exclude), "refresh_sec": LISTENER_REPOS_REFRESH_SEC}
    config["budget"] = {"slots": lane.slots, "min_runners": lane.min_runners, "qae_concurrency": lane.qae_concurrency}
    config["admission"] = {
        "container_memory_bytes": docker_size_bytes(lane.container_memory),
        "reserve_bytes": LISTENER_RESERVE_BYTES,
    }
    config["runner"] = {"name_prefix": lane.name_prefix, "work_folder": LISTENER_WORK_FOLDER}
    config["idle_stop_sec"] = LISTENER_IDLE_STOP_SEC
    config["warm_max_age_sec"] = lane.warm_max_age_sec
    return json.dumps(config, indent=2) + "\n"


def host_settings(host: Host) -> dict[str, object]:
    return {
        "HOST_NAME": host.hostname,
        "SYSBOX_IMAGE": host.sysbox_image,
        "SYSBOX_GB": host.sysbox_disk_gb,
        "LANES": " ".join(lane.name for lane in host.lanes),
    }


def lane_settings(host: Host, lane: Lane) -> dict[str, object]:
    """One KEY=value per setting the bash scripts read; an absent setting is an empty value."""
    return {
        "HOST_NAME": host.hostname,
        "NAME": lane.name,
        "SCOPE": lane.scope,
        "OWNER": lane.owner,
        "GROUP": lane.runner_group if lane.scope == "org" else "",
        "REPOS": " ".join(lane.repos),
        "EXCLUDE": " ".join(lane.exclude),
        "CI_LABEL": lane.ci_label,
        "QAE_LABEL": lane.qae_label or "",
        "WAIT_LABEL": lane.wait_label or "",
        "WAIT_SLOTS": lane.wait_slots,
        "WAIT_MEMORY": lane.wait_memory or "",
        "KINDS": " ".join(lane.kinds),
        "SLOTS": lane.slots,
        "MIN_RUNNERS": lane.min_runners,
        "QAE_CONCURRENCY": lane.qae_concurrency,
        "NAME_PREFIX": lane.name_prefix,
        "IMAGE": lane.image,
        "MEMORY_MAX": lane.memory_max,
        "MEMORY_HIGH": lane.memory_high,
        "CPU_QUOTA": lane.cpu_quota,
        "CPU_WEIGHT": lane.cpu_weight,
        "CONTAINER_MEMORY": lane.container_memory,
        "PIDS": lane.pids,
        "TMP_SIZE": lane.tmp_size,
        "SLOT_RUNTIME_MAX": lane.slot_runtime_max_sec,
        "SLOT_GB": lane.slot_disk_gb,
        "STORE_GB": lane.store_disk_gb,
        "SLICE": lane.slice,
        "TOP_SLICE": lane.top_slice or "",
        "BRIDGE": lane.bridge,
        "LANE_USER": lane.user,
        "LANE_HOME": lane.home,
        "APP_ID": lane.app_id,
        "APP_INSTALLATION_ID": lane.app_installation_id,
        "APP_KEY_FILE": lane.app_key_file,
        "APP_LOGIN": lane.app_login,
        "CODEX_STORE": lane.codex_store or "",
        "CODEX_STORES": " ".join(lane.codex_stores),
        "CHECK_PORTS": " ".join(str(port) for port in lane.check_ports),
    }


def dashboard_config(dashboard: Dashboard) -> str:
    """The dashboard's dashboard.json: its declared configuration and this machine's telemetry file."""
    return json.dumps({**dashboard.config, "telemetry_file": dashboard.telemetry_file}, indent=2) + "\n"


def collector_config(host: Host, dashboard: Dashboard) -> str:
    """The telemetry collector's local-mode configuration (Vibe Verifier docs/dashboard-collector.md).

    workspace_path is the lane's state directory: the filesystem its slot images, store image and
    Codex stores fill. codex_home is the first QAE instance's Codex store, whose login's quota the
    dashboard shows; a lane without QAE has no login, so its quota reads unavailable.
    """
    lane = dashboard.lane
    return json.dumps({
        "version": 1,
        "owner": dashboard.config["owner"],
        "host": {
            "label": f"{host.hostname} {lane.name}",
            "listener_config_path": f"/etc/{lane.name}/listener.json",
            "lane_name": lane.name,
            "workspace_path": lane.state_dir,
            "codex_home": lane.codex_store or NO_CODEX_HOME,
        },
    }, indent=2) + "\n"


def dashboard_settings(dashboard: Dashboard) -> dict[str, object]:
    """One KEY=value per setting bin/provision-dashboards.sh reads; the App is the lane's own."""
    lane = dashboard.lane
    return {
        "NAME": dashboard.name,
        "ACCOUNT": dashboard.account,
        "PORT": dashboard.port,
        "LANE": lane.name,
        "BRIDGE_ACCOUNT": dashboard.bridge_account or "",
        "APP_ID": lane.app_id,
        "APP_INSTALLATION_ID": lane.app_installation_id,
        "APP_KEY_FILE": lane.app_key_file,
        "APP_LOGIN": lane.app_login,
    }
