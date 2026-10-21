"""The names the catalog's checks show (docs/check-names.md): its own workflows and the templates consumers copy."""
import unittest

from test_ci_workflow import CI, NIGHTLY, ROOT, job


class CheckNames(unittest.TestCase):
    def test_each_workflow_is_its_stage_and_each_job_says_what_it_proves(self):
        # A branch rule requires a check by its job's name, so a rename here changes what can merge.
        for workflow, stage, names in ((CI, "Code", {"test": "Tests and gates", "qae-browser": "QA browser tests",
                                                     "regression-browser": "Regression browser tests",
                                                     "lanes": "Lane kit tests", "lanes-listener": "Lane listener tests"}),
                                       (NIGHTLY, "Maintenance", {"coverage-not-lost": "Coverage kept"})):
            self.assertTrue(workflow.read_text().startswith("name: %s\n" % stage), workflow.name)
            for job_id, name in names.items():
                self.assertTrue(job(job_id, workflow).startswith("    name: %s\n" % name), job_id)

    def test_each_template_consumers_copy_carries_the_standard_names(self):
        # A consumer's branch rule requires the plain-named job (Code review, Acceptance criteria, Gates).
        qa = {"criteria": '"Acceptance criteria: read"', "explore": '"Acceptance criteria: walk"', "verify": "Acceptance criteria"}
        review = {"review": '"Code review: model run"', "verify": "Code review"}
        for path, stage, names in (("harnesses/qae/explore.yml", "QA", qa), ("harnesses/qae/explore-codex.yml", "QA", qa),
                                   ("harnesses/review/review.yml", "Review", review),
                                   (".github/workflows/review.yml", "Review", review),
                                   ("harnesses/qae/codex-keepalive.yml", "Maintenance", {"keep-fresh": "Explorer login"}),
                                   ("consumer/vibe-verifier.yml", "Standards", {"vibe-verifier-ok": "Gates"})):
            workflow = ROOT / path
            with self.subTest(path=path):
                self.assertTrue(workflow.read_text().startswith("name: %s\n" % stage))
                for job_id, name in names.items():
                    self.assertTrue(job(job_id, workflow).startswith("    name: %s\n" % name), job_id)


if __name__ == "__main__":
    unittest.main()
