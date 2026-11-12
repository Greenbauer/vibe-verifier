// The table-scroll and table-sliver rules of the layout audit (ui-audit-page.js, whose header lists every rule). A
// table that scrolls sideways while the layout around it has room for all of it is table-scroll: a subscribed app's
// quote page cut its lane table off at the amount column behind a scrollbar, beside a wide empty area of the page
// (2026-11-06). A table that scrolls sideways by a sliver, with no room around it, is table-sliver: a subscribed app's
// list page drew a 992px table in a 982px box at 1280px (2026-11-06), so a scrollbar showed for 10px of its last
// column, and table-scroll found no room to blame. ui-audit.js runs this file beside ui-audit-page.js as one script,
// so `visible` comes from there.
const TABLE = "table, [role=table], [role=grid]";
const OVERLAY = "dialog, [role=dialog], [role=alertdialog], [aria-modal=true]";
const SLACK = 8;   // px a table may overflow its box before it scrolls in a way a person notices
// px a table may hide past SLACK and still hide nothing whole: no column a person reads fits in it (a checkbox
// column with its cell padding is wider), so its scrollbar shows only because the layout missed by a few pixels. A
// table that hides more may scroll by design (a wide table on a phone, a table that hides whole columns) and is not
// judged. A fixed bound, not "less than the last column": a phone's table cuts its last column part way by design.
const SLIVER = 24;

// The width of `el`'s content box: its box less its borders and padding.
function contentWidth(el) {
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  return r.width - ["Left", "Right"].reduce((sum, k) => sum + parseFloat(s["border" + k + "Width"]) + parseFloat(s["padding" + k]), 0);
}

// The room beside `child` inside `parent`: its content width less the span `child` and the siblings beside it
// (laid out at the same height) take up. An empty column is room; a visible card beside the table is not.
// A display:contents wrapper draws no box of its own: its children are the parent's boxes.
const boxes = n => getComputedStyle(n).display === "contents" ? [...n.children].flatMap(boxes) : [n];

function roomBeside(parent, child) {
  const own = child.getBoundingClientRect();
  const beside = [...parent.children].flatMap(boxes).filter(n => n === child || (visible(n) && (r => r.top < own.bottom - 1 && r.bottom > own.top + 1)(n.getBoundingClientRect())));
  const rects = beside.map(n => n.getBoundingClientRect());
  return contentWidth(parent) - (Math.max(...rects.map(r => r.right)) - Math.min(...rects.map(r => r.left)));
}

// The most room any box around `el` has beside it, up to its dialog, else the page's main region, else the body:
// the navigation beside the main region is not room a table could take, and nor is the page behind a dialog. A
// fixed box (an overlay a dialog's panel sits centered in) covers the page under it, so its width is not room either,
// unless it is the main region itself (an app whose content pane is fixed beside a fixed navigation).
function roomAround(el) {
  const top = el.closest(OVERLAY) || el.closest("main, [role=main]") || document.body;
  let room = 0;
  for (let child = el, parent = el.parentElement; parent && child !== top; child = parent, parent = parent.parentElement) {
    if (getComputedStyle(parent).position !== "fixed" || parent.matches("main, [role=main]")) room = Math.max(room, roomBeside(parent, child));
  }
  return room;
}

// The table `el` scrolls sideways and how many px of it are hidden, or null. A box may scroll for something
// else beside a table that fits, so the widest table's own width is what counts.
function hiddenTable(el) {
  if (el.scrollWidth - el.clientWidth <= SLACK) return null;   // overflow first: it is the cheap test, and most elements fail it
  const s = getComputedStyle(el);
  if (!/auto|scroll/.test(s.overflowX) || !visible(el)) return null;
  const width = t => t.getBoundingClientRect().width;
  const table = el.matches(TABLE) ? el : [...el.querySelectorAll(TABLE)].filter(visible).sort((a, b) => width(b) - width(a))[0];
  if (!table) return null;
  const shows = el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight);
  const need = table === el ? el.scrollWidth - el.clientWidth : width(table) - shows;
  return need > SLACK ? { table, need } : null;
}

function scrollRules(add) {
  for (const el of document.querySelectorAll("body *")) {
    try {   // one odd element never costs the page's other findings
      const hidden = hiddenTable(el);
      if (!hidden) continue;
      const room = roomAround(el);
      if (room >= hidden.need) add("table-scroll", hidden.table, `it scrolls sideways to show ${Math.ceil(hidden.need)}px more, while the layout has ${Math.floor(room)}px unused beside it`);
      else if (hidden.need <= SLIVER) add("table-sliver", hidden.table, `it scrolls sideways to show only ${Math.ceil(hidden.need)}px more, too little to hide a column`);
    } catch (e) { continue; }
  }
}

if (typeof module !== "undefined") module.exports = scrollRules;
