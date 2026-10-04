#!/usr/bin/env python3
"""Fetch PR metadata, fully paginating files and commits as well as PRs."""
import argparse
import json
import subprocess

QUERY = '\nquery($owner:String!,$name:String!,$endCursor:String){\n  repository(owner:$owner,name:$name){\n    pullRequests(first:50,after:$endCursor,states:[MERGED,CLOSED,OPEN],orderBy:{field:CREATED_AT,direction:ASC}){\n      pageInfo{hasNextPage endCursor}\n      nodes{\n        number title state createdAt mergedAt closedAt author{login}\n        headRefName baseRefName additions deletions changedFiles\n        mergeCommit{oid}\n        commits{totalCount}\n        reviewThreads(first:100){totalCount nodes{isResolved comments(first:1){nodes{author{login}}}}}\n        comments{totalCount}\n        files(first:100){nodes{path}}\n        prCommits: commits(first:100){nodes{commit{oid}}}\n      }\n    }\n  }\n}'


def query(owner, name, document, selection, number=None):
    command = ["gh", "api", "graphql", "--paginate", "-F", f"owner={owner}",
               "-F", f"name={name}", "-f", f"query={document}", "--jq", selection]
    if number is not None:
        command += ["-F", f"number={number}"]
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def complete_node(owner, name, pr):
    for field, key, expected, node in (
        ("files", "files", pr["changedFiles"], "path"),
        ("commits", "prCommits", pr["commits"]["totalCount"], "commit{oid}"),
    ):
        if len(pr[key]["nodes"]) == expected:
            continue
        document = """query($owner:String!,$name:String!,$number:Int!,$endCursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){
            %s(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{%s}}
          }}}
        """ % (field, node)
        nodes = query(owner, name, document,
                      f".data.repository.pullRequest.{field}.nodes[]", pr["number"])
        if len(nodes) != expected:
            raise ValueError(f"PR #{pr['number']} {field} changed during collection or is incomplete; retry the snapshot")
        pr[key]["nodes"] = nodes
    return pr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner")
    parser.add_argument("name")
    args = parser.parse_args()
    for pr in query(args.owner, args.name, QUERY, ".data.repository.pullRequests.nodes[]"):
        print(json.dumps(complete_node(args.owner, args.name, pr)))


if __name__ == "__main__":
    main()
