#!/usr/bin/env python3
"""The QAE explorer prompt, harnesses/qae/prompt.md, rendered from its template harnesses/qae/prompt.in.md and the facts
the gates judge by (gates/_qae_rules.py, gates/_acceptance.py). Edit the template, then run this file: it writes the
prompt and the copy each explore template inlines, and tests/test_qae_prompt_rules.py fails while they disagree. The
prompt's rules that a gate holds as data are slots in the template, filled here, so the explorer is told what the gate
will judge (#218, #280).
"""
import os
import textwrap

from _acceptance import PROOF_TOKEN
from _qae_rules import DECLARED_BY, ENVIRONMENTAL, MAX_STATES, PROBE_STATUSES, REFUSABLE

HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harnesses", "qae")
TEMPLATE, PROMPT = os.path.join(HARNESS, "prompt.in.md"), os.path.join(HARNESS, "prompt.md")
NUMBERS = "one two three four five six seven eight nine".split()
GITHUB = (("PR_NUMBER", "${{ github.event.pull_request.number }}"), ("REPOSITORY", "${{ github.repository }}"))
# Each lane template's inlined copy: where the block starts, and what the workflow fills in where a lane's own
# step does not (the Codex lane's prompt step fills SITE_URL and SHARD_SHARE from its environment).
LANES = (("explore.yml", "          prompt: |\n", GITHUB + (("SITE_URL", "${{ steps.site.outputs.url }}"),
                                                        ("SHARD_SHARE", "${{ steps.references.outputs.share }}"))),
         ("explore-codex.yml", "<<'PROMPT'\n", GITHUB))


def either(words):
    return words[0] if len(words) == 1 else "%s or %s" % (", ".join(words[:-1]), words[-1])


def paragraph(text):
    """A paragraph of step 2 of the prompt, wrapped and indented as its hand-written ones are."""
    return textwrap.fill(text, 113, initial_indent="   ", subsequent_indent="   ", break_long_words=False,
                         break_on_hyphens=False)


def refusals():
    return paragraph(
        "Mark that one step of the criterion (one per role) and never another. A %s the site answers to it, while the "
        "page shows a handled error, is not a reason to FAIL: a gate excuses it when the page stayed at its URL, kept "
        "its elements and the controls the step used, and showed new text beside them. Any other request the site "
        "answers with 400 or worse fails the run unless %s declare it in their own text (expected-refusal: <status> "
        "<path>, where <status> is %s). A declaration counts for every criterion of the run, so a ticket criterion is "
        "not failed by a refusal the pull request's criteria declare; the status and path must still match exactly, "
        "and writing that in a step line declares nothing. Log each as a step of that criterion; any of them that "
        "fails makes the criterion a FAIL, and a 5xx, a crash or a blank page always does."
        % (either(PROBE_STATUSES), either(list(DECLARED_BY.values())), either(REFUSABLE)))


def environmental():
    return paragraph(
        "If qae-inputs/environmental exists, each of its lines is %s, a regular expression this repository's manifest "
        "declares environmental (the gate does not judge what it matches): %s is not the site's failure and never a "
        "reason to FAIL a criterion. Judge the criterion on what the page shows."
        % (either(["`%s: <pattern>`" % kind for kind, _, _ in ENVIRONMENTAL]),
           either(["%s a `%s:` pattern matches" % (what, kind) for kind, _, what in ENVIRONMENTAL])))


SLOTS = {
    "{{refusals}}": refusals,
    "{{environmental}}": environmental,
    "{{max_states}}": lambda: NUMBERS[MAX_STATES - 1] if MAX_STATES <= len(NUMBERS) else str(MAX_STATES),
    "{{ticket_proof}}": lambda: PROOF_TOKEN,
}


def render(template=TEMPLATE):
    with open(template, encoding="utf-8") as handle:
        text = handle.read()
    for slot, fill in SLOTS.items():
        # A slot the template dropped is a rule the gate judges by and the explorer is no longer told.
        if slot not in text:
            raise ValueError("%s has no %s: the prompt would not tell the explorer that rule" % (template, slot))
        text = text.replace(slot, fill())
    return text


def inlined(lane, marker, prompt, fills):
    """The text of lane template `lane` with `prompt`, filled in, as its block at 12 spaces after `marker`."""
    with open(os.path.join(HARNESS, lane), encoding="utf-8") as handle:
        text = handle.read()
    head = text.index(marker) + len(marker)
    rest = text[head:].splitlines(True)
    end = next(i for i, line in enumerate(rest) if line.strip() and not line.startswith(" " * 12))
    for name, value in fills:
        prompt = prompt.replace(name, value)
    block = "".join(" " * 12 + line if line.strip() else "\n" for line in prompt.splitlines(True))
    return text[:head] + block + "".join(rest[end:])


if __name__ == "__main__":
    prompt = render()
    written = {PROMPT: prompt}
    written.update((os.path.join(HARNESS, lane), inlined(lane, marker, prompt, fills)) for lane, marker, fills in LANES)
    for path, text in written.items():
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("wrote %s" % os.path.relpath(path))
