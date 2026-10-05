# Rule packs

A pack is a directory of [ast-grep](https://ast-grep.github.io/) rules that the `repo-rules` gate
runs for any repository that subscribes to it:

```
# .vibe-verifier
repo-rules --pack <name>
```

The gate reads the pack from the catalog revision the repository pins, so a change to a pack
reaches a consumer the way a new gate does: by bumping its action pin. `rules/` is a release path
for that reason. How the gate runs rules, the ratchet, and base control are in the
[gate contract](../docs/GATE-CONTRACT.md#repository-rules).

## What belongs in a public pack

Only a rule that is true for any repository of that stack: a TypeScript pack holds rules every
TypeScript codebase should follow, whatever it builds and whoever owns it. A rule that encodes one
organization's or one repository's choices (its logger, its API client, its folder layout, the
helper it wants used instead of a library call) does not belong here, however good the rule is. It
lives with its owner:

- in the repository's own rule directory, `.vibe-verifier-rules/` by default; or
- in its organization's wrapper, which writes the rules to a directory outside the repository and
  passes that absolute path as `--rules`.

This repository is public, so a pack also names no private organization, product, repository or
person, not even in a note or a test case.

## Layout

A pack has the same layout as a repository's rule directory:

```
rules/<name>/
  <rule>.yml                 one ast-grep rule (or several, separated by ---)
  tests/<rule>-test.yml      its ast-grep rule test
  sgconfig.yml               optional: only its languageGlobs are read
```

**A rule sees only the files its `language` owns by extension.** A `language: Tsx` rule covers
`.tsx` files and never `.ts` ones, and says nothing about the files it skipped. To cover both, write
the rule once per language, or map the extra extensions in the directory's `sgconfig.yml`:

```yaml
languageGlobs:
  tsx: ['*.ts']
```

The mapping applies to that directory's rules and tests only: each pack and rule directory runs as
its own ast-grep project, so a pack's mapping never changes what a repository's rules see, nor the
other way round. Inside the directory it applies to every rule: with `*.ts` mapped to tsx, the
directory's `language: TypeScript` rules no longer see `.ts` files.

Every rule needs:

- an `id`, unique across every pack and rule directory a repository runs together;
- a `message` that says what to write instead, not only what is wrong: the person or agent reading
  it has the log and nothing else;
- a `note` when the fix needs more than one line (where the replacement lives, how to call it);
- a rule test in its own directory's `tests/`, with at least one `valid` and one `invalid` case,
  which passes.

A rule blocks pull requests unless it says `severity: warning`, `info` or `hint`, which print as
advisory and never fail. Leave `severity` unset or `error` for a rule that should block; use `hint`
for one still being observed. The gate counts findings per rule and file against the merge base, so
a rule only ever blocks a file that gained a violation; see the
[gate contract](../docs/GATE-CONTRACT.md#repository-rules).

A rule without those makes the gate exit 2 for every subscriber, so the catalog's tests run each
pack through the gate.

## Packs

| Pack | Rules | Purpose |
|---|---|---|
| `example` | `no-debugger` (TypeScript) | Exercises the mechanism in the tests and shows the layout. |
| `typescript` | `no-memoized-rejecting-init`, `no-resolve-undefined-in-catch`, `no-raw-main-entry-check` (`.ts`/`.tsx`/`.js` via its `sgconfig.yml`) | Bugs that pass type checks and unit tests: a cached promise that replays one failure forever, a catch block that turns a failure into success, an entry-point check that is false on any path with a space. |
| `supabase` | `supabase-service-role-not-ssr-client`, `supabase-upsert-ignore-duplicates` | Client and PostgREST behavior that silently changes who a query runs as, or turns a lost insert race into an error. |

Each pack rule was replayed before it shipped; new rules follow the same bar. Rules come into a pack after being replayed against the history of the repositories it would
judge (`bin/vibe-verifier run --base-ref <sha>` on each historical pull request, or the gate with
`--all`), so what a pack blocks is known before anyone subscribes to it.
