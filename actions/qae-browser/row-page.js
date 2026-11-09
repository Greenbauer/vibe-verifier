// The row rules of the layout audit (ui-audit-page.js, whose header lists every rule): which controls sit side by
// side in one row, and row-stagger, row-height and touching between neighbours in it. ui-audit.js runs this file
// beside ui-audit-page.js as one script, so `layer`, `visible`, `boxed`, `segmented`, `overlap`, CONTROL, TOL and GAP
// come from there.
const REGION = "aside, nav, main, header, footer, [role=complementary], [role=navigation], [role=main], [role=banner], [role=contentinfo]";

// Whether a page region (a sidebar, the main region, a nav, a header or footer) lies between `el` and `row`. When
// one does for both controls, the row is an app shell's laying out regions, not controls: a sidebar's Collapse
// button and the page's Save button beside it are in no row a person sees. When only one is in a region (a header's
// nav beside its Account button), the row is the region's own and still counts.
function regionBetween(el, row) {
  for (let n = el.parentElement; n && n !== row; n = n.parentElement) if (n.matches(REGION)) return true;
  return false;
}

// The flex or grid row that lays out `a` and `b` side by side, or null.
function layoutRow(a, b) {
  let n = a.parentElement;
  for (let up = 0; n && up < 4 && !n.contains(b); up++) n = n.parentElement;
  if (!n || !n.contains(b) || (regionBetween(a, n) && regionBetween(b, n))) return null;
  const s = getComputedStyle(n);
  return /grid/.test(s.display) || (/flex/.test(s.display) && !/column/.test(s.flexDirection)) ? n : null;
}

const sameRow = (a, b) => layoutRow(a, b) !== null;

// Whether the item of `row` that holds `el` also holds a control above or below it: the item is a column (a stack
// of fields, an open form with its own buttons under its field), not a cell of the row, so lining `el` up with the
// next column says nothing. A hint or label under or over a field is not a control and keeps the cell a cell.
function stacked(el, row) {
  let item = el;
  while (item.parentElement !== row) item = item.parentElement;
  if (item === el) return false;
  const r = el.getBoundingClientRect();
  return [...item.querySelectorAll(CONTROL)].some(c => {
    if (c.contains(el) || el.contains(c) || !visible(c)) return false;
    const rc = c.getBoundingClientRect();
    return overlap(r.top, r.bottom, rc.top, rc.bottom) <= 0;
  });
}

// The control nearest to the right of `a` in its row, or null.
function rightOf(a, ra, controls, rect) {
  let right = null;
  for (const b of controls) {
    const rb = rect.get(b);
    const gap = rb.left - ra.right;
    if (a === b || gap < -0.5 || gap > 240 || overlap(ra.top, ra.bottom, rb.top, rb.bottom) <= 0) continue;
    if (layer(a) !== layer(b) || !sameRow(a, b)) continue;
    if (!right || rb.left < rect.get(right).left) right = b;
  }
  return right;
}

function rowRules(add, controls, rect) {
  for (const a of controls) {
    const ra = rect.get(a), right = rightOf(a, ra, controls, rect);
    if (!right) continue;
    const rr = rect.get(right);
    const row = layoutRow(a, right);
    // A column beside a control is not a row to line up (touching still applies), so it counts as lined up.
    const middles = stacked(a, row) || stacked(right, row) ? 0 : Math.abs((ra.top + ra.bottom) / 2 - (rr.top + rr.bottom) / 2);
    if (middles > TOL && Math.abs(ra.top - rr.top) > TOL && Math.abs(ra.bottom - rr.bottom) > TOL) {
      add("row-stagger", a, `${Math.round(middles)}px off the middle of the control beside it`, right);
    } else if (middles > TOL && Math.abs(ra.height - rr.height) > TOL) {
      add("row-height", a, `${Math.round(ra.height)}px tall beside a ${Math.round(rr.height)}px control`, right);
    }
    const gap = rr.left - ra.right;
    if (gap < GAP && boxed(a) && boxed(right) && !segmented(a, right)) add("touching", a, `${Math.max(0, Math.round(gap))}px from the control beside it`, right);
  }
}

if (typeof module !== "undefined") module.exports = rowRules;
