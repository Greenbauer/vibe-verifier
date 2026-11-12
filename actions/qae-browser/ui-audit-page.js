// The layout audit itself. ui-audit.js runs this file inside a page as one script (its `script`),
// and `audit()` measures what is rendered now, returning one finding per defect a person sees at a
// glance and a screenshot diff never flags, because the baseline already had it:
//
//     row-stagger         two controls side by side (row-page.js) whose tops, middles and bottoms all differ; a
//                         control in a column of the row (its item stacks another control above or
//                         below it: a column of fields, an open form) is not lined up with the next
//     row-height          two controls side by side, one taller, so their middles differ (the same
//                         columns aside)
//     field-stagger       cells of one form row whose fields start at different heights (a cell
//                         with a hint or a second input above its field pushes it down); a cell
//                         whose field follows a heading in it is a page section, not judged
//     touching            two bordered or filled controls less than 4px apart, side by side or
//                         stacked, that are not one segmented group (which squares the corners
//                         where they meet on both, sits in one evenly padded track, or shares a
//                         role=group, tablist, radiogroup or toolbar)
//     edge                a bordered or filled control less than 4px from the inside edge of a bordered or filled
//                         container on some sides but not all (a pill track with even padding all
//                         round, and a bar whose segments span it, are designs; a button flush
//                         with a card's right edge is not)
//     spinner-off-center  a spinner in a button more than 1.5px off the button's middle
//     text-wall           more than 60 words in one paragraph of the page's own copy (a table
//                         cell holds a record, not copy, so it is not judged)
//     long-sentence       a sentence of more than 32 words
//     raw-value           text a person should never read: undefined, null, NaN, [object Object],
//                         a snake_case identifier, an ISO timestamp or a UUID, outside code
//     clipped-text        text its box cuts off with no ellipsis
//     clipped-value       a select's option, or a read-only, disabled or number field's value,
//                         wider than the field shows (a field a person types into is cramped-field's)
//     glyph-off-center    the mark inside a badge, avatar or check circle (a count, initials, an
//                         icon, or a CSS-drawn tick) more than 1px off the circle's middle; the mark
//                         is measured by its ink, not its line box, since a digit sits above the
//                         descender space its line box keeps
//     duplicate-name      two buttons a person can press in one dialog or form whose names are the same,
//                         or one the other with words added (name-page.js); buttons of different rows
//                         of one list or table are told apart by their rows
//     wrapped-label       a label of four words or fewer in a pill, badge, button or tab that takes more than one
//                         line (wrap-page.js); a pill's icon above its text counts, a button's does not
//     table-scroll        a table that scrolls sideways while a box around it, within the page's main region,
//                         has room beside it for all of it (scroll-page.js)
//     table-sliver        a table that scrolls sideways by more than 8px and at most 24px, too little to hide a column,
//                         with no room beside it for table-scroll to name (scroll-page.js)
//     uneven-gutter       two page bands stacked across the same column (a header, a toolbar, a table) whose content
//                         starts, or whose boxes end, more than 4px apart from the edge they share, both within 48px
//                         of it (gutter-page.js); a card holds its own padding and is not a band
//     nested-inset        on a phone (a window under 600px), a card inside another card whose content starts more than
//                         36px in from both sides of the window (inset-page.js): a page margin and one card's padding
//                         take 16px each and the borders 4px, so a second box's padding only costs the phone width
//     cramped-field       a field too small for what it asks (field-page.js): one whose name, label or placeholder asks
//                         for free text (a reason, a note, a comment) and shows fewer than 30 average characters of its
//                         font, or a textarea under two lines; or any field a person types into whose value is wider
//                         than it shows, unless the field is already as wide as it can be (full width on a phone)
//     icon-off-center     a button, tab, segment or button-styled link whose only content is one icon, svg, glyph or
//                         spinner, its ink more than 1.5px off the content box's middle in each of three frames
//                         (icon-page.js finds them; the hook reads their screenshots, ink.js), so a spinner tilted
//                         in 3D that inline-flex centers by its box is measured where it draws
//     modal-dismiss       a modal laid over the page (a role=dialog or alertdialog with aria-modal, a <dialog> opened
//                         with showModal(), or a dialog over a full-window backdrop) that pressing Escape leaves open,
//                         or, unless it is an alertdialog, a click on its backdrop at least 24px outside it; one holding
//                         edited input, or an aria-modal card in the page's flow, is not judged (modal-page.js finds
//                         them; the hook replays each in a window of its own, modal-dismiss.js)
//
// Side by side means laid out by one flex or grid row within four levels of both, so a header's
// button and a search field in the page below it are never compared; a row whose two controls each
// sit in a page region of their own under it (aside, nav, main, header, footer or their roles) is an
// app shell's, so a sidebar's button and the page's beside it are not compared either. A control is measured as
// the box a person sees: a checkbox inside a bordered label is the label, and a fixed or sticky bar
// never shares a row with what scrolls under it.
//
// A finding is {rule, at, text, other, detail}: `at` a CSS path to the element, `text` what it
// shows (never a password field's value), `other` the neighbour or container it was measured
// against. Every measure is a rendered rectangle or computed style, so it needs a real browser: the
// catalog's `QA browser tests` CI job runs it on tests/fixtures/ui-audit/ (tests/test_qae_ui_audit.py).
const TOL = 2;   // px two edges may differ before a person sees it
const GAP = 4;   // px two boxed controls, or a control and its container's edge, need between them
const CLEAR = /rgba\(\d+, \d+, \d+, 0\)|transparent/;
const CONTROL = 'button, input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, [role=button], [role=combobox], a[class*="btn"], a[class*="button"]';
const FIELD = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, [role=combobox]";
const GROUP = "[role=group], [role=tablist], [role=radiogroup], [role=toolbar]";
const RAW = /\b(undefined|null|NaN)\b|\[object Object\]|\b[a-z]+_[a-z_]+\b|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/;

