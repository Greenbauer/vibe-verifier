#!/usr/bin/env python3
"""Write what GitHub records about the last QAE walk of a pull request, for `vibe-verifier criteria
--carry`, which decides from it whether that walk's verdict still holds for the current head.

An update that merges the default branch into a pull request (the pull sync, or its author) starts the
QAE again, and the explorer walks every criterion again even when nothing a browser could see changed.
This reads, from GitHub's own records and never from a comment:

  - the newest earlier run of this same workflow for this pull request (its own number in the run's
    pull_requests, so a run of another pull request from the same branch name never counts) that
    concluded success and left explorer evidence (an artifact named qae-artifacts-*): a run that walked
    the criteria and passed. A run that walked nothing, a carried one among them, leaves no such
    artifact and is skipped. Its workflow file's path comes with it.
  - which files differ between that run's head and the current head, and whether that head is an
    ancestor of the current one (GitHub's compare).
  - how many commits of the base branch the current head lacks: the walk tested the head merged with
    the base, so only a head that has the whole base is the tree a carried verdict would vouch for.
  - when the pull request was opened and its body last edited, and a digest of the body this job read:
    the criteria live there, and a later job that reads the body again decides on the same text or not
    at all.

It decides nothing. Any read that fails leaves the run out (`"run": null`), and no run means no carry.

    carry_facts.py --repo OWNER/NAME --pr N --head SHA --branch REF --base REF --run-id ID --body FILE --out FILE

GH_TOKEN must be able to read actions, contents and pull requests.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from urllib.parse import quote

# How many earlier successful runs are looked at for one that walked. A carried or not-required run in
# between leaves no evidence, so the walk can be a few runs back; past this it is not worth the reads.
CANDIDATES = 5
# GitHub's compare lists at most 300 files; a list that long may be cut, so it is never trusted.
COMPARE_FILES = 300


class CannotRead(Exception):
    pass


def gh(*args):
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRead((result.stderr.strip() or "gh api failed").splitlines()[-1])
    return json.loads(result.stdout)


def walked_run(repo, pr, branch, head, run_id):
    """The newest earlier run of this run's workflow for this pull request that passed and left explorer evidence."""
    workflow = gh("repos/%s/actions/runs/%s" % (repo, run_id))["workflow_id"]
    runs = gh("repos/%s/actions/runs?event=pull_request&status=success&per_page=50&branch=%s" % (repo, quote(branch, safe="")))
    looked = 0
    for run in runs["workflow_runs"]:
        if run["workflow_id"] != workflow or run["head_sha"] == head or str(run["id"]) == str(run_id):
            continue
        if not any(str(p.get("number")) == str(pr) for p in run.get("pull_requests") or []):
            continue
        looked += 1
        if looked > CANDIDATES:
            return None
        artifacts = gh("repos/%s/actions/runs/%s/artifacts?per_page=100" % (repo, run["id"]))["artifacts"]
        if any(a["name"].startswith("qae-artifacts-") and not a["expired"] for a in artifacts):
            return {"id": run["id"], "head": run["head_sha"], "created_at": run["created_at"], "url": run["html_url"],
                    "path": run["path"]}
    return None


def changed_since(repo, base, head):
    compare = gh("repos/%s/compare/%s...%s" % (repo, base, head))
    files = compare.get("files") or []
    names = [f["filename"] for f in files] + [f["previous_filename"] for f in files if f.get("previous_filename")]
    return {"status": compare["status"], "files": names, "cut": len(files) >= COMPARE_FILES}


def behind_base(repo, base, head):
    return gh("repos/%s/compare/%s...%s" % (repo, quote(base, safe=""), head))["behind_by"]


def pull_dates(repo, pr):
    """When the pull request was opened and when its body was last edited (None: never)."""
    owner, name = repo.split("/", 1)
    query = "query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){createdAt lastEditedAt}}}"
    data = gh("graphql", "-f", "query=" + query, "-f", "o=" + owner, "-f", "n=" + name, "-F", "p=%s" % pr)
    pull = data["data"]["repository"]["pullRequest"]
    return pull["createdAt"], pull["lastEditedAt"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for name in ("--repo", "--pr", "--head", "--branch", "--base", "--run-id", "--body", "--out"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    facts = {"head": args.head, "run": None}
    try:
        run = walked_run(args.repo, args.pr, args.branch, args.head, args.run_id)
        if run:
            opened, edited = pull_dates(args.repo, args.pr)
            with open(args.body, "rb") as handle:
                body = hashlib.sha256(handle.read()).hexdigest()
            facts.update(run=run, compare=changed_since(args.repo, run["head"], args.head),
                         base_behind=behind_base(args.repo, args.base, args.head),
                         pull_created_at=opened, body_edited_at=edited, body_sha256=body)
    except (CannotRead, KeyError, TypeError, ValueError, OSError) as error:
        print("::warning::no earlier QAE walk can be carried forward: %s" % error)
        facts = {"head": args.head, "run": None}
    with open(args.out, "w") as handle:
        json.dump(facts, handle, indent=1)
    print("earlier walk: %s" % (facts["run"]["url"] if facts["run"] else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
