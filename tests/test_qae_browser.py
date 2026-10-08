"""The qae-browser composite action's step, run under bash as a runner would, with the tool install,
playwright, sudo, timeout and apt-get stubbed: it installs chromium, then only the system packages
Playwright's own simulation says apt lacks, where the job can install them (root, or passwordless
sudo, as on GitHub's hosted runners), and the browser alone on a runner whose user cannot sudo, where
those packages are the runner's own (harnesses/qae/README.md). The fetch is bounded and tried once
more from the machine's next mirror. Its script is read from the shipped action.yml, so the test
judges what a consumer pins."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, clean_env

ACTION = ROOT / "actions" / "qae-browser" / "action.yml"
MIRRORS = "/etc/apt/apt-mirrors.txt"
FONTS = "Missing system dependencies (2):\n  fonts-unifont\n  xfonts-utils\n"
FETCH = "sh -c apt-get update && apt-get install -y --no-install-recommends --download-only \"$@\" sh"


def script():
    match = re.search(r"\n      run: \|\n((?:        [^\n]*\n|\n)+)", ACTION.read_text())
    return "".join(line[8:] for line in match.group(1).splitlines(True))


class QaeBrowserAction(unittest.TestCase):
    def run_action(self, sudo_exit=0, uid=1001, reports=("",), slow=(), mirrors=None):
        """`reports` is what each `install-deps --dry-run` prints, in order: "" with exit 0, anything
        else with exit 1. `slow` numbers the fetches that run out of time. `mirrors` is the machine's
        mirror list, None where it has none."""
        temp = Path(tempfile.mkdtemp(prefix="vv-qae-browser-"))
        tool = temp / "tool"
        (tool / "node_modules" / ".bin").mkdir(parents=True)
        (tool / "node_modules" / "@playwright" / "mcp").mkdir(parents=True)
        (tool / "node_modules" / "@playwright" / "mcp" / "cli.js").write_text("")
        calls = temp / "calls"
        for number, report in enumerate(reports, 1):
            (temp / ("report-%d" % number)).write_text(report)
        playwright = tool / "node_modules" / ".bin" / "playwright"
        playwright.write_text('''#!/bin/sh
echo "playwright $*" >> "%(calls)s"
[ "$1" = install-deps ] || exit 0
n=$(grep -c "^playwright install-deps" "%(calls)s")
[ -f "%(temp)s/report-$n" ] || n=%(last)d
cat "%(temp)s/report-$n"
[ ! -s "%(temp)s/report-$n" ]
''' % {"calls": calls, "temp": temp, "last": len(reports)})
        playwright.chmod(0o755)
        # The action resolves bin/vibe-verifier relative to itself; the stub answers the tool dir.
        action_path = temp / "actions" / "qae-browser"
        action_path.mkdir(parents=True)
        (temp / "bin").mkdir()
        (temp / "bin" / "vibe-verifier").write_text("import sys; print(%r)\n" % str(tool))
        mirror_file = temp / "apt-mirrors.txt"
        if mirrors is not None:
            mirror_file.write_text(mirrors)
        # On PATH first. sudo: -n true answers with the exit the runner would give, and any other
        # command runs, marked. id: the runner's user, whatever user runs the tests. timeout: runs
        # its command, except a fetch numbered in `slow`, which ends as a timed-out one does.
        # apt-get: records its arguments and the mirrors in force.
        path_dir = temp / "path"
        path_dir.mkdir()
        stubs = {
            "sudo": '[ "$2" = true ] && exit %d\nshift\nSUDO="sudo " exec "$@"\n' % sudo_exit,
            "id": "echo %d\n" % uid,
            "timeout": 'echo "${SUDO}timeout $*" >> "%s"\nn=$(grep -c "timeout " "%s")\nshift 3\n'
                       'case " %s " in *" $n "*) exit 124 ;; esac\nexec "$@"\n' % (calls, calls, " ".join(map(str, slow))),
            "apt-get": 'echo "${SUDO}apt-get $* [mirrors: $(cut -d/ -f3 "%s" 2>/dev/null | xargs)]" >> "%s"\n' % (mirror_file, calls),
        }
        for name, body in stubs.items():
            (path_dir / name).write_text("#!/bin/sh\n" + body)
            (path_dir / name).chmod(0o755)
        output = temp / "output"
        output.write_text("")
        env = clean_env({"GITHUB_ACTION_PATH": str(action_path), "GITHUB_OUTPUT": str(output),
                         "PATH": str(path_dir) + os.pathsep + os.environ["PATH"]})
        text = script()
        self.assertEqual(text.count(MIRRORS), 1)
        result = subprocess.run(["bash", "-c", text.replace(MIRRORS, str(mirror_file))], cwd=temp, capture_output=True, text=True, env=env)
        self.mirrors_after = mirror_file.read_text() if mirror_file.exists() else None
        return result, (calls.read_text() if calls.exists() else "").splitlines(), output.read_text()

    def test_a_runner_that_lacks_nothing_never_reaches_apt(self):
        result, calls, output = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["playwright install chromium", "playwright install-deps --dry-run chromium"])
        self.assertRegex(output, r"^mcp=.*/node_modules/@playwright/mcp/cli\.js\nconsole-pages=.*/actions/qae-browser/console-pages\.js\n$")

    def test_where_sudo_needs_no_password_only_the_missing_packages_are_installed(self):
        result, calls, output = self.run_action(reports=(FONTS,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            "playwright install chromium",
            "playwright install-deps --dry-run chromium",
            "sudo timeout -k 10 90 %s fonts-unifont xfonts-utils" % FETCH,
            "sudo apt-get update [mirrors: ]",
            "sudo apt-get install -y --no-install-recommends --download-only fonts-unifont xfonts-utils [mirrors: ]",
            "sudo apt-get install -y --no-install-recommends fonts-unifont xfonts-utils [mirrors: ]",
        ])

    def test_as_root_the_packages_are_installed_without_sudo(self):
        result, calls, output = self.run_action(sudo_exit=1, uid=0, reports=(FONTS,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[2], "timeout -k 10 90 %s fonts-unifont xfonts-utils" % FETCH)
        self.assertEqual(calls[-1], "apt-get install -y --no-install-recommends fonts-unifont xfonts-utils [mirrors: ]")
        self.assertFalse([call for call in calls if call.startswith("sudo")])

    def test_a_runner_whose_user_cannot_sudo_gets_the_browser_alone(self):
        # A password prompt would stop the job; the packages are the runner's own there.
        result, calls, output = self.run_action(sudo_exit=1, reports=(FONTS,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["playwright install chromium"])
        self.assertRegex(output, r"^mcp=.*/node_modules/@playwright/mcp/cli\.js\nconsole-pages=.*/actions/qae-browser/console-pages\.js\n$")

    def test_a_fetch_that_runs_out_of_time_is_tried_once_more_from_the_next_mirror(self):
        # A hosted runner's first mirror served 90 kB/s for minutes (a consumer's runs, 2026-10-07).
        # Its list gives each mirror a priority, so the order of the lines decides nothing.
        listed = "http://slow.example/ubuntu/\tpriority:1\nhttps://next.example/ubuntu/\tpriority:2\nhttps://last.example/ubuntu/\tpriority:3\n"
        result, calls, output = self.run_action(reports=(FONTS,), slow=(1,), mirrors=listed)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::warning::apt did not fetch within 90 s", result.stdout)
        self.assertEqual(calls[2:], [
            "sudo timeout -k 10 90 %s fonts-unifont xfonts-utils" % FETCH,
            "sudo timeout -k 10 300 %s fonts-unifont xfonts-utils" % FETCH,
            "sudo apt-get update [mirrors: next.example last.example]",
            "sudo apt-get install -y --no-install-recommends --download-only fonts-unifont xfonts-utils [mirrors: next.example last.example]",
            "sudo apt-get install -y --no-install-recommends fonts-unifont xfonts-utils [mirrors: slow.example next.example last.example]",
        ])
        self.assertEqual(self.mirrors_after, listed)

    def test_a_machine_with_one_mirror_or_none_is_tried_again_as_it_is(self):
        for listed in (None, "http://only.example/ubuntu/\n"):
            with self.subTest(listed=listed):
                result, calls, output = self.run_action(reports=(FONTS,), slow=(1,), mirrors=listed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len([call for call in calls if "timeout -k 10" in call]), 2)
                self.assertEqual(self.mirrors_after, listed)

    def test_a_fetch_that_fails_twice_fails_the_step_and_installs_nothing(self):
        listed = "http://slow.example/ubuntu/\nhttps://next.example/ubuntu/\n"
        result, calls, output = self.run_action(reports=(FONTS,), slow=(1, 2), mirrors=listed)
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::apt could not fetch chromium's system packages, on either try", result.stdout)
        self.assertFalse([call for call in calls if "apt-get" in call and "timeout" not in call])
        self.assertEqual(self.mirrors_after, listed)

    def test_a_machine_with_no_package_lists_fetches_them_then_asks_again(self):
        # A fresh container: apt cannot simulate anything until it has lists, and Playwright says so.
        result, calls, output = self.run_action(sudo_exit=1, uid=0, reports=("Error: 'apt-get install -s' exited with code 100:\nE: Unable to locate package libnss3\n", FONTS))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls[1:5], [
            "playwright install-deps --dry-run chromium",
            "timeout -k 10 90 %s" % FETCH,
            "apt-get update [mirrors: ]",
            "apt-get install -y --no-install-recommends --download-only [mirrors: ]",
        ])
        self.assertEqual(calls[5], "playwright install-deps --dry-run chromium")
        self.assertEqual(calls[-1], "apt-get install -y --no-install-recommends fonts-unifont xfonts-utils [mirrors: ]")

    def test_a_simulation_that_names_no_package_fails_the_step(self):
        # Never read as "nothing missing": only Playwright's exit 0 says that.
        result, calls, output = self.run_action(reports=("Error: something else\n",))
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::Playwright could not say which system packages chromium lacks", result.stdout)
        self.assertFalse([call for call in calls if "apt-get install -y --no-install-recommends " in call and "--download-only" not in call])


if __name__ == "__main__":
    unittest.main()
