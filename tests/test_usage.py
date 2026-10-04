"""The numeric usage contract, collectors, and source-workflow artifact wiring."""
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

COLLECTOR = ROOT / "actions" / "usage" / "collect.py"
ACTION = ROOT / "actions" / "usage" / "action.yml"
REVIEW = ROOT / "harnesses" / "review" / "review.yml"
QAE = ROOT / "harnesses" / "qae" / "explore.yml"
CODEX_ACTION = ROOT / "actions" / "qae-codex" / "action.yml"
OWN_REVIEW = ROOT / ".github" / "workflows" / "review.yml"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
SHA = "0123456789abcdef0123456789abcdef01234567"


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="vv-usage-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.work]))
        self.output = Path(self.work, "out", "usage.json")
        self.env = clean_env({
            "GITHUB_REPOSITORY": "octo/demo",
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_RUN_ATTEMPT": "2",
            "VV_HEAD_SHA": SHA,
        })

    def common(self, role="swe-reviewer", job="review"):
        return [sys.executable, str(COLLECTOR), "--output", str(self.output),
                "--role", role, "--job-key", job]

    def record(self):
        return json.loads(self.output.read_text())

    def assert_metadata(self, record, provider, role, job):
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual((record["provider"], record["role"], record["job_key"]),
                         (provider, role, job))
        self.assertEqual((record["owner"], record["repository"]), ("octo", "octo/demo"))
        self.assertEqual((record["head_sha"], record["run_id"], record["run_attempt"]),
                         (SHA, 123456789, 2))
        self.assertRegex(record["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(record["account_alias"])


class ClaudeUsage(CollectorCase):
    def run_fixture(self, messages=None, raw=None, ran="true", account=None, path=None):
        source = Path(path or Path(self.work, "execution.json"))
        if raw is not None or messages is not None:
            source.write_text(raw if raw is not None else json.dumps(messages))
        args = self.common()
        if account is not None:
            args.extend(["--account-alias", account])
        args.extend(["claude", "--source-file", str(source), "--source-ran", ran])
        return subprocess.run(args, capture_output=True, text=True, env=self.env)

    def result(self, usage=None, subtype="success"):
        return {"type": "result", "subtype": subtype, "is_error": subtype != "success",
                "result": "private final answer", "usage": usage}

    def test_final_result_is_the_only_source_and_cache_fields_are_not_duplicated(self):
        messages = [
            {"type": "assistant", "message": {"usage": {"input_tokens": 9000, "output_tokens": 9000}}},
            self.result({"input_tokens": 11, "cache_read_input_tokens": 22,
                         "cache_creation_input_tokens": 33, "output_tokens": 44})
            | {"modelUsage": {"claude": {"inputTokens": 9999, "outputTokens": 9999}}},
        ]
        run = self.run_fixture(messages)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        record = self.record()
        self.assert_metadata(record, "anthropic", "swe-reviewer", "review")
        self.assertEqual(record["status"], "complete")
        self.assertIsNone(record["status_reason"])
        self.assertEqual(record["usage"], {
            "input_tokens": 11,
            "cached_input_tokens": 22,
            "cache_creation_input_tokens": 33,
            "output_tokens": 44,
            "reasoning_output_tokens": None,
        })

    def test_a_failed_final_result_keeps_aggregate_usage_as_partial(self):
        run = self.run_fixture([self.result({"input_tokens": 1, "cache_read_input_tokens": 2,
                                             "cache_creation_input_tokens": 3, "output_tokens": 4},
                                            "error_during_execution")])
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((self.record()["status"], self.record()["status_reason"]),
                         ("partial", "provider_failed"))
        self.assertEqual(self.record()["usage"]["cached_input_tokens"], 2)

    def test_legitimate_zero_is_complete_but_missing_usage_is_unavailable(self):
        zero = {name: 0 for name in ("input_tokens", "cache_read_input_tokens",
                                     "cache_creation_input_tokens", "output_tokens")}
        self.assertEqual(self.run_fixture([self.result(zero)]).returncode, 0)
        self.assertEqual(self.record()["usage"]["output_tokens"], 0)
        self.assertEqual(self.record()["status"], "complete")
        self.output.unlink()
        self.assertEqual(self.run_fixture([self.result(None)]).returncode, 0)
        self.assertEqual((self.record()["status"], self.record()["status_reason"], self.record()["usage"]),
                         ("unavailable", "no_final_usage", None))

    def test_not_run_and_missing_artifact_are_explicit_not_zero(self):
        for ran, reason in (("false", "not_run"), ("true", "no_artifact")):
            with self.subTest(ran=ran):
                run = self.run_fixture(ran=ran, path=Path(self.work, "absent.json"))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual((self.record()["status"], self.record()["status_reason"], self.record()["usage"]),
                                 ("unavailable", reason, None))
                self.output.unlink()

    def test_invalid_numeric_fields_never_become_usage(self):
        invalid = [True, -1, 1.5, float("nan"), float("inf"), "5", 2**63]
        for value in invalid:
            with self.subTest(value=value):
                usage = {"input_tokens": value, "cache_read_input_tokens": 0,
                         "cache_creation_input_tokens": 0, "output_tokens": 0}
                run = self.run_fixture([self.result(usage)])
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual((self.record()["status_reason"], self.record()["usage"]),
                                 ("invalid_usage", None))
                self.output.unlink()

    def test_content_and_secret_shapes_never_reach_output_or_logs(self):
        secret = "sk-ant-api03-this-is-fixture-data-not-a-real-secret"
        messages = [{"type": "user", "message": {"content": secret}},
                    self.result({"input_tokens": 1, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0, "output_tokens": 1})]
        run = self.run_fixture(messages, account="review-seat")
        artifact = self.output.read_text()
        self.assertNotIn(secret, run.stdout + run.stderr + artifact)
        self.assertNotIn("private final answer", artifact)
        self.assertEqual(self.record()["account_alias"], "review-seat")

    def test_source_must_be_a_bounded_regular_file(self):
        large = Path(self.work, "large.json")
        with large.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        link = Path(self.work, "link.json")
        link.symlink_to(large)
        for path in (large, link):
            with self.subTest(path=path.name):
                run = self.run_fixture(path=path)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual((self.record()["status_reason"], self.record()["usage"]),
                                 ("invalid_usage", None))
                self.output.unlink()


class CodexUsage(CollectorCase):
    def setUp(self):
        super().setUp()
        self.events = Path(self.work, "events")
        self.fake = Path(self.work, "codex")
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os,signal,sys,time\n"
            "pid=os.environ.get('FAKE_PID_FILE')\n"
            "if pid: open(pid,'w').write(str(os.getpid()))\n"
            "if os.environ.get('FAKE_WAIT'):\n"
            " signal.signal(signal.SIGTERM, lambda s,f: sys.exit(0))\n"
            " while True: time.sleep(.05)\n"
            "with open(os.environ['FAKE_EVENTS'],'rb') as source:\n"
            " sys.stdout.buffer.write(source.read()); sys.stdout.flush()\n"
            "sys.exit(int(os.environ.get('FAKE_EXIT','0')))\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def run_fake(self, lines, exit_code=0):
        self.events.write_bytes(b"\n".join(line if isinstance(line, bytes) else json.dumps(line).encode()
                                             for line in lines) + b"\n")
        env = dict(self.env, FAKE_EVENTS=str(self.events), FAKE_EXIT=str(exit_code))
        args = self.common("qae-explorer", "explore") + ["codex", "--", str(self.fake), "exec", "--json"]
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def completed(self, input_tokens, cached, output, reasoning):
        return {"type": "turn.completed", "usage": {"input_tokens": input_tokens,
                "cached_input_tokens": cached, "output_tokens": output,
                "reasoning_output_tokens": reasoning}}

    def test_multiple_completed_turns_are_summed_without_adding_cache_to_input(self):
        run = self.run_fake([{"type": "thread.started", "private": "ignore"},
                             self.completed(10, 7, 3, 2), self.completed(20, 11, 5, 4)])
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        record = self.record()
        self.assert_metadata(record, "openai", "qae-explorer", "explore")
        self.assertEqual(record["usage"], {"input_tokens": 30, "cached_input_tokens": 18,
                                            "cache_creation_input_tokens": None, "output_tokens": 8,
                                            "reasoning_output_tokens": 6})
        self.assertEqual(record["status"], "complete")

    def test_failed_command_preserves_exit_and_partial_usage(self):
        run = self.run_fake([self.completed(5, 4, 3, 2)], exit_code=17)
        self.assertEqual(run.returncode, 17, run.stdout + run.stderr)
        self.assertEqual((self.record()["status"], self.record()["status_reason"]),
                         ("partial", "provider_failed"))
        self.assertEqual(self.record()["usage"]["input_tokens"], 5)

    def test_an_invalid_later_event_cannot_partly_change_the_last_valid_total(self):
        invalid = self.completed(100, 200, 2**63, 300)
        run = self.run_fake([self.completed(5, 4, 3, 2), invalid])
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual((self.record()["status"], self.record()["status_reason"]),
                         ("partial", "invalid_usage"))
        self.assertEqual(self.record()["usage"], {
            "input_tokens": 5, "cached_input_tokens": 4,
            "cache_creation_input_tokens": None, "output_tokens": 3,
            "reasoning_output_tokens": 2,
        })

    def test_zero_and_missing_are_distinct(self):
        self.assertEqual(self.run_fake([self.completed(0, 0, 0, 0)]).returncode, 0)
        self.assertEqual((self.record()["status"], self.record()["usage"]["input_tokens"]), ("complete", 0))
        self.output.unlink()
        self.assertEqual(self.run_fake([{"type": "turn.started"}]).returncode, 0)
        self.assertEqual((self.record()["status_reason"], self.record()["usage"]), ("no_final_usage", None))

    def test_malformed_invalid_and_oversized_events_do_not_leak(self):
        secret = b"ghp_fixtureSecretValueThatMustNeverAppear"  # gitleaks:allow synthetic credential canary for redaction test
        invalid = self.completed(True, 0, 0, 0)
        run = self.run_fake([b'{"type":"message","content":"' + secret + b'"}',
                             b"{" + b"x" * (1024 * 1024 + 1), invalid])
        combined = run.stdout.encode() + run.stderr.encode() + self.output.read_bytes()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn(secret, combined)
        self.assertEqual((self.record()["status_reason"], self.record()["usage"]),
                         ("invalid_usage", None))

    def test_sigterm_reaches_the_owned_child_and_returns_shell_signal_status(self):
        pid_file = Path(self.work, "child.pid")
        env = dict(self.env, FAKE_WAIT="1", FAKE_PID_FILE=str(pid_file))
        args = self.common("qae-explorer", "explore") + ["codex", "--", str(self.fake)]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(100):
            if pid_file.exists():
                break
            time.sleep(.02)
        self.assertTrue(pid_file.exists(), "fake provider did not start")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stdout + stderr)
        self.assertEqual((self.record()["status"], self.record()["status_reason"]),
                         ("unavailable", "provider_failed"))


class WorkflowWiring(unittest.TestCase):
    def test_claude_jobs_collect_after_the_model_and_upload_only_usage_json(self):
        for path, model_name, role in ((REVIEW, "Review", "swe-reviewer"),
                                       (QAE, "Explore the acceptance criteria", "qae-explorer")):
            text = path.read_text()
            collect = text.index("- name: Collect numeric usage")
            upload = text.index("- name: Keep numeric usage")
            self.assertLess(text.index("- name: " + model_name), collect)
            self.assertLess(collect, upload)
            block = text[collect:text.index("\n\n", upload)]
            self.assertIn("if: always()", block)
            self.assertIn("retention-days: 7", block)
            self.assertIn("path: ${{ runner.temp }}/vv-usage/usage.json", block)
            self.assertIn("name: vv-usage-%s-${{ github.run_attempt }}" % role, block)
            self.assertIn(UPLOAD, block)
            # Only the review's model step tolerates a failure, so its gate step can classify it.
            self.assertNotIn("continue-on-error", block)
            self.assertEqual(text.count("continue-on-error: true"), 1 if path == REVIEW else 0)

    def test_consumer_templates_pin_usage_and_source_review_uses_the_local_action(self):
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/usage@" + "0" * 40, REVIEW.read_text())
        self.assertIn("uses: Greenbauer/vibe-verifier/actions/usage@" + "0" * 40, QAE.read_text())
        self.assertIn("uses: ./actions/usage", OWN_REVIEW.read_text())
        self.assertIn('source="$RUNNER_TEMP/claude-execution-output.json"', ACTION.read_text())

    def test_review_receipt_precedes_usage_without_ignoring_capture_failures(self):
        for path in (OWN_REVIEW, REVIEW):
            text = path.read_text()
            receipt = text.index("- name: Post the receipt")
            collect = text.index("- name: Collect numeric usage")
            upload = text.index("- name: Keep numeric usage")
            self.assertLess(text.index("- name: Review\n"), receipt)
            self.assertLess(receipt, collect)
            self.assertLess(collect, upload)
            self.assertEqual(text[collect:text.index("\n\n  verify:")].count("if: always()"), 2)
            self.assertIn("if-no-files-found: error", text[upload:])
            self.assertNotIn("continue-on-error", text[receipt:])
            self.assertNotIn("if: always()", text[receipt:collect])

    def test_source_review_usage_block_matches_the_consumer_template(self):
        def block(path):
            text = path.read_text()
            section = text[text.index("- name: Collect numeric usage"):text.index("\n\n  verify:")]
            return section.replace(
                "uses: Greenbauer/vibe-verifier/actions/usage@" + "0" * 40 +
                " # CONSUMER: pin the commit you subscribe to",
                "uses: USAGE_ACTION").replace(
                "uses: ./actions/usage # zizmor: ignore[self-repository]",
                "uses: USAGE_ACTION")
        self.assertEqual(block(OWN_REVIEW), block(REVIEW))

    def test_codex_action_uses_json_and_owns_its_dedicated_upload(self):
        text = CODEX_ACTION.read_text()
        self.assertIn("collect.py", text)
        self.assertIn("exec --json", text)
        self.assertIn("name: vv-usage-qae-explorer-${{ github.run_attempt }}", text)
        self.assertIn("path: ${{ runner.temp }}/vv-usage/usage.json", text)
        self.assertIn("retention-days: 7", text)
        self.assertIn(UPLOAD, text)
        self.assertNotIn("continue-on-error", text)

    def test_helper_is_standard_library_and_new_files_stay_under_500_lines(self):
        text = COLLECTOR.read_text()
        self.assertNotIn("from requests", text)
        self.assertNotIn("import requests", text)
        for path in (COLLECTOR, ACTION, ROOT / "docs" / "USAGE-CONTRACT.md", Path(__file__)):
            self.assertLessEqual(len(path.read_text().splitlines()), 500, path)


if __name__ == "__main__":
    unittest.main()
