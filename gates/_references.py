"""How the acceptance-verdict gate judges a criterion's design references (`[ref: <key>]`, harnesses/qae/README.md):
the image the workflow supplied, the comparison step at its width, and the second look a FAIL on that comparison gets.

A FAIL whose one anchor is a comparison step counts only when the reference check (gates/_reference_check.py, which
the explore job runs) saw the difference too. When its record qae/ACn-reference-check.json matches the FAIL's claim,
step and reference and both images, the reference is one the criterion names (so the workflow's digest vouches for
it), and the model did not see the difference, the criterion is judged as a PASS anchored at that step. A record that
confirms it, did not run, is about another claim or other images, or is missing leaves the FAIL standing.
"""
import hashlib
import os
import re

from _acceptance import annotations, named_references, png_size
from _reference_check import comparison, overturned

STEP_LINE = re.compile(r"^[ \t]*-[ \t]+step[ \t]+(?P<k>[0-9]+):(?P<text>.*)$", re.IGNORECASE | re.MULTILINE)


def comparison_findings(item, key, text, artifacts, width):
    """Why the step log `text` of a PASSed `item` shows no comparison with reference `key` at the reference's
    `width` (None when the reference was not supplied, which is a finding of its own)."""
    steps = [m.group("k") for m in STEP_LINE.finditer(text)
             if key.casefold() in (named.casefold() for named in named_references(m.group("text")))]
    if not steps:
        return ["%s is a PASS, but no step line in qae/%s.md names `reference %s`" % (item, item, key)]
    if width is None:
        return []
    findings = []
    for k in steps:
        shot = os.path.join(artifacts, "qae", "%s-step-%s.png" % (item, k))
        size = png_size(shot) if os.path.isfile(shot) else None
        if size is None:
            findings.append("%s is a PASS, but step %s, its comparison with reference %s, has no PNG screenshot "
                            "qae/%s-step-%s.png to show it was compared at the reference's %d pixels"
                            % (item, k, key, item, k, width))
        elif size[0] != width:
            findings.append("%s is a PASS, but step %s compared with reference %s at %d pixels wide, and the reference "
                            "is %d: resize the browser to the reference's width before that step's screenshot"
                            % (item, k, key, size[0], width))
    return findings


def reference_finding(item, key, declared, artifacts):
    """Why the reference `key` that `item` names is not the image the workflow supplied, or None."""
    if declared is None:
        return ("%s names reference `%s`, but this QAE manifest declares no supplied references: add "
                "--references qae-inputs/references.json to its acceptance-verdict line" % (item, key))
    if key not in declared:
        return ("%s names reference `%s`, but the workflow supplied no qae-inputs/references/%s.png. Supply it from "
                "the site step (needs-operator-reference): a reference is never invented, so %s cannot pass "
                "without it" % (item, key, key, item))
    image = os.path.join(artifacts, "references", key + ".png")
    if not os.path.isfile(image):
        return "%s: the evidence holds no references/%s.png, the copy the workflow made" % (item, key)
    with open(image, "rb") as handle:
        if hashlib.sha256(handle.read()).hexdigest() != declared[key]:
            return "%s: references/%s.png in the evidence is not the image the workflow supplied" % (item, key)
    if png_size(image) is None:
        return "%s: references/%s.png is not a PNG, so the width it is compared at cannot be read" % (item, key)
    return None


def second_look(item, wording, line, artifacts):
    """(whether the FAIL `line` counts, why): a FAIL on a comparison with a reference the criterion names does not
    count when the reference check's record refutes it (gates/_reference_check.py); every other FAIL counts."""
    second = overturned(item, line, artifacts)
    if not second:
        return True, ""
    named = (key.casefold() for key in annotations(wording).references)
    # Only a reference the criterion names is one the workflow's digest vouches for (reference_finding).
    if second[0] and comparison(item, line)[1].casefold() not in named:
        return True, "the reference it was compared with is not one this criterion names"
    return not second[0], second[1]
