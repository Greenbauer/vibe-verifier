"""Run the shipped Codex action shell to verify optional caller-owned model settings."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import clean_env
from test_qae_codex import ACTION, run_script


class Settings(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="vv-codex-settings-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.temp = self.work / "runner-temp"
        self.temp.mkdir()
        self.codex = self.work / "codex"
        self.codex.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                              'from pathlib import Path\n'
                              'Path("argv.json").write_text(json.dumps(sys.argv[1:]))\n')
        self.codex.chmod(0o755)
        (self.work / "prompt.md").write_text("prompt\n")

    def run_step(self, model="", effort=""):
        script = run_script(ACTION.read_text(), "- name: Explore the acceptance criteria in a real browser",
                            "- name: Redact the secrets from the artifacts")
        return subprocess.run(["bash", "-c", script], cwd=self.work, capture_output=True, text=True,
                              env=clean_env({"CODEX": str(self.codex), "PROMPT_FILE": "prompt.md", "MCP": "",
                                             "ARTIFACTS": "qae-artifacts", "STORAGE_STATE": "", "SECRETS_FILE": "",
                                             "MODEL": model, "REASONING_EFFORT": effort,
                                             "RUNNER_TEMP": str(self.temp), "GITHUB_ACTION_PATH": str(ACTION.parent),
                                             "GITHUB_REPOSITORY": "octo/demo", "GITHUB_RUN_ID": "1",
                                             "GITHUB_RUN_ATTEMPT": "1", "VV_HEAD_SHA": "0" * 40,
                                             "USAGE_ACCOUNT_ALIAS": ""}))

    def test_empty_inputs_preserve_runner_selection(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = json.loads((self.work / "argv.json").read_text())
        self.assertNotIn("--model", argv)
        self.assertFalse(any(arg.startswith("model_reasoning_effort=") for arg in argv))
        self.assertIn("configured model=runner-default reasoning_effort=runner-default", result.stdout)
        self.assertIn("--ephemeral", argv)
        self.assertIn('approval_policy="never"', argv)

    def test_explicit_settings_are_independent_arguments(self):
        for model, effort in [("gpt-6.1-sol", ""), ("", "high")] + [("gpt-6.1-sol", effort) for effort in ("low", "medium", "high", "xhigh", "max", "ultra")]:
            with self.subTest(model=model, effort=effort):
                result = self.run_step(model, effort)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                argv = json.loads((self.work / "argv.json").read_text())
                if model:
                    self.assertEqual(argv[argv.index("--model") + 1], model)
                else:
                    self.assertNotIn("--model", argv)
                if effort:
                    setting = f'model_reasoning_effort="{effort}"'
                    self.assertIn(setting, argv)
                    self.assertEqual(argv[argv.index(setting) - 1], "-c")
                else:
                    self.assertFalse(any(arg.startswith("model_reasoning_effort=") for arg in argv))
                self.assertIn(f"configured model={model or 'runner-default'} reasoning_effort={effort or 'runner-default'}",
                              result.stdout)

    def test_invalid_settings_fail_before_codex_without_echoing_them(self):
        for model, effort in [("--other", ""), ("$(touch injected)", ""), ('model"\nsecret', ""),
                              ("", "unknown"), ("", 'high" injected'), ("", "$(touch injected)")]:
            with self.subTest(model=model, effort=effort):
                result = self.run_step(model, effort)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((self.work / "argv.json").exists())
                self.assertFalse((self.work / "injected").exists())
                self.assertFalse((self.work / "qae-artifacts").exists())
                self.assertNotIn(model or effort, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
