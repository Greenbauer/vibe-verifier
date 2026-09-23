#!/usr/bin/env python3
"""A stand-in `gh` for the consumers and apply-down tests. Serves repository files from a fixture
directory instead of GitHub and records every call, so a test can assert what was written.

    FAKE_GH_ROOT/<owner>/<name>/<path>   the file `gh api repos/<owner>/<name>/contents/<path>` returns
    FAKE_GH_ROOT/<owner>/<name>/.broken  make every read of that repository fail with HTTP 500
    FAKE_GH_ROOT/<owner>/<name>/.open-pr an already-open pin-bump PR, served to the head query
    FAKE_GH_ROOT/<owner>/<name>/.native  JSON overriding that repository's native enforcement:
                                         {"allowed_actions", "sha_pinning_required", "patterns_allowed", "alerts"}
    FAKE_GH_ROOT/<owner>/<name>/.id      the repository id `gh api repos/<owner>/<name>` answers (default 1)
    FAKE_GH_ROOT/<owner>/.rulesets.json  the organization's rulesets, a JSON list of full ruleset objects;
                                         absent, `orgs/<owner>/rulesets` answers 404, as for a user account
    FAKE_GH_ROOT/calls.log               one JSON object per call: {method, endpoint, content?, body?}

A repository with no `.native` file answers as a fully enforced one (SHA pinning on, the actions
allowlist `selected`, Dependabot alerts on), so a test says only what it is actually about.

`gh repo list <owner>` answers with every <owner>/<name> directory present; writes (POST/PUT) are
recorded and acknowledged without changing the fixture, because what the tests check is the bytes
the tool sends. A JSON body sent with `--input -` is recorded as `body`.
"""
import base64
import json
import os
import sys

root = os.environ["FAKE_GH_ROOT"]
argv = sys.argv[1:]


def record(method, endpoint, **extra):
    with open(os.path.join(root, "calls.log"), "a") as handle:
        handle.write(json.dumps(dict(method=method, endpoint=endpoint, **extra)) + "\n")


def fields():
    out = {}
    for index, item in enumerate(argv):
        if item == "-f" and index + 1 < len(argv):
            name, _, value = argv[index + 1].partition("=")
            out[name] = value
    return out


def broken(repo):
    return os.path.exists(os.path.join(root, repo, ".broken"))


def native(repo):
    path = os.path.join(root, repo, ".native")
    return json.load(open(path)) if os.path.exists(path) else {}


if argv[:2] == ["repo", "list"]:
    owner = argv[2]
    base = os.path.join(root, owner)
    names = sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))) if os.path.isdir(base) else []
    print("\n".join("%s/%s" % (owner, name) for name in names))
    sys.exit(0)

if argv[:2] == ["pr", "create"]:
    repo = argv[argv.index("--repo") + 1]
    record("PR", repo)
    print("https://github.com/%s/pull/1" % repo)
    sys.exit(0)

if argv[:1] == ["api"]:
    endpoint = argv[1]
    method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
    given = fields()
    extra = {"content": base64.b64decode(given["content"]).decode()} if "content" in given else {}
    if "--input" in argv and argv[argv.index("--input") + 1] == "-":
        extra["body"] = json.load(sys.stdin)
    record(method, endpoint, **extra)
    if endpoint.startswith("orgs/"):
        owner, rest = endpoint.split("/")[1], endpoint.split("?")[0].split("/")[2:]
        path = os.path.join(root, owner, ".rulesets.json")
        if rest[:1] != ["rulesets"] or not os.path.exists(path):
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            sys.exit(1)
        rulesets = json.load(open(path))
        if method != "GET":
            print(json.dumps({}))
        elif len(rest) == 1:
            print(json.dumps([{"id": r["id"], "name": r["name"], "enforcement": r["enforcement"]} for r in rulesets]))
        else:
            print(json.dumps(next(r for r in rulesets if str(r["id"]) == rest[1])))
        sys.exit(0)
    repo = endpoint[len("repos/"):].split("/contents/")[0].split("/git/")[0].split("/pulls")[0].split("?")[0].rstrip("/")
    repo = "/".join(repo.split("/")[:2])
    if broken(repo):
        print("gh: Internal Server Error (HTTP 500)", file=sys.stderr)
        sys.exit(1)
    if method != "GET":
        print(json.dumps({"commit": {"sha": "0" * 40}, "ref": "refs/heads/x", "object": {"sha": "0" * 40}}))
        sys.exit(0)
    if endpoint.endswith("/actions/permissions/selected-actions"):
        print(json.dumps({"github_owned_allowed": True, "verified_allowed": False,
                          "patterns_allowed": native(repo).get("patterns_allowed", ["anthropics/claude-code-action@*"])}))
        sys.exit(0)
    if endpoint.endswith("/actions/permissions"):
        print(json.dumps({"enabled": True, "allowed_actions": native(repo).get("allowed_actions", "selected"),
                          "sha_pinning_required": native(repo).get("sha_pinning_required", True)}))
        sys.exit(0)
    if endpoint.endswith("/vulnerability-alerts"):
        if native(repo).get("alerts", True):
            sys.exit(0)  # 204 No Content: gh prints nothing and exits 0
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    if "/contents/" in endpoint:
        path = endpoint.split("/contents/", 1)[1].split("?")[0]
        target = os.path.join(root, repo, path)
        if os.path.isdir(target):
            print(json.dumps([{"name": name, "path": path + "/" + name, "type": "file"}
                              for name in sorted(os.listdir(target)) if os.path.isfile(os.path.join(target, name))]))
            sys.exit(0)
        if not os.path.isfile(target):
            print("gh: Not Found (HTTP 404)", file=sys.stderr)
            sys.exit(1)
        with open(target, "rb") as handle:
            body = handle.read()
        # `--jq .content` asks for the raw field; anything else wants the object.
        encoded = base64.b64encode(body).decode()
        print(encoded if "--jq" in argv and argv[argv.index("--jq") + 1] == ".content"
              else json.dumps({"content": encoded, "sha": "b" * 40}))
        sys.exit(0)
    if "/git/ref/heads/" in endpoint:
        print(json.dumps({"object": {"sha": "a" * 40}}))
        sys.exit(0)
    if "/pulls" in endpoint:
        open_pr = os.path.join(root, repo, ".open-pr")
        print(json.dumps([{"html_url": open(open_pr).read().strip()}] if os.path.exists(open_pr) else []))
        sys.exit(0)
    if endpoint.startswith("repos/") and endpoint.count("/") == 2:
        id_file = os.path.join(root, repo, ".id")
        print(json.dumps({"default_branch": "main", "id": int(open(id_file).read()) if os.path.exists(id_file) else 1}))
        sys.exit(0)

print("fake gh: unsupported invocation %r" % argv, file=sys.stderr)
sys.exit(64)
