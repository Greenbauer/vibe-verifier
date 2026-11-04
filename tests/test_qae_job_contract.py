"""Each qae-inputs/ file a QAE template job hands an action is written earlier in that same job. Jobs share no workspace:
a file the verify job writes does not exist in the explore job. An action input that defaults to such a file, left
unset by the step, reads nothing on every consumer's run (copy-audit's site-file did: #293). This reads every action
a template step uses, its defaults with the step's `with:` values over them, and the manifest the gates step runs."""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

TEMPLATES = ("explore.yml", "explore-codex.yml")
PATH = re.compile(r"^qae-inputs/[\w./-]+$")
# A shell redirection or a Python open for writing, of one qae-inputs/ path.
WRITE = re.compile(r"""(?:>>?\s*"?|open\(\s*")(qae-inputs/[\w./-]+)""")
USES = re.compile(r"^        uses: [\w.-]+/vibe-verifier/actions/([\w-]+)@", re.MULTILINE)
GATE_PATH = re.compile(r"(?<=\s)qae-inputs/[\w./-]+")
# An input the action writes, not reads.
WRITES = {"out"}
# An input whose file a step may leave unwritten, which the action reads as none (its description says so).
OPTIONAL = {("qae-inputs", "references")}


def jobs(text):
    """{job: [step text, ...]} of a workflow, in order."""
    body = text[text.index("\njobs:\n"):]
    found = {}
    for job in re.split(r"\n(?=  [\w-]+:\n)", body)[1:]:
        found[job.split(":", 1)[0].strip()] = re.split(r"\n(?=      - )", job)[1:]
    return found


def value(raw):
    raw = raw.split(" #", 1)[0].strip()
    return raw[1:-1] if raw[:1] in "'\"" and raw[-1:] == raw[:1] else raw


def defaults(root, action):
    """{input: default} of an action's inputs."""
    text = (root / "actions" / action / "action.yml").read_text()
    if "\ninputs:\n" not in text:
        return {}
    inputs = text[text.index("\ninputs:\n"):]
    inputs = inputs[:re.search(r"\n\S", inputs[1:]).start() + 1]
    found = {}
    for name, block in re.findall(r"^  ([\w-]+):\n((?:    .*\n)*)", inputs + "\n", re.MULTILINE):
        default = re.search(r"^    default: (.*)$", block, re.MULTILINE)
        found[name] = value(default.group(1)) if default else ""
    return found


def given(step):
    block = re.search(r"^        with:\n((?:          .*\n?)*)", step, re.MULTILINE)
    return {name: value(raw) for name, raw in re.findall(r"^          ([\w-]+): (.*)$", block.group(1) if block else "",
                                                         re.MULTILINE)}


def unwritten_reads(root, template):
    """(job, action, input or manifest line, path) for each qae-inputs/ path a step reads that no earlier step of its job
    wrote."""
    manifest = (root / "harnesses" / "qae" / "manifest").read_text()
    found = []
    for job, steps in jobs((root / "harnesses" / "qae" / template).read_text()).items():
        written = set()
        for step in steps:
            uses = USES.search(step)
            if uses:
                action = uses.group(1)
                inputs = {**defaults(root, action), **given(step)}
                reads = [(name, path) for name, path in inputs.items()
                         if PATH.match(path) and name not in WRITES and (action, name) not in OPTIONAL]
                if action == "gates":
                    reads += [("manifest", path) for line in manifest.splitlines()
                              if re.match(r"^(?:# )?[a-z][a-z-]+ --", line) for path in GATE_PATH.findall(line)]
                found += [(job, action, name, path) for name, path in reads if path not in written]
                written |= {path for name, path in inputs.items() if name in WRITES and PATH.match(path)}
            script = "\n".join(line for line in step.splitlines() if not line.lstrip().startswith("#"))
            written |= set(WRITE.findall(script))
    return found


class JobContract(unittest.TestCase):
    def test_every_file_a_template_step_hands_an_action_is_written_earlier_in_its_job(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                self.assertEqual(unwritten_reads(ROOT, template), [])

    def test_the_check_reads_actions_the_templates_use(self):
        # Each lane hands files to these actions; a parser that found none would pass anything.
        for template in TEMPLATES:
            used = set(USES.findall((ROOT / "harnesses" / "qae" / template).read_text()))
            self.assertLessEqual({"ticket", "criteria", "features", "gates", "qa-review"}, used, template)

    def test_a_default_another_job_writes_is_found(self):
        tree = Path(tempfile.mkdtemp(prefix="vv-contract-"))
        self.addCleanup(shutil.rmtree, tree)
        (tree / "actions" / "grade").mkdir(parents=True)
        (tree / "actions" / "grade" / "action.yml").write_text(
            "name: g\ninputs:\n  out:\n    default: qae-inputs/grades\n"
            "  site-file:\n    description: d\n    default: qae-inputs/site-url\nruns:\n  using: composite\n")
        (tree / "harnesses" / "qae").mkdir(parents=True)
        (tree / "harnesses" / "qae" / "manifest").write_text("")
        (tree / "harnesses" / "qae" / "t.yml").write_text(
            "on: x\njobs:\n  explore:\n    steps:\n      - name: Grade\n"
            "        uses: Greenbauer/vibe-verifier/actions/grade@0\n"
            "  verify:\n    steps:\n      - name: Write\n        run: |\n          printf x > qae-inputs/site-url\n"
            "      - name: Grade\n        uses: Greenbauer/vibe-verifier/actions/grade@0\n")
        self.assertEqual(unwritten_reads(tree, "t.yml"), [("explore", "grade", "site-file", "qae-inputs/site-url")])


if __name__ == "__main__":
    unittest.main()
