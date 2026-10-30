"""The fixture the `vibe-verifier qa-limit` tests share: explore jobs, their steps and failure annotations served by
the fake `gh` (tests/fake_gh.py), and the command run against them."""
import json
from pathlib import Path

from helpers import runner
from qa_review_fixture import HEAD, GhFixture

EXPLORER_STEP = "Explore the acceptance criteria in a real browser"


class QaLimitFixture(GhFixture):
    # The limit messages as the lanes print them.
    CLAUDE_LIMIT = ("Action failed with error: SDK execution error: Error: Claude Code returned an error result: "
                    "You've hit your limit · resets 9am (UTC)")  # Greenbauer/zack.land run 38286901786
    CODEX_LIMIT = ("Codex ended with an error: You've hit your usage limit. Visit https://chatgpt.com/settings/usage "
                   "to purchase more credits or try again at Oct 13th, 2026 9:00 AM.")

    def explorers(self, *jobs):
        """Each job a (name, conclusion, [failure annotation messages], [failed steps]: by default the explorer's when
        it failed); its id is its position plus 100."""
        site = Path(self.fixtures) / "acme" / "site"
        (site / ".annotations").mkdir(exist_ok=True)
        listed = []
        for number, job in enumerate(jobs, 100):
            name, conclusion, messages = job[:3]
            failed = job[3] if len(job) > 3 else ([EXPLORER_STEP] if conclusion == "failure" else [])
            steps = [{"number": 1, "name": "Set up job", "conclusion": "success"}] + [
                {"number": 2 + index, "name": step, "conclusion": "failure"} for index, step in enumerate(failed)]
            listed.append({"id": number, "name": name, "conclusion": conclusion, "steps": steps})
            notes = [{"annotation_level": "failure", "message": "Process completed with exit code 1."}]
            notes += [{"annotation_level": "failure", "message": message} for message in messages]
            notes.append({"annotation_level": "warning", "message": "Node.js 20 is deprecated."})
            (site / ".annotations" / ("%d.json" % number)).write_text(json.dumps(notes))
        (site / ".jobs.json").write_text(json.dumps([{"id": 1, "name": "qae-criteria", "conclusion": "success"}] + listed))

    def limit(self, *extra):
        """The qa-limit step on run 42: its stdout and the outputs it wrote to $GITHUB_OUTPUT."""
        output = self.file("github-output", "")
        result = runner("qa-limit", "--repo", "acme/site", "--run-id", "42", "--head", HEAD, *extra,
                        env=dict(self.env, GITHUB_OUTPUT=output))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = dict(line.split("=", 1) for line in Path(output).read_text().splitlines())
        self.assertEqual(sorted(outputs), ["limited", "limits"])
        return result.stdout, outputs

    def site(self, name):
        return Path(self.fixtures) / "acme" / "site" / name

    def listed(self):
        return json.loads(self.site(".jobs.json").read_text())

    def jobs_reads(self):
        return len([c for c in self.calls() if c["endpoint"].startswith("repos/acme/site/actions/runs/42/jobs")])
