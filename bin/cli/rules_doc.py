"""`rules-doc`: the generated Markdown block that lists every rule the manifest's repo-rules lines enable, printed or
written into a file (`repo-rules --doc` fails a pull request whose block differs from it)."""
import os

from _contract import CannotRun
from _tools import ensure
import repo_rules
from cli.runner import fail, read_text


def rules_doc_command(args):
    """Print or write the generated rules block for the rules the manifest's repo-rules lines enable."""
    try:
        enabled = repo_rules.manifest_rules(args.manifest)
        if not enabled:
            return fail("%s has no repo-rules line, so it enables no rules" % args.manifest)
        block = repo_rules.rules_doc(ensure("ast-grep"), repo_rules.head_sources(args.repo, *enabled))
    except CannotRun as error:
        return fail("rules-doc: %s" % error)
    if not args.write:
        print(block)
        return 0
    text = read_text(args.write) if os.path.isfile(args.write) else ""
    found = repo_rules.BLOCK.search(text)
    if found:
        text = text[:found.start()] + block + text[found.end():]
    else:
        text += ("\n" if text and not text.endswith("\n") else "") + ("\n" if text.strip() else "") + block + "\n"
    with open(args.write, "w", encoding="utf-8") as handle:
        handle.write(text)
    print("%s the rules block in %s" % ("replaced" if found else "appended", args.write))
    return 0


def add_arguments(parser):
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", default=".vibe-verifier")
    parser.add_argument("--write", metavar="PATH", help="replace the block in this file, or append it when the file has none")