function cssPath(el) {
  const parts = [];
  for (let n = el; n && n.nodeType === 1 && parts.length < 5; n = n.parentElement) {
    if (n.id) { parts.unshift(n.tagName.toLowerCase() + "#" + n.id); break; }
    const test = n.getAttribute("data-testid");
    const same = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [];
    parts.unshift(n.tagName.toLowerCase() + (test ? `[data-testid="${test}"]` : "") + (same.length > 1 ? `:nth-of-type(${same.indexOf(n) + 1})` : ""));
  }
  return parts.join(" > ");
}

function shown(el) {
  const value = el.type === "password" ? "" : el.value;
  return (el.getAttribute("aria-label") || el.innerText || value || el.placeholder || "").replace(/\s+/g, " ").trim().slice(0, 60);
}

function visible(el) {
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return r.width >= 2 && r.height >= 2 && s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity) > 0.05;
}

function behind(el) {
  for (let n = el; n; n = n.parentElement) {
    const color = getComputedStyle(n).backgroundColor;
    if (!CLEAR.test(color)) return color;
  }
  return "rgb(255, 255, 255)";
}

// A box a person sees as one: a border all round, a shadow, or a fill unlike what is behind it. A row with
// only a divider under it is not a box.
function boxed(el) {
  const s = getComputedStyle(el);
  const border = ["Top", "Right", "Bottom", "Left"].every(k => parseFloat(s["border" + k + "Width"]) > 0 && !CLEAR.test(s["border" + k + "Color"]));
  const fill = !CLEAR.test(s.backgroundColor) && s.backgroundColor !== behind(el.parentElement);
  return border || fill || shadowed(s);
}

// Whether a box shadow draws: one of its layers has a colour and an offset, blur or spread. Tailwind's ring and shadow
// utilities leave layers of size 0 (`0 0 #0000`, or a ring colour at 0px) on elements that draw no shadow at all.
function shadowed(s) {
  return s.boxShadow !== "none" && s.boxShadow.split(/,(?![^(]*\))/)
    .some(layer => !CLEAR.test(layer) && /[1-9]/.test(layer.replace(/[a-z-]+\([^)]*\)/g, "")));
}

function asSeen(el) {
  if (!el.matches("input[type=checkbox], input[type=radio]")) return el;
  const wrapper = el.closest("label");
  return wrapper && boxed(wrapper) ? wrapper : null;
}

function layer(el) {
  for (let n = el; n; n = n.parentElement) if (/fixed|sticky/.test(getComputedStyle(n).position)) return n;
  return null;
}

// A boxed track with the same thin padding all round (6px at most), holding both: a segmented toggle such
// as Map | List. A card's ordinary padding is wider, so buttons touching inside a card still count.
function evenTrack(el) {
  const s = getComputedStyle(el);
  const pads = ["Top", "Right", "Bottom", "Left"].map(k => parseFloat(s["padding" + k]));
  return pads.every(v => v > 0 && v <= 6 && Math.abs(v - pads[0]) < 0.5) && boxed(el);
}

