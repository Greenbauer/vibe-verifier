"""pull_sync.py's reads of GitHub: the default branch, every open pull request with its head's checks, and
whether the default branch merges only an up-to-date head. Each read goes through `gh` with the token in
GH_TOKEN; a read that gives no answer a plan can be built on raises CannotRead."""
import json
import subprocess

DEFAULT_BRANCH = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){defaultBranchRef{name}}}"
# The classic protection rule's "require branches to be up to date"; a ruleset's is read over REST.
STRICT = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
          "defaultBranchRef{branchProtectionRule{requiresStrictStatusChecks}}}}")
# `headRef.compare(headRef: $base).aheadBy` is how many commits the base has that the head lacks.
# With --needs-update the head's commit status of each name is read too (named() below); a token
# needs statuses read for it, so the field is asked for only then.
PULLS = """query($owner:String!,$name:String!,$base:String!%(variables)s){
  repository(owner:$owner,name:$name){
    pullRequests(states:OPEN,first:100,orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage}
      nodes{
        number isDraft isCrossRepository mergeable updatedAt baseRefName headRefOid
        labels(first:100){nodes{name}}
        headRef{compare(headRef:$base){aheadBy}}
        commits(last:1){nodes{commit{committedDate committedViaWeb parents{totalCount} statusCheckRollup{state contexts(first:1){
          checkRunCountsByState{state count} statusContextCountsByState{state count}}}%(status)s}}}
      }
    }
  }
}"""


class CannotRead(Exception):
    """GitHub did not give an answer a plan can be built on."""


def gh(*args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


def graphql(query, **variables):
    args = ["graphql", "-f", "query=" + query]
    for name, value in variables.items():
        args += ["-f", "%s=%s" % (name, value)]
    result = gh(*args)
    if result.returncode != 0:
        raise CannotRead((result.stderr or result.stdout).strip().split("\n")[0] or "gh failed")
    try:
        answer = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CannotRead("GitHub's answer was not JSON") from None
    if answer.get("errors") or not (answer.get("data") or {}).get("repository"):
        raise CannotRead("GitHub answered with errors or without the repository")
    return answer["data"]


def named(contexts):
    """The query's variables and status fields for the --needs-update names: one aliased context each."""
    aliases = range(len(contexts))
    return {"variables": "".join(",$c%d:String!" % i for i in aliases),
            "status": "\n          status{%s}" % " ".join("c%d:context(name:$c%d){state}" % (i, i) for i in aliases)}


WITHOUT = {"variables": "", "status": ""}


def read(repo, contexts=()):
    """The default branch and every open pull request, each head with the state of each commit status
    in `contexts`, aliased c0, c1, ... Without one the query does not ask for statuses at all: GitHub
    answers "Resource not accessible by integration" to a token that cannot read them, even for an
    empty name (2026-10-14)."""
    owner, _, name = repo.partition("/")
    default = graphql(DEFAULT_BRANCH, owner=owner, name=name)["repository"]["defaultBranchRef"]
    if not default:
        raise CannotRead("%s has no default branch" % repo)
    if contexts:
        values = {"c%d" % i: context for i, context in enumerate(contexts)}
        data = graphql(PULLS % named(contexts), owner=owner, name=name, base=default["name"], **values)
    else:
        data = graphql(PULLS % WITHOUT, owner=owner, name=name, base=default["name"])
    pulls = data["repository"]["pullRequests"]
    if pulls["pageInfo"]["hasNextPage"]:
        raise CannotRead("%s has more than 100 open pull requests" % repo)
    return default["name"], pulls["nodes"]


def requires_up_to_date(repo, base):
    """Whether the base merges only a head that is up to date with it, from a classic protection rule
    or an active ruleset: True, False, or None when this token could read neither. None is treated as
    False by the plan, which keeps every running check: the side that wastes time, not work."""
    owner, _, name = repo.partition("/")
    answers = []
    try:
        rule = graphql(STRICT, owner=owner, name=name)["repository"]["defaultBranchRef"]["branchProtectionRule"]
        answers.append(bool(rule and rule["requiresStrictStatusChecks"]))
    except (CannotRead, TypeError, KeyError):
        pass
    result = gh("repos/%s/rules/branches/%s?per_page=100" % (repo, base))
    try:
        rules = json.loads(result.stdout) if result.returncode == 0 else None
        if isinstance(rules, list):
            answers.append(any(rule.get("type") == "required_status_checks"
                               and (rule.get("parameters") or {}).get("strict_required_status_checks_policy")
                               for rule in rules))
    except json.JSONDecodeError:
        pass
    return any(answers) if answers else None
