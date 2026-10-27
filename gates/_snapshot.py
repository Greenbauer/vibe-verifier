"""The page snapshots playwright-mcp writes (a call's `snapshot` or `inlineSnapshot` field, Playwright's aria snapshot
in YAML), compared as gates/_probe.py compares the one before an action with the one after it.

A line is one element: `- paragraph [ref=e9]: quantity must be a positive number`, `- cell "AC3" [ref=e4]:`, its
children indented below it. Playwright keeps an element's ref while the element keeps its role and name, gives a new
element a new ref, and starts over when a document loads, so a ref is the same element only with the same role and name.
"""
import re

REF = re.compile(r"\[ref=(?P<ref>(?:f[0-9]+)?e[0-9]+)\]")
NODE = re.compile(r'^\s*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)'
                  r'(?::(?: (?P<text>.*))?)?$')
CONTROLS = frozenset("button link textbox searchbox spinbutton combobox checkbox radio switch slider option listbox "
                     "menuitem menuitemcheckbox menuitemradio tab img".split())
# Roles that divide a page into parts (a banner, the main content): an element holding one is more than a form's part.
LANDMARKS = frozenset("main banner navigation contentinfo complementary".split())
# How far above a control its message may be, in snapshot levels: the field's own cell or row, the table or form.
NEAR = 4
# The share of the elements before an action that must still be there after it: a page answering in place keeps
# nearly all of them, and a crash, an error page or a blank page in its place keeps few.
KEPT = 0.9


def tree(state):
    """[(line, [the indexes of its ancestors, nearest first])] of a snapshot's lines, read from their indentation."""
    nodes, stack = [], []
    for line in state.splitlines():
        depth = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        nodes.append((line, [index for _, index in reversed(stack)]))
        stack.append((depth, len(nodes) - 1))
    return nodes


def elements(state):
    """{ref: (role, name)} of every element a snapshot shows under a ref."""
    found = {}
    for node in map(NODE.match, state.splitlines()):
        ref = REF.search(node.group("attrs")) if node else None
        if ref:
            found[ref.group("ref")] = (node.group("role"), node.group("name"))
    return found


def kept(before, after):
    """The share of `before`'s elements that `after` shows under the same ref, role and name (0 when it shows none)."""
    was, now = elements(before), elements(after)
    return sum(now.get(ref) == role for ref, role in was.items()) / len(was) if was else 0.0


def own_text(line):
    """A snapshot line's own text, casefolded, or '': never its name, which Playwright takes from an element's content
    (a typed value's too), nor a control's."""
    node = NODE.match(line)
    if not node or node.group("role") in CONTROLS:
        return ""
    return " ".join((node.group("text") or "").strip().strip('"').split()).casefold()


def new_text(before, after, used, values):
    """True when `after` shows an element's own text that `before` holds nowhere, under a ref `before` does not show,
    holding no value of `values`, inside an element at most NEAR levels above a control of `used` (refs) that is not
    the page's top and holds no landmark: the message the page put up beside the controls the action used."""
    nodes, was, seen = tree(after), elements(before), " ".join(before.casefold().split())
    whole = {up for line, ups in nodes for up in ups if NODE.match(line) and NODE.match(line).group("role") in LANDMARKS}
    whole.update(index for index, (_, ups) in enumerate(nodes) if not ups)
    near = {up for line, ups in nodes if any("[ref=%s]" % ref in line for ref in used) for up in ups[:NEAR]} - whole
    for line, ups in nodes:
        text, ref = own_text(line), REF.search(line)
        if text and ref and ref.group("ref") not in was and text not in seen and near.intersection(ups) and \
                not any(value in text for value in values):
            return True
    return False