function segmented(a, b) {
  const group = a.closest(GROUP);
  if (group && group === b.closest(GROUP)) return true;
  let track = a.parentElement;
  for (let up = 0; track && up < 3 && !track.contains(b); up++) track = track.parentElement;
  if (track && track.contains(b) && evenTrack(track)) return true;
  return parseFloat(getComputedStyle(a).borderTopRightRadius) < 1 && parseFloat(getComputedStyle(b).borderTopLeftRadius) < 1;
}

const overlap = (a1, a2, b1, b2) => Math.min(a2, b2) - Math.max(a1, b1);

function stackRules(add, controls, rect) {
  for (const a of controls) {
    const ra = rect.get(a);
    for (const b of controls) {
      const rb = rect.get(b), gap = rb.top - ra.bottom;
      if (a === b || gap < -0.5 || gap >= GAP || overlap(ra.left, ra.right, rb.left, rb.right) <= 0.5 * Math.min(ra.width, rb.width)) continue;
      if (layer(a) === layer(b) && boxed(a) && boxed(b) && !segmented(a, b)) add("touching", a, `${Math.max(0, Math.round(gap))}px above the control below it`, b);
    }
  }
}

// A heading in `cell` above `field`: the cell is a section of the page (a dashboard column of
// titled cards), not a cell of one form row, whose caption is a label.
function underHeading(cell, field) {
  return [...cell.querySelectorAll("h1, h2, h3, h4, h5, h6, [role=heading]")]
    .some(h => visible(h) && h.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING);
}

// The first fields of two side-by-side cells of `row` that start furthest apart: [offset, a, b].
function worstFieldPair(row) {
  const cells = [...row.children].filter(visible)
    .map(cell => [cell.getBoundingClientRect(), cell.matches(FIELD) ? cell : [...cell.querySelectorAll(FIELD)].find(visible), cell])
    .filter(([, field, cell]) => field && !underHeading(cell, field));
  let worst = [0];
  cells.forEach(([ra, fa], i) => cells.slice(i + 1).forEach(([rb, fb]) => {
    if (overlap(ra.top, ra.bottom, rb.top, rb.bottom) <= 0 || overlap(ra.left, ra.right, rb.left, rb.right) > 0) return;
    const qa = fa.getBoundingClientRect(), qb = fb.getBoundingClientRect();
    if (Math.abs(qa.height - qb.height) <= 4 && Math.abs(qa.top - qb.top) > worst[0]) worst = [Math.abs(qa.top - qb.top), fa, fb];
  }));
  return worst;
}

function fieldRules(add) {
  for (const row of document.querySelectorAll("*")) {
    const s = getComputedStyle(row);
    if (!/grid|flex/.test(s.display) || (/flex/.test(s.display) && /column/.test(s.flexDirection))) continue;
    const [worst, a, b] = worstFieldPair(row);
    if (worst > TOL) add("field-stagger", a, `fields in one row start up to ${Math.round(worst)}px apart`, b);
  }
}

// The sides of `container` that `r` sits less than GAP inside, or [] when the inset is a design.
function nearSides(r, container, controls) {
  const pr = container.getBoundingClientRect(), s = getComputedStyle(container);
  if (evenTrack(container)) return [];
  const border = k => parseFloat(s["border" + k + "Width"]);
  const bar = controls.filter(c => container.contains(c)).length > 1;
  if (bar && (r.width >= pr.width - border("Left") - border("Right") - 1 || r.height >= pr.height - border("Top") - border("Bottom") - 1)) return [];
  return [["left", r.left - pr.left - border("Left")], ["right", pr.right - border("Right") - r.right],
    ["top", r.top - pr.top - border("Top")], ["bottom", pr.bottom - border("Bottom") - r.bottom]].filter(([, d]) => d > -0.5 && d < GAP);
}

function edgeRules(add, controls, rect) {
  for (const el of controls.filter(boxed)) {   // text alone against an edge is aligned, not cramped
    const r = rect.get(el);
    let container = el.parentElement;
    for (let up = 0; container && container !== document.body && up < 6 && !boxed(container); up++) container = container.parentElement;
    if (!container || container === document.body || !boxed(container)) continue;
    const pr = container.getBoundingClientRect();
    if (pr.width - r.width < 1 && pr.height - r.height < 1) continue;   // a wrapper the size of the control
    const near = nearSides(r, container, controls);
    if (near.length && near.length < 4) add("edge", el, near.map(([side, d]) => `${side} ${Math.max(0, Math.round(d))}px`).join(", ") + " from its container's edge", container);
  }
}

