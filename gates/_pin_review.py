"""The pin bump's hold for a review copy whose anthropics/claude-code-action is older than the review template's
(gates/_pin_bump.py `holds`; actions/pin-bump/README.md, "What it does on each run", step 3)."""
import re

REVIEW_MODEL = re.compile(r"uses: *\S*vibe-verifier/actions/review-model@")
REVIEW_ACTION = re.compile(r"uses: *anthropics/claude-code-action@([0-9a-f]{40})")
REVIEW_TEMPLATE = "harnesses/review/review.yml"


def stale_review_action(git, source, target, path, text):
    """Why a review copy must be copied again before its pin moves: it reads the model from actions/review-model at
    the pin, and the model at `target` can need a newer anthropics/claude-code-action than the copy pins (claude-opus-5-5
    needs Claude Code 2.1.280; the copy's v1.0.171 ran 2.1.207 and every review failed). Held while the copy pins that
    action at a commit the template at `target` does not (harnesses/review/README.md, "The model follows the pin")."""
    if not REVIEW_MODEL.search(text):
        return []
    template = git(source, "show", "%s:%s" % (target, REVIEW_TEMPLATE))
    want = set(REVIEW_ACTION.findall(template.stdout)) if template.returncode == 0 else set()
    have = set(REVIEW_ACTION.findall(text))
    if not want or have <= want:
        return []
    return ["%s pins anthropics/claude-code-action at %s, but %s's %s pins %s: copy it again first, or the review "
            "runs a model the action's Claude Code does not know" % (path, " ".join(sorted(sha[:10] for sha in have - want)),
                                                                    target[:10], REVIEW_TEMPLATE, " ".join(sorted(sha[:10] for sha in want)))]
