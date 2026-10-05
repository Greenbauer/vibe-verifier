# Agent skills

Two skills for coding agents working in a repository that subscribes to the `repo-rules` gate. They
carry the same principle as the gate: agents copy the nearest code and skip prose, so a repository's
patterns live in executable rules whose message names the fix, the prose about them is generated from
the rules (`bin/vibe-verifier rules-doc`), and a mistake that repeats becomes a rule.

| Skill | When it applies |
|---|---|
| [`follow-repo-rules`](follow-repo-rules/SKILL.md) | Before editing and before pushing: read the generated rules block, run the gates, fix each finding the way its message says, never suppress one without a reason, extend the established helper rather than add a second way. |
| [`encode-a-lesson`](encode-a-lesson/SKILL.md) | When the same mistake happens a second time: encode it as a type, a rule, a helper or a runtime check, in that order; for a rule, test it with the real bad code and its fix, replay recent history for false alarms, regenerate the block and delete the prose it replaces. |

Each is one `SKILL.md` with YAML frontmatter holding `name` (lowercase letters, digits and hyphens,
the same as its folder) and `description`, then the instructions.

## Install

Copy each skill's folder into the consumer repository's `.claude/skills/` and commit it:

```bash
mkdir -p .claude/skills
cp -R <catalog>/skills/follow-repo-rules <catalog>/skills/encode-a-lesson .claude/skills/
```

`<catalog>` is a clone of this repository, ideally at the commit the consumer pins.

- **Claude Code** loads a project's skills from `.claude/skills/<skill-name>/SKILL.md`; `name` is
  optional there and defaults to the folder's name, and `description` is recommended. A skill folder
  there may also be a symlink to a directory elsewhere on disk
  ([Claude Code: skills](https://code.claude.com/docs/en/skills), read 2026-10-05).
- **Cursor** loads a project's skills from `.agents/skills/` and `.cursor/skills/`, and for backward
  compatibility also from `.claude/skills/`, so the one copy above serves both. It requires `name` and
  `description`, and `name` must match the folder ([Cursor: skills](https://cursor.com/docs/skills),
  read 2026-10-05). Its documentation says nothing about symlinks, so a symlinked skill in Cursor is
  unverified: copy rather than link where Cursor is used.

A copy does not update itself: copy again after bumping the catalog pin. `skills/` is not a release
path (nothing in a workflow reads it), so a change here does not mark a consumer's pin stale.
