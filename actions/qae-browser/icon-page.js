// The icon-off-center rule's in-page half (ui-audit-page.js's header lists every rule; ink.js reads the pixels): the
// controls whose only visible content is one icon, svg, glyph or spinner, with the boxes the hook screenshots to find
// where that mark's ink sits. glyph-page.js judges the mark of a round badge; this judges a button's, a tab's, a
// segment's or a button-styled link's. ui-audit.js runs this file beside the others as one script, so `boxed`,
// `visible`, `spins`, `cssPath`, `shown`, `radius`, `hidden` and `iconText` come from there.
const PRESS = 'button, [role=button], [role=tab], [role=radio], a, label[class*="btn"], label[class*="button"]';
const DRAWN = "svg, img, canvas";

// Whether `el` shows inside `control`: not hidden, faded out (a label kept at opacity 0 to hold the width while a
// spinner shows) or only for a screen reader, nor inside a box that is.
function showsIn(el, control) {
  for (let n = el; n && n !== control; n = n.parentElement) if (hidden(n)) return false;
  return true;
}

// Whether `control` shows any text a person reads as a label: an icon font's ligature ("close") and text an svg draws
// are part of the icon.
function labelled(control) {
  const walk = document.createTreeWalker(control, NodeFilter.SHOW_TEXT);
  for (let t = walk.nextNode(); t; t = walk.nextNode()) {
    const el = t.parentElement;
    if (t.textContent.trim() && !el.closest("svg") && !iconText(el) && showsIn(el, control)) return true;
  }
  return false;
}

// The one element that draws `control`'s mark, through wrappers that draw nothing, or null: an svg, img or canvas, a
// spinner, an icon font's glyph, a box with nothing in it (an icon drawn by a background or a mask), or a box whose
// visible children are all laid over each other (the orbits of one spinner).
function loneMark(control) {
  let n = control;
  for (let depth = 0; depth < 4; depth++) {
    const kids = [...n.children].filter(k => !hidden(k));
    if (n !== control && (n.matches(DRAWN) || spins(n) || iconText(n) || !kids.length)) return n;
    if (n !== control && kids.every(k => /absolute|fixed/.test(getComputedStyle(k).position))) return n;
    if (kids.length !== 1) return null;
    n = kids[0];
  }
  return null;
}

// A ::before or ::after that draws something: a caret beside an icon is a second mark, and a spinner a loading
// button draws in its own ::after is the only one.
function drawnPseudo(el, which) {
  const s = getComputedStyle(el, which);
  return !/^(none|normal)$/.test(s.content) && s.display !== "none" && s.visibility !== "hidden" && parseFloat(s.opacity) > 0.05;
}

// The boxes the hook needs to read `el`'s ink, in the window's CSS px: the whole pixels to screenshot (`clip`), the box
// inside its border, 1px in, with its corners rounded as the border's are (`scan`: ink outside it is the border, or
// what is behind the corners), and its content box's middle (`center`).
function inkBoxes(el, r) {
  const s = getComputedStyle(el), px = k => parseFloat(s[k]);
  const scan = { left: r.left + px("borderLeftWidth") + 1, top: r.top + px("borderTopWidth") + 1,
    right: r.right - px("borderRightWidth") - 1, bottom: r.bottom - px("borderBottomWidth") - 1 };
  const most = Math.max(0, Math.min(scan.right - scan.left, scan.bottom - scan.top) / 2);
  const border = { TopLeft: ["Top", "Left"], TopRight: ["Top", "Right"], BottomRight: ["Bottom", "Right"], BottomLeft: ["Bottom", "Left"] };
  scan.radius = Object.entries(border).map(([corner, sides]) =>
    Math.min(most, Math.max(0, radius(s, corner, r) - Math.max(...sides.map(k => px("border" + k + "Width"))) - 1)));
  const center = { x: (r.left + px("borderLeftWidth") + px("paddingLeft") + r.right - px("borderRightWidth") - px("paddingRight")) / 2,
    y: (r.top + px("borderTopWidth") + px("paddingTop") + r.bottom - px("borderBottomWidth") - px("paddingBottom")) / 2 };
  const x = Math.floor(r.left), y = Math.floor(r.top);
  return { clip: { x, y, width: Math.ceil(r.right) - x, height: Math.ceil(r.bottom) - y }, scan, center };
}

// The one mark of `el`, named by its tag and class or its pseudo-element (so an icon swapped for a spinner is sampled
// again), when `el` is a control whose only content is one mark, drawn in a box a person sees, whole in the window and
// not under anything: a screenshot of the window is what the hook reads. Else null.
function iconMark(el, r, room) {
  // In the window first: it is the cheap test, and most of a long page's links and buttons fail it.
  if (r.left < 0 || r.top < 0 || Math.ceil(r.right) > room.width || Math.ceil(r.bottom) > room.height) return null;
  if (!visible(el) || !boxed(el) || labelled(el)) return null;
  const lone = loneMark(el), pseudos = ["::before", "::after"].filter(which => drawnPseudo(el, which));
  if ((lone ? 1 : 0) + pseudos.length !== 1) return null;
  const top = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
  if (!top || !(top === el || el.contains(top))) return null;
  return lone ? lone.tagName.toLowerCase() + "." + (lone.getAttribute("class") || "") : pseudos[0];
}

// Every control in the window whose only content is one icon, svg, glyph or spinner, for the hook to screenshot:
// {at, mark, text, clip, scan, center}. A box with no border, fill or shadow has no middle a person sees, so it is not judged.
function iconControls() {
  const room = { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight };
  const out = [];
  for (const el of document.querySelectorAll(PRESS)) {
    try {   // one odd element never costs the page's other controls
      const r = el.getBoundingClientRect();
      const mark = iconMark(el, r, room);
      if (mark) out.push({ at: cssPath(el), mark, text: shown(el), ...inkBoxes(el, r) });
    } catch (e) { continue; }
  }
  return out;
}

if (typeof module !== "undefined") module.exports = iconControls;
