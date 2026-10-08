"""Which repositories an owner has, and a budget that does not starve the same ones every pass.

Kept out of github.py so that file stays within the length ratchet. Each function stays within
the complexity ratchet on its own: a new module may not contain one over the limit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import REPOSITORY, coverage_label
from .gh_api import ApiError
from .github import CHECKS_NOT_LOADED, _endpoint
from .pull_signals import face_fields, load_signals
from .util import iso_time

_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)
_SEARCH_REPOSITORY = "https://api.github.com/repos/"


class OwnerSet:
    def _unloaded_pull(self, repository: str, row: dict, inventory: dict, code: str,
                       signals: dict | None = None) -> dict:
        """A listed pull request whose checks were not read. A spent budget says so; a real access
        failure stays unavailable. Signals already read for this pull request are kept."""
        identity = self._pull_identity(repository, row, inventory)
        budget = code == "request_budget_exhausted"
        face = face_fields(None, signals, evidence=False, draft=identity["draft"], checks=[], statuses=[], expected=[])
        pull = {**identity, **face, "head_changed": False, "evidence_available": False, "attention": True,
                "attention_reason": CHECKS_NOT_LOADED if budget else "Current-head evidence is unavailable",
                "checks": [], "statuses": [], "expected": [], "runs": [], "source_error": code}
        if not budget:
            pull["unavailable"] = True
        return pull

    def _one_pull(self, repository: str, row: dict, inventory: dict, rules: dict, signals: dict) -> dict:
        base = (row.get("base") or {}).get("ref")
        if isinstance(base, str) and base not in rules:
            rules[base] = self._branch_rules(repository, base)
        return self._pull(repository, row, inventory, rules.get(base, []), signals.get(row.get("number")))

    def _note_pull_error(self, repository: str, row: dict, inventory: dict, signals: dict, error: ApiError,
                         pulls: list, errors: list) -> bool:
        pulls.append(self._unloaded_pull(repository, row, inventory, error.code, signals.get(row.get("number"))))
        errors.append({"pull": row.get("number"), "code": error.code})
        return error.code == "request_budget_exhausted"

    def _detail_repository(self, repository: str, rows: list[dict], inventory: dict) -> tuple[dict, bool]:
        """Read current-head detail. True when the budget stopped this repository before it finished."""
        pulls, errors, rules = [], [], {}
        signals = load_signals(self.api, repository, len(rows))
        stopped = False
        for row in rows:
            if stopped:
                stopped = self._note_pull_error(
                    repository, row, inventory, signals, ApiError("request_budget_exhausted"), pulls, errors)
                continue
            try:
                pulls.append(self._one_pull(repository, row, inventory, rules, signals))
            except ApiError as error:
                stopped = self._note_pull_error(repository, row, inventory, signals, error, pulls, errors)
        return {"repository": repository, **inventory, "pulls": pulls, "errors": errors}, stopped

    def _repository(self, repository: str, inventory: dict) -> dict:
        rows = self.api.items(_endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
        return self._detail_repository(repository, rows, inventory)[0]

    def _by_need(self, names: list[str]) -> list[str]:
        """Never successfully detailed first (never started before started-but-unfinished), then
        the least recently detailed. A repository added since the last pass has neither stamp."""
        def key(name: str) -> tuple:
            success = self._detailed_at.get(name)
            if success is None:
                return (0, self._attempted_at.get(name) or _EARLIEST, names.index(name))
            return (1, success, names.index(name))
        return sorted(names, key=key)

    def _known_inventory(self, repository: str, inventory: dict[str, dict] | None) -> dict:
        known = (inventory or {}).get(repository)
        return known if known else self.inventory(repository)

    def _installation_rows(self) -> list[dict] | None:
        try:
            rows = self.api.items("installation/repositories", "repositories")
        except ApiError as error:
            if self._listing == "installation" or error.code not in ("forbidden", "not_found"):
                raise
            return None
        self._listing = "installation"
        return rows

    def _remember_kind(self, described: dict) -> str:
        kind, login = described.get("type"), described.get("login")
        if kind not in ("User", "Organization") or not isinstance(login, str) or login.casefold() != self.config.owner.casefold():
            raise ApiError("invalid_response")
        self._kind = kind
        return kind

    def _account_endpoint(self) -> str:
        if self._kind == "Organization":
            return "orgs/%s/repos" % self.config.owner
        return _endpoint("users/%s/repos" % self.config.owner, type="owner")

    def _repository_rows(self) -> list[dict]:
        """Every repository this credential can see for the owner.

        An installation token reads `GET /installation/repositories` (user or organization).
        A person's `gh` login is refused there, and then reads `GET /orgs/{owner}/repos` or
        `GET /users/{owner}/repos?type=owner`. The choice is remembered for this process."""
        if self._listing != "account":
            rows = self._installation_rows()
            if rows is not None:
                return rows
        if self._kind is None:
            self._remember_kind(self.api.one("users/%s" % self.config.owner))
        rows = self.api.items(self._account_endpoint())
        self._listing = "account"
        return rows

    def _reject_foreign_owner(self, nested: object) -> None:
        if not isinstance(nested, dict):
            return
        login = nested.get("login")
        if isinstance(login, str) and login.casefold() != self.config.owner.casefold():
            raise ApiError("invalid_response")

    def _canonical_name(self, row: object) -> str | None:
        if not isinstance(row, dict):
            raise ApiError("invalid_response")
        full = row.get("full_name")
        if not isinstance(full, str) or full.count("/") != 1:
            raise ApiError("invalid_response")
        repo_owner, name = full.split("/", 1)
        if repo_owner.casefold() != self.config.owner.casefold() or not REPOSITORY.fullmatch(name):
            raise ApiError("invalid_response")
        self._reject_foreign_owner(row.get("owner"))
        archived = row.get("archived")
        if archived is True:
            return None
        if archived is not False:
            raise ApiError("invalid_response")
        return self.config.owner + "/" + name

    def _names_from(self, rows: object) -> list[str]:
        if not isinstance(rows, list):
            raise ApiError("invalid_response")
        names, seen = [], set()
        for row in rows:
            canonical = self._canonical_name(row)
            if canonical is None or canonical.casefold() in seen:
                continue
            seen.add(canonical.casefold())
            names.append(canonical)
        return names

    @staticmethod
    def _kind_from(rows: list[dict]) -> str | None:
        kinds = set()
        for row in rows:
            owner = row.get("owner")
            if isinstance(owner, dict) and owner.get("type") in ("User", "Organization"):
                kinds.add(owner["type"])
        if len(kinds) == 1:
            return kinds.pop()
        return None

    def _search_query(self, kind: str) -> str:
        qualifier = "org" if kind == "Organization" else "user"
        return "is:pr is:open %s:%s" % (qualifier, self.config.owner)

    def _search_match(self, item: object, known: dict[str, str]) -> str | None:
        if not isinstance(item, dict):
            raise ApiError("invalid_response")
        url = item.get("repository_url")
        if not isinstance(url, str) or not url.startswith(_SEARCH_REPOSITORY):
            raise ApiError("invalid_response")
        return known.get(url[len(_SEARCH_REPOSITORY):].casefold())

    def _search_page(self, payload: object, known: dict[str, str]) -> tuple[list, set[str]]:
        if not isinstance(payload, dict) or payload.get("incomplete_results") is True:
            raise ApiError("unavailable")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ApiError("invalid_response")
        found = set()
        for item in items:
            match = self._search_match(item, known)
            if match:
                found.add(match)
        return items, found

    def _open_names(self, kind: str, names: list[str]) -> set[str]:
        """Which of names have an open pull request, from one owner-wide search.

        `GET /search/issues` with `org:` or `user:`. Repositories absent from it are not listed
        one by one. An incomplete search is a failure, not an empty owner."""
        known = {name.casefold(): name for name in names}
        found: set[str] = set()
        query = self._search_query(kind)
        for page in range(1, 11):
            items, page_found = self._search_page(
                self.api.one(_endpoint("search/issues", q=query, per_page=100, page=page)), known)
            found.update(page_found)
            if len(items) < 100:
                return found
        raise ApiError("unavailable")

    def _owner_kind(self, rows: list[dict]) -> str:
        kind = self._kind_from(rows) or self._kind
        if kind is None:
            kind = self._remember_kind(self.api.one("users/%s" % self.config.owner))
        return kind

    def _discover(self) -> tuple[tuple[str, ...], set[str]]:
        """Non-archived repositories of the owner, and which of them have an open pull request.

        A repository owned by anyone else refuses the whole discovery. Archived repositories
        are left out. The set is not cached: the next refresh sees a repository that appeared
        or disappeared."""
        rows = self._repository_rows()
        names = self._names_from(rows)
        if not names:
            return (), set()
        return tuple(names), self._open_names(self._owner_kind(rows), names)

    def _empty_repository(self, repository: str, inventory: dict, when) -> dict:
        return {"repository": repository, "subscription": inventory.get("subscription", "unknown"),
                "pulls": [], "errors": [], "sampled_at": iso_time(when),
                **({"evidence": inventory["evidence"]} if "evidence" in inventory else {})}

    def _not_loaded_repository(self, repository: str, rows: list[dict], inventory: dict) -> dict:
        known = inventory or {"subscription": "unknown"}
        return {"repository": repository, **known, "sampled_at": iso_time(self.clock()),
                "pulls": [self._unloaded_pull(repository, row, known, "request_budget_exhausted") for row in rows],
                "errors": [{"pull": row.get("number"), "code": "request_budget_exhausted"} for row in rows]}

    def _remember(self, names) -> None:
        keep = set(names)
        self._detailed_at = {name: when for name, when in self._detailed_at.items() if name in keep}
        self._attempted_at = {name: when for name, when in self._attempted_at.items() if name in keep}

    def _scope(self) -> tuple:
        if self.config.all_repositories:
            return self._discover()
        return self.config.repositories, set(self.config.repositories)

    def _mark_idle(self, names, open_names, listings: dict) -> None:
        if not self.config.all_repositories:
            return
        for repository in names:
            if repository not in open_names:
                listings[repository] = []

    def _list_one(self, repository: str, listings: dict, errors: list) -> bool:
        try:
            listings[repository] = self.api.items(
                _endpoint("repos/%s/pulls" % repository, state="open", per_page=100))
        except ApiError as error:
            errors.append({"repository": repository, "code": error.code})
            return error.code == "request_budget_exhausted"
        return False

    def _fill_unlisted(self, names, listings: dict, errors: list, budget_hit: bool) -> bool:
        for repository in names:
            if repository in listings or any(error.get("repository") == repository for error in errors):
                continue
            errors.append({"repository": repository, "code": "request_budget_exhausted"})
            budget_hit = True
        return budget_hit

    def _list_open(self, names, open_names) -> tuple[dict, list, bool]:
        listings: dict[str, list] = {}
        errors: list[dict] = []
        self._mark_idle(names, open_names, listings)
        budget_hit = False
        pending = [repository for repository in names if repository not in listings]
        for repository in self._by_need(pending):
            if budget_hit:
                break
            budget_hit = self._list_one(repository, listings, errors)
        return listings, errors, self._fill_unlisted(names, listings, errors, budget_hit)

    def _cached_or_unknown(self, repository: str, inventory) -> dict:
        return (inventory or {}).get(repository) or {"subscription": "unknown"}

    def _idle(self, repository: str, open_names) -> bool:
        return self.config.all_repositories and repository not in open_names

    def _read_empty_inventory(self, repository: str, inventory, errors: list) -> tuple:
        try:
            return self._known_inventory(repository, inventory), True, False
        except ApiError as error:
            if error.code == "request_budget_exhausted":
                return self._cached_or_unknown(repository, inventory), False, True
            errors.append({"repository": repository, "code": error.code})
            return None, False, False

    def _keep_empty(self, repository: str, known: dict, inventories: dict, by_repository: dict, when, spent: bool) -> None:
        if spent:
            inventories[repository] = known
        by_repository[repository] = self._empty_repository(repository, known, when)
        self._detailed_at[repository] = self.clock()

    def _store_empty(self, repository, rows, open_names, inventory, budget_hit, errors, inventories, by_repository, when) -> bool:
        if rows:
            return budget_hit
        if self._idle(repository, open_names):
            self._keep_empty(repository, {"subscription": "unknown"}, inventories, by_repository, when, False)
            return budget_hit
        if budget_hit:
            self._keep_empty(repository, self._cached_or_unknown(repository, inventory), inventories, by_repository, when, False)
            return budget_hit
        known, spent, hit = self._read_empty_inventory(repository, inventory, errors)
        if known is None:
            return budget_hit
        self._keep_empty(repository, known, inventories, by_repository, when, spent)
        return budget_hit or hit

    def _skip_detail(self, repository, rows, known, by_repository) -> bool:
        by_repository[repository] = self._not_loaded_repository(repository, rows, known)
        return True

    def _detail_one(self, repository, rows, inventory, inventories, by_repository, errors) -> bool:
        known = self._cached_or_unknown(repository, inventory)
        if self.api.calls >= self.api.max_calls:
            return self._skip_detail(repository, rows, known, by_repository)
        self._attempted_at[repository] = self.clock()
        try:
            repo_inventory = self._known_inventory(repository, inventory)
        except ApiError as error:
            errors.append({"repository": repository, "code": error.code})
            if error.code != "request_budget_exhausted":
                return False
            self._detailed_at.pop(repository, None)
            return self._skip_detail(repository, rows, known, by_repository)
        inventories[repository] = repo_inventory
        row, stopped = self._detail_repository(repository, rows, repo_inventory)
        row["sampled_at"] = iso_time(self.clock())
        by_repository[repository] = row
        if stopped:
            self._detailed_at.pop(repository, None)
            return True
        self._detailed_at[repository] = self.clock()
        return False

    def _detail_all(self, listings, inventory, inventories, by_repository, errors, budget_hit) -> bool:
        pending = [name for name, rows in listings.items() if rows]
        for repository in self._by_need(pending):
            if budget_hit:
                known = self._cached_or_unknown(repository, inventory)
                budget_hit = self._skip_detail(repository, listings[repository], known, by_repository)
                continue
            budget_hit = self._detail_one(repository, listings[repository], inventory, inventories, by_repository, errors)
        return budget_hit

    def _finish(self, now, names, rate, errors, inventories, by_repository, budget_hit) -> dict:
        if budget_hit:
            errors.append({"code": "request_budget_exhausted"})
        bots = self._bots(self.clock(), by_repository, names)
        repository_rows = [by_repository[name] for name in names if name in by_repository]
        return {"owner": self.config.owner, "sampled_at": iso_time(now), "repositories": repository_rows,
                "repository_names": list(names),
                "coverage": {"selected": len(names), "readable": len(repository_rows),
                             "label": coverage_label(self.config), "inventory": inventories},
                "bots": bots, "errors": errors, "partial": bool(errors) or bots["partial"],
                "api": {**rate, "calls": self.api.calls, "lowest_remaining": self.api.lowest_remaining,
                        "max_calls": self.api.max_calls}}

    def collect(self, inventory: dict[str, dict] | None = None) -> dict:
        """List every repository's open pull requests before any run or job detail.

        Detail is then spent on the repositories that have gone longest without it, so a busy
        head of the list cannot starve the tail on every pass. A pull request that was listed
        but not detailed stays visible with its checks not loaded yet. `repositories: all`
        learns the set, and which of it has an open pull request, before that, and does not
        read a repository the search says is empty."""
        now = self.clock().astimezone(timezone.utc)
        with self._job_lock:
            self._jobs = {}
        self._prune_job_cache(now)
        self.api.begin()
        rate = self.api.rate()
        names, open_names = self._scope()
        self._remember(names)
        listings, errors, budget_hit = self._list_open(names, open_names)
        inventories: dict[str, dict] = {}
        by_repository: dict[str, dict] = {}
        for repository, rows in listings.items():
            budget_hit = self._store_empty(
                repository, rows, open_names, inventory, budget_hit, errors, inventories, by_repository, now)
        budget_hit = self._detail_all(listings, inventory, inventories, by_repository, errors, budget_hit)
        return self._finish(now, names, rate, errors, inventories, by_repository, budget_hit)
