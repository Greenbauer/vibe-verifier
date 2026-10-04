#!/usr/bin/env bash
# usage: fetch_prs.sh OWNER NAME > out.json
set -euo pipefail
gh api graphql --paginate -F owner="$1" -F name="$2" -f query='
query($owner:String!,$name:String!,$endCursor:String){
  repository(owner:$owner,name:$name){
    pullRequests(first:50,after:$endCursor,states:[MERGED,CLOSED,OPEN],orderBy:{field:CREATED_AT,direction:ASC}){
      pageInfo{hasNextPage endCursor}
      nodes{
        number title state createdAt mergedAt closedAt author{login}
        headRefName baseRefName additions deletions changedFiles
        mergeCommit{oid}
        commits{totalCount}
        reviewThreads(first:100){totalCount nodes{isResolved comments(first:1){nodes{author{login}}}}}
        comments{totalCount}
        files(first:100){nodes{path}}
        prCommits: commits(first:100){nodes{commit{oid}}}
      }
    }
  }
}' --jq '.data.repository.pullRequests.nodes[]'
