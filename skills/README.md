# Agent skills

Skills for coding agents working in a repository that subscribes to Vibe Verifier. The first two are for
the `repo-rules` gate and carry the same principle as the gate: agents copy the nearest code and skip prose, so a repository's
patterns live in executable rules whose message names the fix, the prose about them is generated from
the rules (`bin/vibe-verifier rules-doc`), and a mistake that repeats becomes a rule.

| Skill | When it applies |
|---|---|
| [`follow-repo-rules`](follow-repo-rules/SKILL.md) | Before editing and before pushing: read the generated rules block, run the gates, fix each finding the way its message says, never suppress one without a reason, extend the established helper rather than add a second way. |
| [`encode-a-lesson`](encode-a-lesson/SKILL.md) | When the same mistake happens a second time: encode it as a type, a rule, a helper or a runtime check, in that order; for a rule, test it with the real bad code and its fix, replay recent history for false alarms, regenerate the block and delete the prose it replaces. |
| [`write-acceptance-criteria`](write-acceptance-criteria/SKILL.md) | When opening a pull request, editing its description, or when QAE fails: write `## Acceptance criteria` the explorer can check on the preview, check the section with `bin/vibe-verifier criteria` before pushing, and re-run QAE after a description edit instead of pushing. |

Each is one `SKILL.md` with YAML frontmatter holding `name` (lowercase letters, digits and hyphens,
the same as its folder) and `description`, then the instructions.

## Install

This repository is a Claude Code plugin marketplace (`.claude-plugin/marketplace.json`), and its one
plugin, `vibe-verifier-skills`, is these skills. So every repository gets the same skills from this one
copy, and no repository commits its own. Each person installs it once, for every repository they open:

```bash
claude plugin marketplace add Greenbauer/vibe-verifier
claude plugin install vibe-verifier-skills@vibe-verifier --scope user
```

The skills are then `/vibe-verifier-skills:write-acceptance-criteria` and so on, and Claude uses each one
when a task matches its description, as it does a project's own skills. Updates from a third-party
marketplace are off by default: run `claude plugin marketplace update vibe-verifier`, or turn on
`autoUpdate` for the marketplace. Without a GitHub SSH key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.
Plugins load in the CLI, the IDE extensions and local desktop sessions, not in cloud sessions; a
`claude-code-action` workflow passes `plugin_marketplaces` and `plugins` inputs instead
([Claude Code: plugins](https://code.claude.com/docs/en/plugins/install), read 2026-10-14).

**Cursor** does not read Claude Code plugins. It loads a project's skills from `.agents/skills/`,
`.cursor/skills/` and `.claude/skills/` ([Cursor: skills](https://cursor.com/docs/skills), read
2026-10-05), so a repository whose agents run in Cursor would need a copy there; none is kept today.

`skills/` is not a release path (nothing in a workflow reads it), so a change here does not mark a
consumer's pin stale.
