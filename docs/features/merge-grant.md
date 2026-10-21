# Merge grants

`bin/vibe-verifier merge-grant --repo OWNER/NAME --pr N [--policy FILE]` says whether a merge policy lets an agent merge a pull request without a human review: `eligible: <grant>` (exit 0) or `not eligible: <reason>` (exit 1); exit 2 when the policy is malformed. It judges only what the pull request changes, and for a grant that asks, its repository and title: never its checks, reviews or mergeability, which a merge guard checks on its own. It is a tool, not a gate. A merge guard calls it before letting an agent merge, and the dashboard's "Agent mergeable" pill reads the same decision (`dashboard/merge_grant.py`), so the two agree by construction.

## Surfaces

- command: `merge-grant`
- source: `gates/_merge_grant.py`
- source: `gates/_merge_grant_cli.py`
- source: `gates/_merge_grant.default.json`
- source: `dashboard/merge_grant.py`

## The policy

Each team writes its own: which paths are low-risk, which stay a person's, which repositories or titles an agent may merge whole. A policy is one JSON file:

```json
{
  "version": 1,
  "extends": "default",
  "categories": {"scripts": [{"path": "^scripts/ci/"}]},
  "grants": [
    {"name": "tooling repositories", "repos": ["acme/infra-*"], "exclusions": ["codeowners"]},
    {"name": "fix titles", "repos": ["acme/app"], "title": "^fix(\\([^)]*\\))?: ",
     "exclusions": ["database", "codeowners", "security-names", "rls", "workflow-security"]},
    {"name": "non-product grant", "categories": ["ci", "docs", "build", "dependencies", "tests", "scripts"],
     "exclusions": ["database", "codeowners", "security-names", "rls", "workflow-security"]}
  ]
}
```

- **categories** name path sets. An entry is `{"path": RE}` (a Python regular expression, searched; `"ignore_case": true` to fold case). An entry with `"json_changes_only": [keys]` is judged on every path it matches, even one another category also takes (an `e2e/package.json` is still a `package.json`), and holds only when nothing outside those keys changed between the base and the head (`pnpm.overrides` names a nested key), and `"json_unchanged": [keys]` only when those keys (dotted names nested ones too) did not change. An absent file reads as `{}`; a file that is not a JSON object is a no.
- **exclusions** keep a merge a person's. `{"description", "path", "except"}` is a hit when any changed path (a rename's old path too) matches `path` and not `except`; `ignore_case` folds case for `path` only, so `except: "\\.mdx?$"` exempts `README.md` and not `SECURITY.MD`. With `"lines": {"changed": RE, "removed": RE, "strip": RE, "ignore_case": bool}`, `path` only selects the files to read, and the hit is an added or removed line matching `changed`, or a removed line matching `removed`, after `strip` is taken out of each line.
- **grants** are tried in order, and the first that holds decides. `repos` (`owner/name` or `owner/*` patterns, case-blind) and `title` scope a grant; without them it covers every repository and title. `categories` is the list every changed path must fall in, or `"any"` (the default). `exclusions` names the exclusions that apply. A grant with no categories and no exclusions reads nothing from GitHub.
- **`"extends": "default"`** starts from [`gates/_merge_grant.default.json`](../../gates/_merge_grant.default.json): categories and exclusions are added or replaced by name, and `grants`, when given, replaces the default's list. Without `--policy` the default decides: its one grant, the non-product grant, covers a pull request whose every path is CI configuration, Markdown, build or tool config, dependencies (lockfiles, and a `package.json` that changed only its dependency keys) or tests, with no migration, SQL, `supabase/`, CODEOWNERS, auth- or security-named file, or workflow line with a write scope, a secret other than `GITHUB_TOKEN`, `pull_request_target` or `id-token`, or a removed permissions line.

A policy that names private repositories lives with its owner, never in this public repository: the merge guard and the dashboard host each read it from a path of their own (`merge_policy_file` in the [dashboard configuration](../dashboard.md#configuration-contract)).

## Reach

From a shell with `gh` signed in: `bin/vibe-verifier merge-grant --repo acme/app --pr 12 --policy ~/merge-policy.json`. A merge guard runs that before `gh pr merge` and refuses on anything but exit 0, passing `--gh` when it must bypass a wrapper. The dashboard reads it for every pull request when `merge_policy_file` is set, through its own budgeted REST reads ([dashboard data](../dashboard-data.md)).

## Verify

- `tests/test_merge_grant.py::test_each_category_alone_and_mixed`
- `tests/test_merge_grant.py::test_product_and_left_out_files`
- `tests/test_merge_grant.py::test_a_rename_is_judged_at_its_old_path`
- `tests/test_merge_grant.py::test_exclusions`
- `tests/test_merge_grant.py::test_package_json_dependency_keys_only`
- `tests/test_merge_grant.py::test_vercel_headers_unchanged`
- `tests/test_merge_grant.py::test_a_content_condition_holds_even_where_another_category_takes_the_path`
- `tests/test_merge_grant.py::test_markdown_is_exempt_only_by_its_lowercase_extension`
- `tests/test_merge_grant.py::test_unreadable_is_a_no`
- `tests/test_merge_grant_workflows.py::test_lines_that_stay_a_persons`
- `tests/test_merge_grant_workflows.py::test_removed_permission_lines`
- `tests/test_merge_grant_workflows.py::test_the_token_carve_out_and_line_breaks_cannot_hide_a_line`
- `tests/test_merge_grant_workflows.py::test_a_permissions_block_moved_to_another_job_is_removed_there`
- `tests/test_merge_grant_workflows.py::test_a_case_only_change_of_a_scope_line_is_still_a_change`
- `tests/test_merge_grant.py::test_whitelist_reads_nothing_and_matches_patterns_case_blind`
- `tests/test_merge_grant.py::test_title_grant_allows_product_code_not_exclusions`
- `tests/test_merge_grant.py::test_a_team_can_replace_and_add_categories`
- `tests/test_merge_grant.py::test_json_unchanged_reads_a_dotted_key_as_nested`
- `tests/test_merge_grant.py::test_rejects`
- `tests/test_merge_grant.py::test_short_list_and_rename`
- `tests/test_dashboard_merge_grant.py::test_one_reading_per_head_base_and_title`
- `tests/test_dashboard_merge_grant.py::test_the_pull_carries_the_grant_that_covers_it`

## Gotchas

- The base is read at the base branch's current head (the guard's `baseRefOid`, the dashboard's `base.sha`), not at the merge base: a content condition compares the pull request's head with where the base is now.
- Changed lines come from a line diff in which each line carries the less-indented lines above it (its YAML keys), so a block moved under another job counts as removed and added, and every line under a renamed key counts as changed: stricter than a bare `diff`, never looser.
- GitHub lists at most 3000 files of a pull request; a longer one is always a no, because the listing is shorter than its count of changed files.
