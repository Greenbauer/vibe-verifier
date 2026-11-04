"""The first-use walk's task and prompt, `bin/vibe-verifier first-use-prompt`: a second QAE session, given only the
task a pull request's `## First-use task` section states and the site, does that task as a person new to the site
and comes back to where it started (harnesses/first-use/README.md). gates/first_use_walk.py judges what it left.

    first-use-prompt --body FILE --site URL --out FILE

writes harnesses/first-use/prompt.md with the task and the site filled in, and prints `task: <n> words`; a body with
no such section, or an empty one, writes nothing and prints `task: none`. Exit 0 either way: the workflow walks only
when the file exists, and the gate decides whether a pull request needed one."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = os.path.join(ROOT, "harnesses", "first-use", "prompt.md")
HEADING = re.compile(r"^##[ \t]+First-use task[ \t]*$", re.IGNORECASE)
ANY_HEADING = re.compile(r"^#{1,6}[ \t]+")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def task(body):
    """The text under the body's `## First-use task` heading, up to the next heading, HTML comments left out, or ''."""
    lines, inside, kept = COMMENT.sub("", body).splitlines(), False, []
    for line in lines:
        if HEADING.match(line):
            inside = True
        elif inside and ANY_HEADING.match(line):
            break
        elif inside:
            kept.append(line)
    return "\n".join(kept).strip()


def prompt(text, site):
    with open(PROMPT, encoding="utf-8") as handle:
        template = handle.read()
    return template.replace("TASK", text, 1).replace("SITE_URL", site)


def command(args):
    with open(args.body, encoding="utf-8") as handle:
        text = task(handle.read())
    if not text:
        print("task: none")
        return 0
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(prompt(text, args.site))
    print("task: %d words" % len(text.split()))
    return 0


def add_arguments(parser):
    parser.add_argument("--body", metavar="FILE", required=True)
    parser.add_argument("--site", metavar="URL", required=True)
    parser.add_argument("--out", metavar="FILE", required=True)
