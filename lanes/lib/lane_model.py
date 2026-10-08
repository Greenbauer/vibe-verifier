"""A machine's lanes and dashboards as values: what lane_load.py builds from hosts/<host>.yml and
lane_render.py turns into the files and settings the scripts read. Everything a lane's name decides
(its user, paths, units, slices and bridge) is derived here, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# docker's size suffixes (--memory 4g, tmpfs size=2g).
DOCKER_SIZE = re.compile(r"^[1-9][0-9]*[kmg]?$")


@dataclass(frozen=True)
class Lane:
    """One disposable-container lane: one Sysbox container per job, started by its listener."""

    name: str
    scope: str
    owner: str
    runner_group: str
    repos: tuple[str, ...]
    exclude: tuple[str, ...]
    ci_label: str
    qae_label: str | None
    app_id: int
    app_installation_id: int
    app_key_file: str
    app_login: str
    codex_store: str | None
    slots: int
    min_runners: int
    qae_concurrency: int
    name_prefix: str
    image: str
    preload: tuple[tuple[str, str], ...]
    memory_max: str
    memory_high: str
    cpu_quota: str
    cpu_weight: int
    container_memory: str
    pids: int
    tmp_size: str
    runtime_max_sec: int
    warm_max_age_sec: int
    slot_disk_gb: int
    store_disk_gb: int
    check_ports: tuple[int, ...]
    wait_label: str | None = None
    wait_slots: int = 0
    wait_memory: str | None = None
    # The filesystem of the lane's store (README.md, "Disk"): xfs, where a job's store is a reflink
    # copy of the preloaded one, or btrfs, where it is a snapshot of it.
    store_fs: str = "xfs"

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds of job, each with its own label, scale set and slot template."""
        return ("ci",) + (("qae",) if self.qae_label else ()) + (("wait",) if self.wait_label else ())

    @property
    def user(self) -> str:
        """The lane's own system user: always the lane's name."""
        return self.name

    @property
    def state_dir(self) -> str:
        return f"/var/lib/{self.name}"

    @property
    def home(self) -> str:
        return f"{self.state_dir}/home"

    @property
    def bridge(self) -> str:
        return f"{self.name}0"

    @property
    def codex_stores(self) -> tuple[str, ...]:
        """The QAE instances' Codex login stores, the n-th for qae instance n (none without QAE)."""
        return codex_stores(self.codex_store, self.qae_concurrency) if self.codex_store else ()

    def label(self, kind: str) -> str:
        if kind not in self.kinds:
            raise ValueError(f"lane kind {kind!r} is not one of {', '.join(self.kinds)}")
        if kind == "wait":
            return self.wait_label
        return self.ci_label if kind == "ci" else self.qae_label

    def template(self, kind: str) -> str:
        self.label(kind)
        return f"{self.name}-{kind}@.service"

    def kind_slots(self, kind: str) -> int:
        """How many slots of a kind may run at once: the wait kind's own budget, else `slots`."""
        self.label(kind)
        return self.wait_slots if kind == "wait" else self.slots

    def unit(self, kind: str, slot: int) -> str:
        slots = self.kind_slots(kind)
        if slot < 1 or slot > slots:
            raise ValueError(f"lane slot {slot} is outside 1..{slots}")
        return self.template(kind).replace("@.", f"@{slot}.")

    @property
    def always_on_template(self) -> str:
        """The template a lane ran before its listener: one always-on slot per instance.

        bin/provision-lane.sh retires it (Phase L); nothing writes it any more.
        """
        return f"{self.name}@.service"

    @property
    def slice(self) -> str:
        return f"{self.name}.slice"

    @property
    def top_slice(self) -> str | None:
        """The root-level ancestor systemd derives from a dashed slice name, or None.

        systemd nests slices on dashes: greenbauer-ci.slice lives inside greenbauer.slice, and a
        CPUWeight only competes with siblings, so the lane's weight must also sit on this ancestor
        to rank the lane against system.slice.
        """
        return f"{self.name.split('-', 1)[0]}.slice" if "-" in self.name else None

    @property
    def slot_runtime_max_sec(self) -> int:
        """The slot unit's RuntimeMaxSec: runtime_max_sec, plus warm_max_age_sec with a warm pool.

        systemd counts RuntimeMaxSec from the unit's start, and the listener recycles a warm slot
        only once its unit has been up warm_max_age_sec, so a job the slot takes just before that
        still has the whole runtime_max_sec.
        """
        return self.runtime_max_sec + (self.warm_max_age_sec if self.min_runners else 0)

    @property
    def preload_text(self) -> str:
        """The preload map in canonical text: the image build hashes it and reads it."""
        return "".join(f"{repo} {directory}\n" for repo, directory in self.preload)


@dataclass(frozen=True)
class Dashboard:
    """One Vibe Verifier dashboard on loopback, fed by one lane of the machine (README.md, "Dashboards").

    config is the dashboard's own configuration (Vibe Verifier docs/dashboard.md), less telemetry_file,
    which is always this machine's published telemetry. The dashboard validates the rest itself.
    """

    name: str
    lane: Lane
    port: int
    bridge_account: str | None
    config: dict

    @property
    def account(self) -> str:
        return f"vibe-dashboard-{self.name}"

    @property
    def telemetry_file(self) -> str:
        return f"/var/lib/vibe-dashboard/{self.name}/telemetry.json"


@dataclass(frozen=True)
class Host:
    """One machine: its Sysbox filesystem, the lanes it runs and the dashboards it serves."""

    hostname: str
    sysbox_image: str
    sysbox_disk_gb: int
    lanes: tuple[Lane, ...]
    dashboards: tuple[Dashboard, ...] = ()

    def lane(self, name: str) -> Lane:
        for lane in self.lanes:
            if lane.name == name:
                return lane
        raise ValueError(f"no lane named {name!r} (the lanes are {', '.join(lane.name for lane in self.lanes)})")

    def dashboard(self, name: str) -> Dashboard:
        for dashboard in self.dashboards:
            if dashboard.name == name:
                return dashboard
        raise ValueError(f"no dashboard named {name!r}")


def codex_stores(base: str, count: int) -> tuple[str, ...]:
    """One Codex login store per QAE instance: instance 1's is the lane's codex_store itself, so the
    login seeded there before stores were per instance stays where it is, and instance n's is the
    sibling <codex_store>-<n>, under the same parent and so behind the same directory permissions.
    Never nested in the first: prepare and cleanup delete everything in a store but its auth.json.

    Codex rotates a login's refresh token as a job uses it, so two concurrent jobs on one store retire
    each other's session (OpenAI: one auth.json per runner or per serialized job stream). Each QAE
    instance mounts only its own store, and the listener starts it only once that store holds a login.
    """
    return tuple(base if n == 1 else f"{base}-{n}" for n in range(1, count + 1))


def docker_size_bytes(size: str) -> int:
    """Bytes in a docker size (`4g`, `512m`, `2048k`, or plain bytes), as `docker run --memory` reads it."""
    if not DOCKER_SIZE.fullmatch(size):
        raise ValueError(f"{size!r} is not a docker size matching {DOCKER_SIZE.pattern}")
    factor = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(size[-1], 1)
    return int(size.rstrip("kmg")) * factor
