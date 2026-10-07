---
name: follow-repo-rules
description: Follow a repository's executable code rules (Vibe Verifier's repo-rules gate, ast-grep rules) while changing its code. Use before editing code in a repository that has a .vibe-verifier manifest, a .vibe-verifier-rules directory or a generated rules block in AGENTS.md or CLAUDE.md, and again before pushing.
---

# Follow the repository's rules

This repository writes its established patterns as ast-grep rules, and Vibe Verifier's `repo-rules`
gate runs them. Each rule's message says what to write instead. Prose about the rules is generated
from the rule files, so the rule files are the source of truth.

`<catalog>` below is a clone of Vibe Verifier, ideally at the commit the repository pins (the SHA after
`vibe-verifier/actions/gates@` in its workflow, or in its organization's CI wrapper). `<manifest>` is
the gate list: `.vibe-verifier` at the repository root, or, when the repository has none because a CI
wrapper keeps its list, a temporary file holding the `repo-rules` line from the repository's
`AGENTS.md` or `CLAUDE.md`. `<base>` is the branch the pull request merges into.

## Before editing

1. Read the generated rules block. It sits between `<!-- BEGIN vibe-verifier rules-doc` and
   `<!-- END vibe-verifier rules-doc -->`, usually in `AGENTS.md` or `CLAUDE.md`:

   ```bash
   grep -rl "BEGIN vibe-verifier rules-doc" --include='*.md' .
   ```

   No block? Print it: `<catalog>/bin/vibe-verifier rules-doc --repo . --manifest <manifest>`.

2. Note every rule whose language and globs cover the files you will touch. A blocking rule fails the
   pull request; an advisory one is reported only. Follow both.
3. Before writing new code, find how the repository already does the same thing and reuse it. Prefer
   extending the established helper over adding a second way to do it; a rule that names a helper
   means that helper is the one way.

## Before pushing

Commit first: the gates read git history, not uncommitted edits. Then run:

```bash
<catalog>/bin/vibe-verifier run --repo . --manifest <manifest> --base-ref <base>
```

Exit 0 is a pass, 1 means findings, 2 means a gate could not run. Exit 2 is never a pass: read the
error and fix its cause (a missing base ref, a rule file that cannot be read, a failing rule test).

## Fixing findings

- Do what the message says, and what the note says when the finding has one. Do not reshape the code
  only to stop the pattern from matching; that hides the mistake the rule exists to catch.
- The gate counts each rule's findings per file against the merge base. A finding listed on a line
  you did not write is still yours when your change raised that file's count.
- Never add an `// ast-grep-ignore: <rule-id>` comment without a stated reason. Write the reason in a
  comment on the line above it, say why this case is the exception, and mention it in the pull
  request. If the rule itself is wrong, change the rule instead, in its own pull request (see the
  `encode-a-lesson` skill).
- A `rules-doc` finding means the block no longer matches the rule files. Regenerate it; never edit
  it by hand:

  ```bash
  <catalog>/bin/vibe-verifier rules-doc --repo . --manifest <manifest> --write AGENTS.md
  ```

  (use the file the finding names). A pull request that changes a rule file regenerates the block in
  the same commit.
