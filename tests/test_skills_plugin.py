"""The catalog is a Claude Code plugin marketplace whose plugin is skills/: one copy of the agent skills
for every repository, installed per person instead of committed into each repository."""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTMATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)


def frontmatter(path):
    with open(path) as handle:
        match = FRONTMATTER.match(handle.read())
    fields = {}
    for line in (match.group("body").splitlines() if match else []):
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


class SkillsPlugin(unittest.TestCase):
    def test_the_marketplace_lists_the_repository_root_as_its_skills_plugin(self):
        # The root holds skills/ and no other plugin component, so the plugin is exactly the skills.
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as handle:
            market = json.load(handle)
        self.assertEqual(market["name"], "vibe-verifier")
        self.assertTrue(market["owner"]["name"])
        self.assertEqual([(p["name"], p["source"]) for p in market["plugins"]], [("vibe-verifier-skills", ".")])
        for component in ("commands", "agents", "hooks", ".mcp.json", "output-styles"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, component)), component)

    def test_every_skill_names_its_folder_and_says_when_it_applies(self):
        # Cursor requires name to match the folder; Claude picks a skill by its description.
        skills = os.path.join(ROOT, "skills")
        folders = sorted(d for d in os.listdir(skills) if os.path.isdir(os.path.join(skills, d)))
        self.assertIn("write-acceptance-criteria", folders)
        for folder in folders:
            fields = frontmatter(os.path.join(skills, folder, "SKILL.md"))
            self.assertEqual(fields.get("name"), folder)
            self.assertTrue(fields.get("description"), folder)


if __name__ == "__main__":
    unittest.main()
