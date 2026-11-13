"""Draws the reference-check fixtures: a made-up planner page with a date-range modal, at 1440 by 900.

close-x/: the reference and a screenshot that both show the modal with its close X; only the dates differ (real data
in place of sample data). The FAIL claims the X is extra, the shape of a consumer's real invented difference.
no-modal/: the same reference against the page with the modal closed, a difference that is really there.
Run `python3 tests/fixtures/reference-check/make.py` to redraw them (Pillow)."""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "date-range-modal"
FONT = ImageFont.load_default(size=18)
TITLE = ImageFont.load_default(size=24)


def page(dates, modal=True):
    image = Image.new("RGB", (1440, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 214, 900], fill="#f4f4f6")
    for n, label in enumerate(["Projects", "Search", "Board", "Plan", "Calendar", "People", "Settings"]):
        draw.text((30, 100 + 52 * n), label, fill="#24305e", font=FONT)
    draw.text((240, 26), "Plan | Spring trip", fill="#24305e", font=TITLE)
    for n in range(4):
        draw.rectangle([630, 110 + 122 * n, 1422, 206 + 122 * n], outline="#cccccc")
        draw.rectangle([1010, 150 + 122 * n, 1095, 188 + 122 * n], outline="#999999")
        draw.text((1032, 158 + 122 * n), "+ Add", fill="#333333", font=FONT)
    if not modal:
        return image
    shade = Image.new("RGB", image.size, "black")
    image = Image.blend(image, shade, 0.45)
    draw = ImageDraw.Draw(image)
    draw.rectangle([460, 100, 980, 435], fill="white")
    draw.text((590, 118), "Edit Your Date Range", fill="#24305e", font=TITLE)
    draw.line([945, 125, 959, 139], fill="#555555", width=2)
    draw.line([945, 139, 959, 125], fill="#555555", width=2)
    draw.line([460, 165, 980, 165], fill="#e6e6e6")
    for n, date in enumerate(dates):
        left = 500 + 236 * n
        draw.rectangle([left, 230, left + 204, 270], outline="#cccccc")
        draw.text((left + 12, 240), date, fill="#333333", font=FONT)
    draw.line([460, 342, 980, 342], fill="#e6e6e6")
    draw.rectangle([680, 370, 760, 408], fill="#f47a5a")
    draw.text((693, 378), "Update", fill="white", font=FONT)
    return image


def save(image, *path):
    full = os.path.join(HERE, *path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    image.save(full, optimize=True)


STEP = "step 24: compared with reference %s -> " % KEY
CASES = {"close-x": (page(["2026-12-10", "2026-12-12"]), "The modal adds a close X absent from the reference and leaves a gap "
                     "between the date fields", "The date modal adds a close control and separates the date fields unlike the reference."),
         "no-modal": (page([], modal=False), "The Edit Your Date Range modal is missing: the screen shows the plan with no "
                      "modal open", "The date range modal is not open where the reference shows it.")}

if __name__ == "__main__":
    for name, (shot, saw, why) in CASES.items():
        save(page(["2026-09-10", "2026-09-13"]), name, "references", KEY + ".png")
        save(shot, name, "qae", "TC11-step-24.png")
        with open(os.path.join(HERE, name, "qae", "TC11.md"), "w") as handle:
            handle.write("- %s%s\n" % (STEP, saw))
        with open(os.path.join(HERE, name, "verdict.md"), "w") as handle:
            handle.write("acceptance-check: TC11 -- FAIL -- %s (qae/TC11.md::%s%s)\n" % (why, STEP, saw))
