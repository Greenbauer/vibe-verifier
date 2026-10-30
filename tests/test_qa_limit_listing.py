"""`vibe-verifier qa-limit` while GitHub has not listed a finished explore job's steps or annotations yet, and on a
rerun of only the verify job, whose copies of the explore jobs carry no annotations (Greenbauer/zack.land run
41110815005, both attempts): it reads again, for a bounded time, and reads a copy as the job that ran."""
import json

from qa_limit_fixture import QaLimitFixture


class Listing(QaLimitFixture):
    def test_steps_github_has_not_listed_yet_are_read_again(self):
        # Greenbauer/zack.land run 41110815005: read seconds after both walks failed on the limit, GitHub listed no
        # steps on one and steps with no conclusion on the other; about 15 minutes later it listed them all.
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]),
                       ("Acceptance criteria: walk (2)", "failure", [self.CLAUDE_LIMIT]))
        filled = self.listed()
        empty = json.loads(json.dumps(filled))
        empty[1]["steps"] = []
        for step in empty[2]["steps"]:
            step["conclusion"] = None
        self.site(".jobs.json").write_text(json.dumps({"sequence": [empty, empty, filled]}))
        stdout, outputs = self.limit("--wait", "60", "--interval", "0")
        self.assertEqual(outputs["limited"], "true", stdout)
        self.assertEqual(self.jobs_reads(), 3)

    def test_annotations_github_has_not_listed_yet_are_read_again(self):
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]))
        notes = self.site(".annotations") / "100.json"
        notes.write_text(json.dumps({"sequence": [[], json.loads(notes.read_text())]}))
        stdout, outputs = self.limit("--wait", "60", "--interval", "0")
        self.assertEqual(outputs["limited"], "true", stdout)
        self.assertEqual(self.jobs_reads(), 2)

    def test_steps_still_unlisted_at_the_deadline_stay_red(self):
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]))
        jobs = self.listed()
        jobs[1]["steps"] = []
        self.site(".jobs.json").write_text(json.dumps(jobs))
        stdout, outputs = self.limit("--wait", "0", "--interval", "0")
        self.assertEqual(outputs, {"limited": "false", "limits": "[]"})
        self.assertIn("::warning::GitHub had not listed every explore job's steps and failure annotations after 0s, "
                      "so the cause of the failure is unknown: rerun this job once they are listed", stdout)

    def test_a_listed_failure_of_another_kind_ends_the_wait(self):
        # One walk failed another way, and GitHub has listed it: the run is not excused whatever the other shows,
        # so nothing waits for the other's steps.
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]),
                       ("Acceptance criteria: walk (2)", "failure", ["site did not answer"]))
        jobs = self.listed()
        jobs[1]["steps"] = []
        self.site(".jobs.json").write_text(json.dumps(jobs))
        stdout, outputs = self.limit("--wait", "600", "--interval", "600")
        self.assertEqual(outputs, {"limited": "false", "limits": "[]"})
        self.assertEqual(self.jobs_reads(), 1)

    def test_a_rerun_of_the_verify_job_reads_the_walks_that_ran(self):
        # Rerunning only the verify job (run 41110815005, attempt 2) copies the walks into the new attempt under new
        # ids, with their steps and times but no annotations; the attempt that ran them has the limit.
        self.explorers(("Acceptance criteria: walk", "failure", [self.CLAUDE_LIMIT]),
                       ("Acceptance criteria: walk (2)", "failure", [self.CLAUDE_LIMIT]))
        ran = self.listed()
        for job in ran:
            job.update(run_attempt=1, started_at="2026-10-30T18:54:24Z", completed_at="2026-10-30T18:5%dZ" % job["id"])
        copies = [dict(job, id=job["id"] + 100, run_attempt=2) for job in ran]
        self.site(".jobs-attempt-1.json").write_text(json.dumps(ran))
        self.site(".jobs.json").write_text(json.dumps(copies))
        stdout, outputs = self.limit("--wait", "0", "--interval", "0")
        self.assertEqual(outputs["limited"], "true", stdout)
        endpoints = [c["endpoint"] for c in self.calls()]
        self.assertIn("repos/acme/site/actions/runs/42/attempts/1/jobs?per_page=100", endpoints)
        self.assertIn("repos/acme/site/check-runs/101/annotations?per_page=100", endpoints)
        # A rerun that starts before GitHub lists the first attempt's steps waits for them there.
        unlisted = json.loads(json.dumps(ran))
        for job in unlisted[1:]:
            job["steps"] = []
        self.site(".jobs-attempt-1.json").write_text(json.dumps({"sequence": [unlisted, ran]}))
        stdout, outputs = self.limit("--wait", "60", "--interval", "0")
        self.assertEqual(outputs["limited"], "true", stdout)
        self.site(".jobs-attempt-1.json").write_text(json.dumps(ran))
        # A walk this attempt ran again (a start of its own) is read as itself: its own annotations say no limit.
        copies[2].update(started_at="2026-10-30T19:12:30Z")
        (self.site(".annotations") / "201.json").write_text(json.dumps([
            {"annotation_level": "failure", "message": "Process completed with exit code 1."},
            {"annotation_level": "failure", "message": "site did not answer"}]))
        self.site(".jobs.json").write_text(json.dumps(copies))
        self.assertEqual(self.limit("--wait", "0", "--interval", "0")[1], {"limited": "false", "limits": "[]"})
