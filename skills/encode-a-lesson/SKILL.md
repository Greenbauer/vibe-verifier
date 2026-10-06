---
name: encode-a-lesson
description: Turn a mistake that has happened twice into an enforced check (a type, an ast-grep rule, a helper, a runtime check) instead of a prose instruction. Use when the same mistake shows up a second time, in a review finding, a failed acceptance check or a fix pull request, or when asked to add a rule or make sure something never happens again.
---

# Encode a lesson

Agents copy the nearest code and skip prose, so a lesson kept only in a doc or a prompt is lost by the
next session. The second time the same mistake happens, encode it with the strongest mechanism that
would have caught both occurrences, in this order:

1. **Make the bad state unrepresentable in types**: a narrower parameter type, a required field, a
   union in place of a flag, a branded type that only the right constructor returns.
2. **An ast-grep rule** in the repository's rule directory whose message names the fix.
3. **A canonical helper** that does it right, plus a rule that points every call site at it.
4. **A runtime check** at the boundary (validation, an assertion) with a test that triggers it.

Stop at the first that fits. The rest of this skill is for a rule (2 and 3).

`<catalog>` is a clone of Vibe Verifier. `<manifest>` is the gate list: `.vibe-verifier` at the
repository root, or, when a CI wrapper keeps the list instead, a temporary file holding the
`repo-rules` line from the repository's `AGENTS.md` or `CLAUDE.md`. `<dir>` is the rule directory:
`.vibe-verifier-rules/` unless the manifest's `repo-rules` line passes `--rules DIR`. `<bad>` is the
commit that introduced the mistake and `<fix>` the commit that fixed it.

## Write the rule and its test

1. `<dir>/<id>.yml`: `id`, `language`, `rule`, a `message` that says what to write instead in one
   sentence, and a `note` saying where the replacement lives and how to call it. Leave `severity`
   unset to block; set `severity: hint` to observe it first. A rule sees only its language's file
   extensions: a `Tsx` rule sees no `.ts` file unless the directory's `sgconfig.yml` maps `*.ts` to
   tsx under `languageGlobs`.
2. `<dir>/tests/<id>-test.yml`: the `invalid` case is the real bad code from `<bad>`, trimmed to the
   smallest snippet that still shows the mistake; the `valid` case is the same code as `<fix>` wrote
   it. Add a `valid` case for every false alarm you find below.

## Prove it

Run the gate on the old commits with the new rule. A rule directory inside the repository is judged
from the base, where the new rule does not exist yet; an absolute `--rules` path is read as it is, so
copy the directory out first:

```bash
RULES=$(mktemp -d) && cp -R <dir>/. "$RULES"
WT=$(mktemp -d) && git worktree add -q --detach "$WT" <bad>
python3 <catalog>/gates/repo_rules.py --repo "$WT" --base-ref <bad>^ --rules "$RULES"
```

That must exit 1 and name the bad line. Then at the fix:

```bash
git -C "$WT" checkout -q --detach <fix>
python3 <catalog>/gates/repo_rules.py --repo "$WT" --base-ref <fix>^ --rules "$RULES"
python3 <catalog>/gates/repo_rules.py --repo "$WT" --all --rules "$RULES"
```

The first must exit 0. The second lists every finding in the tree: the file `<fix>` repaired must not
be in it. Any other finding is either the same mistake elsewhere (fix it in this pull request, or let
the ratchet hold it) or a false alarm (tighten the rule).

Replay the last 20 merged pull requests for false alarms (with merge commits, `--first-parent` makes
each commit's first parent the base its pull request was judged against):

```bash
for c in $(git log --first-parent --format=%H -20 origin/main); do
  git -C "$WT" checkout -q --detach "$c"
  python3 <catalog>/gates/repo_rules.py --repo "$WT" --base-ref "$c^" --rules "$RULES" > /dev/null
  echo "exit $? at $c"
done
git worktree remove --force "$WT"
```

Every `exit 1` is a pull request the rule would have blocked: either another occurrence of the mistake
(cite it in the pull request) or a false alarm (tighten the rule, add the code as a `valid` case, and
replay again). An `exit 2` means the gate could not run: fix that before trusting the replay (the
repository's first commit has no parent, so it always reports one).

## Finish

1. Regenerate the rules block so the prose follows the rule:

   ```bash
   <catalog>/bin/vibe-verifier rules-doc --repo . --manifest <manifest> --write AGENTS.md
   ```

2. Delete the prose instruction the rule replaces (in `AGENTS.md`, `CLAUDE.md`, a review prompt): the
   generated block now carries its message.
3. One pull request with the rule, its test, the regenerated block and the deleted prose. Its body
   names `<bad>` and `<fix>` and gives the replay result.
