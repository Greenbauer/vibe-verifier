"""Whether a pull request adds a piece of copy a page showed: the copy-audit and ui-audit gates block only on what
the pull request adds, and list what the base already had for a cleanup pull request to work from.

The copy is the pull request's when four consecutive whole words of it are on a line `git diff <base>...HEAD` adds,
or, for a shorter label, the whole label as a string, an element's text or a line of its own, after decoding HTML
entities and straightening quotes (gates/copy_audit.py's docstring has the rest)."""
import html
import re

from _contract import git, resolve_base

RUN = 4  # consecutive words of a block on an added line that make it the pull request's copy
QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})


def normal(text):
    """Text as a diff line and a page both can hold it: entities decoded, curly quotes straight, one space, lower case."""
    return " ".join(html.unescape(text).translate(QUOTES).split()).lower()


def added_text(args):
    """Every line the pull request adds, normalized, one per line."""
    diff = git(args.repo, "diff", "--unified=0", "--no-color", "--no-ext-diff", resolve_base(args.repo, args.base_ref) + "...HEAD")
    return "\n".join(normal(line[1:]) for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))


def added_by_pull_request(text, added):
    """Whether an added line holds four consecutive whole words of the block, or, for a shorter label, the whole label
    as a string, an element's text or a line of its own (`"Save"`, `>Save<`, JSX text on its own line): a label's
    word inside code (`unsaved`, `row.quote_status`) is not the label."""
    words = normal(text).split()
    if len(words) < RUN:
        label = re.escape(" ".join(words))
        return re.search(r"""(?:[>"'`]\s*|^[ \t]*)%s(?:\s*[<"'`]|[ \t]*$)""" % label, added, re.M) is not None
    return any(re.search(r"(?<!\w)%s(?!\w)" % re.escape(" ".join(words[at:at + RUN])), added)
               for at in range(len(words) - RUN + 1))
