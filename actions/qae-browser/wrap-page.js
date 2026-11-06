// The wrapped-label rule of the layout audit (ui-audit-page.js, whose header lists every rule): a short label in a
// pill, badge, button or tab laid out on more than one line. A subscribed app's quote panel showed its margin pill
// as an arrow above "12.5%", in a card with room for both on one line (2026-11-06); the glyph rule judges only a
// mark of three characters or fewer, and nothing else measured lines. ui-audit.js runs this file beside
// ui-audit-page.js and glyph-page.js as one script, so `boxed`, `visible`, `radius` and CONTROL come from there.
const LABEL = { words: 4, chars: 24 };   // a label longer than this may wrap: it is a sentence, not a label
const TAB = '[role=tab], [role=status], [role=switch]';

// Text a screen reader reads and a person does not see: a clipped or collapsed box (a .sr-only span).
function hidden(node) {
  const s = getComputedStyle(node);
  return !visible(node) || (s.position === "absolute" && (s.clip !== "auto" || s.clipPath !== "none"));
}

// An icon drawn from a font's ligature ("add", "close"): an icon font first in the stack (system stacks end in
// "Segoe UI Symbol", which every label inherits), or an icon class of its own.
const ICON_CLASS = /(^|\s)(icon|material-[\w-]+|fa[srbl]?|bi|glyphicon)(\s|$)/i;
const iconText = node => /icon|symbol|glyph|awesome/i.test(getComputedStyle(node).fontFamily.split(",")[0]) || ICON_CLASS.test(node.className);

// The rectangles a person reads as the label: each line of its text, and each icon beside it.
function pieces(el, icons) {
  const out = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.nodeType === 3) {
      if (!n.textContent.trim() || hidden(n.parentElement) || (!icons && iconText(n.parentElement))) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      out.push(...[...range.getClientRects()].filter(r => r.width > 0 && r.height > 0));
    } else if (icons && (n.tagName.toLowerCase() === "svg" || n.tagName === "IMG") && visible(n)) {
      out.push(n.getBoundingClientRect());
    }
  }
  return out;
}

// How many lines the rectangles fill: two that share more than 1px of height are one line.
function lines(rects) {
  const bands = [];
  for (const r of [...rects].sort((a, b) => a.top - b.top)) {
    const band = bands.find(b => r.top < b.bottom - 1 && r.bottom > b.top + 1);
    if (band) { band.top = Math.min(band.top, r.top); band.bottom = Math.max(band.bottom, r.bottom); }
    else bands.push({ top: r.top, bottom: r.bottom });
  }
  return bands.length;
}

// A pill or badge: boxed and every corner rounded to half its height, so it was drawn for one line.
function pill(el, r) {
  if (r.height > 96 || !boxed(el)) return false;
  const s = getComputedStyle(el);
  return ["TopLeft", "TopRight", "BottomLeft", "BottomRight"].every(k => radius(s, k, r) >= r.height / 2 - 1);
}

function wrapRules(add) {
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();   // size first: it is the cheap test, and most elements fail it
    if (r.height < 10 || r.height > 96 || el.closest("svg") || (el.parentElement && el.parentElement.closest(CONTROL + ", " + TAB))) continue;
    const text = el.textContent.replace(/\s+/g, " ").trim();
    if (!text || text.length > LABEL.chars || text.split(" ").length > LABEL.words || !visible(el)) continue;
    // A pill's icon and text count as its label; a button's or tab's icon (an svg, an img or an icon font's
    // ligature) may sit above its text by design. Text only a screen reader reads is no line a person sees.
    const isPill = pill(el, r);
    if (!isPill && !el.matches(CONTROL + ", " + TAB)) continue;
    let n = 0;
    try { n = lines(pieces(el, isPill)); } catch (e) { continue; }   // one odd element never costs the page's other findings
    if (n > 1) add("wrapped-label", el, `its label "${text}" takes ${n} lines; a label this short belongs on one`);
  }
}

if (typeof module !== "undefined") module.exports = wrapRules;
