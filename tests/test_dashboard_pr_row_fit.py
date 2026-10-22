"""The Pull requests row grid fits its panel at every window width from 651px (below that a phone stacks
the cells) up: the columns' minimum widths plus the gaps never add up to more than the row, so no column
runs past the row and the list never scrolls sideways. Before, a 921 to 1060px window with the sidebar
beside the list, and a 651 to 702px one, pushed the CI column and the arrow out of the row."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "dashboard/static/styles.css").read_text()
SCROLLBAR = 17  # A classic (non-overlay) vertical scrollbar in main takes this much of the row.


def block(query):
    """The media rule's text up to its matching close, whether it spans one line or many."""
    start = CSS.index("@media (" + query + ") {")
    depth = 0
    for end in range(CSS.index("{", start), len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[end], 0)
        if depth == 0:
            return CSS[start:end + 1]
    raise ValueError(f"unclosed @media ({query})")


def grids():
    """{max-width: needed px} for each media rule that sets the row grid, plus the unqueried rule as None."""
    found = {None: grid(CSS[:CSS.index("@media")])}
    for limit in re.findall(r"@media \(max-width: (\d+)px\) \{", CSS):
        within = block(f"max-width: {limit}px")
        if ".list-heading, .pr-row {" in within:
            found[int(limit)] = grid(within)
    return found


def rule(selector, within):
    match = re.search(r"(?m)^\s*" + re.escape(selector) + r" \{([^}]*)\}", within)
    return match.group(1)


def px(declaration, name):
    return int(re.search(name + r": (\d+)px", declaration).group(1))


def grid(within):
    declaration = rule(".list-heading, .pr-row", within)
    columns = re.search(r"grid-template-columns: ([^;]+);", declaration).group(1)
    minimums = [int(m) for m in re.findall(r"minmax\((\d+)px", columns)] + [int(f) for f in re.findall(r"(?<![(\d])(\d+)px", columns)]
    return sum(minimums) + px(declaration, "gap") * (len(minimums) - 1)


class PullRowFit(unittest.TestCase):
    def test_the_row_grid_fits_its_panel_at_every_width_from_651px(self):
        top = CSS[:CSS.index("@media")]
        sidebar = int(re.search(r"grid-template-columns: (\d+)px", rule(".shell", top)).group(1))
        main_padding = int(re.search(r"padding: \d+px (\d+)px", rule("main", top)).group(1))
        row_padding = int(re.search(r"padding: \d+px (\d+)px", rule(".pr-row", top)).group(1))
        rules = grids()
        limits = sorted(limit for limit in rules if limit is not None and limit > 650)
        self.assertIn("grid-template-columns: 1fr", rule(".shell", block("max-width: 920px")))
        for width in range(651, 1601):
            beside = sidebar if width > 920 else 0
            room = width - beside - 2 * main_padding - 2 - 2 * row_padding - SCROLLBAR  # 2: the list's border
            needed = rules[next((limit for limit in limits if width <= limit), None)]
            self.assertLessEqual(needed, room, f"{width}px window: columns need {needed}px, row has {room}px")

    def test_a_wide_window_keeps_its_columns(self):
        self.assertIn(
            "grid-template-columns: minmax(220px, 1.4fr) minmax(170px, 1fr) minmax(200px, 1.3fr) minmax(165px, .65fr) 18px; gap: 12px;",
            rule(".list-heading, .pr-row", CSS[:CSS.index("@media")]))


if __name__ == "__main__":
    unittest.main()
