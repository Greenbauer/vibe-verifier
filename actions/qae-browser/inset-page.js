// The nested-inset rule of the layout audit (ui-audit-page.js, whose header lists every rule): on a phone, a card
// inside another card whose content starts more than 32px in from both sides of the window, so the boxes around it
// spend width a phone does not have. A page margin of 16px (Android's standard margin for compact windows) and one
// card's padding of 16px are the budget; a second box's border and padding are what nesting adds. Subscribed apps
// showed it on phones in 2026-11: a results page whose line cards (Total, Part, Date, Price) sat in a card of their own,
// so each line's text ran in a column some 100px narrower than the 375px window. ui-audit.js runs this file beside
// ui-audit-page.js as one script, so `boxed`, `visible` and CONTROL come from there.
// px: a window narrower than `phone` is a phone's; `budget` is the inset a page margin and one card's padding take a side,
// and `borders` the slack for the boxes' borders, so a border alone never makes a finding.
const INSET = { phone: 600, budget: 32, borders: 4 };

// The window's width less a scrollbar: what the page lays out in.
const pageWidth = () => document.documentElement.clientWidth;

// A card: a block that draws a box a person sees (a border, a fill unlike what is behind it, or a shadow), at least half
// the window wide, and not a control, a link, a table's cell or row, or what an svg draws. A pill, a badge, a button and a
// link styled as one are boxes too, but they hold a label, not a section, and a tile in a two-column grid is narrower
// than half the window. A table that a phone lays out as a stack of cards (its rows `display: block`) is cards.
function cardBox(el) {
  if (el.matches(CONTROL) || el.closest(CONTROL + ", a[href], summary, label, svg")) return false;
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  return !/^inline|contents|table/.test(s.display) && r.width >= pageWidth() / 2 && visible(el) && boxed(el);
}

// A box around `el` that costs it width: a card that does not span the window (a full-width band's padding is the page's margin).
function outerCard(el) {
  for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
    if (!cardBox(n)) continue;
    const r = n.getBoundingClientRect();
    if (r.left > 1 || r.right < pageWidth() - 1) return n;
  }
  return null;
}

// How far in from each side of the window the card's content box sits.
function contentInset(el) {
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  return {
    left: r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft),
    right: pageWidth() - (r.right - parseFloat(s.borderRightWidth) - parseFloat(s.paddingRight)),
  };
}

function insetRules(add) {
  if (pageWidth() >= INSET.phone) return;
  const nested = [...document.querySelectorAll("body *")].filter(el => {
    try {   // one odd element never costs the page's other findings
      if (!cardBox(el) || !el.innerText.trim()) return false;
      const at = contentInset(el);
      return Math.min(at.left, at.right) > INSET.budget + INSET.borders && outerCard(el);
    } catch (e) { return false; }
  });
  // The innermost card names the defect: a card around it is the nesting, not a second finding.
  for (const el of nested.filter(el => !nested.some(other => other !== el && el.contains(other)))) {
    const at = contentInset(el), lost = Math.round(at.left + at.right), width = pageWidth();
    add("nested-inset", el, `its content starts ${Math.round(at.left)}px in from the left and ends ${Math.round(at.right)}px in from the right of a ${width}px window: ${lost}px (${Math.round(100 * lost / width)}%) goes to the boxes around it, where a page margin and one card's padding take ${INSET.budget}px a side`, outerCard(el));
  }
}

if (typeof module !== "undefined") module.exports = insetRules;