function spins(n) {
  return visible(n) && (getComputedStyle(n).animationName !== "none" || /spin|loader|loading/i.test(n.getAttribute("class") || "") || n.getAttribute("role") === "progressbar");
}

function spinnerRules(add) {
  for (const button of document.querySelectorAll("button, [role=button]")) {
    const spinner = visible(button) && [...button.querySelectorAll("*")].find(spins);
    if (!spinner) continue;
    const rb = button.getBoundingClientRect(), rs = spinner.getBoundingClientRect();
    const off = Math.abs((rs.top + rs.bottom) / 2 - (rb.top + rb.bottom) / 2);
    if (off > 1.5) add("spinner-off-center", spinner, `${off.toFixed(1)}px off the button's middle`, button);
  }
}

function copyRules(add) {
  const blocks = [...document.querySelectorAll("p, li, dd, td, th, label, span, div, h1, h2, h3, h4, h5, h6, a, button, strong, b, em, small")]
    .filter(el => visible(el) && [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
  for (const el of blocks) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(" ");
    const text = (el.matches("p, li, dd") ? el.innerText : own).replace(/\s+/g, " ").trim();
    const words = text.split(" ").filter(Boolean).length;
    if (el.matches("p, li, dd, div") && !el.closest("td, th, [contenteditable], textarea") && words > 60) add("text-wall", el, `${words} words in one block`);
    const longest = Math.max(...text.split(/(?<=[.!?])\s+/).map(sentence => sentence.split(" ").filter(Boolean).length));
    if (longest > 32) add("long-sentence", el, `a ${longest}-word sentence`);
    const raw = own.match(RAW);
    if (raw && !el.closest("code, pre, kbd, samp")) add("raw-value", el, `shows "${raw[0]}"`);
    const s = getComputedStyle(el);
    if (el.scrollWidth > el.clientWidth + 1 && /hidden|clip/.test(s.overflowX) && s.textOverflow !== "ellipsis") add("clipped-text", el, `${el.scrollWidth - el.clientWidth}px of text cut off`);
  }
}

function clippedValueRules(add) {
  const measure = document.createElement("canvas").getContext("2d");
  const fields = document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=password]), select");
  for (const el of fields) {
    const typed = el.tagName === "INPUT" && !el.readOnly && !el.disabled && el.type !== "number";   // it scrolls
    const text = el.tagName === "SELECT" ? (el.selectedOptions[0] || {}).text : el.value;
    if (typed || !text || el === document.activeElement || !visible(el)) continue;
    const s = getComputedStyle(el);
    measure.font = s.font;
    const room = el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight) - (el.tagName === "SELECT" && s.appearance !== "none" ? 20 : 0);
    const need = measure.measureText(text).width;
    if (need > room + 1) add("clipped-value", el, `"${text.slice(0, 30)}" needs ${Math.ceil(need)}px, the field shows ${Math.floor(room)}px`);
  }
}

function audit() {
  const findings = [], seen = new Set();
  const add = (rule, el, detail, other) => {
    const key = [rule, cssPath(el), other ? cssPath(other) : ""].join("|");
    if (seen.has(key)) return;
    seen.add(key);
    findings.push({ rule, at: cssPath(el), text: shown(el), other: other ? shown(other) : undefined, detail });
  };
  const all = [...document.querySelectorAll(CONTROL + ", input[type=checkbox], input[type=radio]")].map(asSeen);
  const controls = [...new Set(all)].filter(el => el && visible(el) && !el.parentElement.closest(CONTROL));   // a control inside a control is part of it
  const rect = new Map(controls.map(el => [el, el.getBoundingClientRect()]));
  rowRules(add, controls, rect);
  stackRules(add, controls, rect);
  fieldRules(add);
  edgeRules(add, controls, rect);
  spinnerRules(add);
  copyRules(add);
  clippedValueRules(add);
  glyphRules(add);
  nameRules(add);
  wrapRules(add);
  scrollRules(add);
  gutterRules(add);
  insetRules(add);
  crampedRules(add);
  return findings;
}

if (typeof module !== "undefined") module.exports = audit;
