// The uneven-gutter rule of the layout audit (ui-audit-page.js, whose header lists every rule): bands stacked one
// above the other across the same column (a page header, a toolbar, a table) whose content starts, or ends, a
// different distance in from the edge they share, so the page's left and right edges do not line up. A subscribed
// app's rate page started its header 32px in and its toolbar and table below it 16px in (2026-11-06). ui-audit.js
// runs this file beside ui-audit-page.js as one script, so `boxed` and `visible` come from there.
const GUTTER = { max: 48, tol: 4 };   // px: insets more than `tol` apart differ; one past `max` is a layout choice, not a gutter
const REPLACED = "img, svg, video, canvas, input, select, textarea, button, [role=button]";

// What a person sees of `band` nearest its left and right edges: where its lines of text start, and its images,
// controls and drawn boxes. A box as wide as the band (a divider, a full-bleed strip, a chart) is structure, not
// content. Text sets only the left edge: left-aligned lines end where their words do, so a ragged right is not a gutter.
function ink(band) {
  const edge = band.getBoundingClientRect(), seen = { left: Infinity, right: -Infinity };
  const walk = document.createTreeWalker(band, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, { acceptNode: inFlow });
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.nodeType === 3) {
      const at = lineStart(n);
      if (at !== null) seen.left = Math.min(seen.left, at);
    } else if (drawn(n, edge)) {
      const r = n.getBoundingClientRect();
      seen.left = Math.min(seen.left, r.left);
      seen.right = Math.max(seen.right, r.right);
    }
  }
  if (!Number.isFinite(seen.left)) return null;
  return { left: seen.left - edge.left, right: Number.isFinite(seen.right) ? edge.right - seen.right : NaN };
}

// A hidden box, one taken out of the flow (a menu, a tooltip), what an svg draws inside itself (a chart's axis
// labels) and a centered box are not where the band's content sits.
function inFlow(n) {
  const host = n.nodeType === 1 ? n : n.parentElement;
  if (host.parentElement && host.parentElement.closest("svg")) return NodeFilter.FILTER_REJECT;
  if (n.nodeType === 1 && (!visible(n) || /absolute|fixed/.test(getComputedStyle(n).position) || centered(n))) return NodeFilter.FILTER_REJECT;
  return NodeFilter.FILTER_ACCEPT;
}

// Text a block centers or sets right: its lines start where their words do, not at a gutter.
const offStart = s => /center|right|end/.test(s.textAlign) && s.direction !== "rtl";

// A box placed by centering, not by a gutter: one its flex or grid row centers or spreads around (a row of footer
// links), an inline one in a centered line (a logo), and a column centered at a fixed size. An inline box in a
// right-aligned line ends at the right gutter, so it counts.
function centered(el) {
  const up = el.parentElement;
  if (!up) return false;
  const row = getComputedStyle(up);
  if (/flex|grid/.test(row.display) && /center|space-around|space-evenly/.test(row.justifyContent)) return true;
  if (/^inline/.test(getComputedStyle(el).display)) return row.textAlign === "center";
  return fixedColumn(el);
}

// A column centered at a fixed size: both side margins written `auto` and a width or max-width in px (a 300px empty
// state, which lands inside 48px on a phone). The computed style map keeps `auto` and the length as written, where
// getComputedStyle has turned them into the px they came to. A box held in by a relative width (calc(100% - 32px))
// or by margins in px is a gutter.
function fixedColumn(el) {
  const m = el.computedStyleMap && el.computedStyleMap();
  if (!m) return false;
  const auto = k => String(m.get(k)) === "auto", px = k => m.get(k) instanceof CSSUnitValue && m.get(k).unit === "px";
  return auto("margin-left") && auto("margin-right") && (px("width") || px("max-width"));
}

// Where a text node's lines start: the content edge of the block that lays them out, so a first-line indent or a
// centered line does not move it. Null for words with no text, and for text in a centered box.
function lineStart(node) {
  if (!node.textContent.trim()) return null;
  let block = node.parentElement;
  while (block && /^(inline|contents)/.test(getComputedStyle(block).display)) block = block.parentElement;
  if (!block || centered(block) || offStart(getComputedStyle(block))) return null;
  return block.getBoundingClientRect().left + parseFloat(getComputedStyle(block).borderLeftWidth) + parseFloat(getComputedStyle(block).paddingLeft);
}

// An image, a control or a drawn box narrower than the band.
function drawn(n, edge) {
  const r = n.getBoundingClientRect();
  return !(r.left <= edge.left + 1 && r.right >= edge.right - 1) && (n.matches(REPLACED) || boxed(n));
}

// A card: a box with rounded corners. Stacked cards each hold their own padding, so their insets are not gutters.
const card = el => boxed(el) && parseFloat(getComputedStyle(el).borderTopLeftRadius) > 0;

// The bands of a column: children that span its content width and stand at least 24px tall, top to bottom.
function bands(column) {
  const own = column.getBoundingClientRect(), s = getComputedStyle(column);
  const left = own.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft);
  const right = own.right - parseFloat(s.borderRightWidth) - parseFloat(s.paddingRight);
  return [...column.children].filter(c => {
    const r = c.getBoundingClientRect();
    return r.height >= 24 && Math.abs(r.left - left) <= 2 && Math.abs(r.right - right) <= 2 && visible(c) && !card(c);
  }).sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
}

function compareBands(add, list) {
  const insets = list.map(b => ({ b, in: ink(b) })).filter(x => x.in);
  for (let i = 1; i < insets.length; i++) {
    const [above, below] = [insets[i - 1], insets[i]];
    // Layers laid over each other (a fixed bar over a full-screen scroller) are not one band under another.
    if (below.b.getBoundingClientRect().top < above.b.getBoundingClientRect().bottom - 1) continue;
    for (const side of ["left", "right"]) {
      const [a, b] = [above.in[side], below.in[side]];
      if (!(a >= 1 && b >= 1) || a > GUTTER.max || b > GUTTER.max || Math.abs(a - b) <= GUTTER.tol) continue;
      add("uneven-gutter", below.b, `its content starts ${Math.round(b)}px in from the ${side} edge; the band above starts ${Math.round(a)}px in`, above.b);
    }
  }
}

// The page's columns: boxes as wide as the window, or as the main region, at its left edge. Inside them the page's
// gutters are set; a card further in holds its own padding (a table's cells beside a heading), which is not judged.
function pageColumns() {
  const [body] = document.querySelectorAll("body"), [main] = document.querySelectorAll("main, [role=main]");
  if (!body) return [];
  const spans = [body, main].filter(Boolean).map(el => el.getBoundingClientRect());
  const wide = el => { const r = el.getBoundingClientRect(); return spans.some(s => Math.abs(r.left - s.left) <= 2 && Math.abs(r.width - s.width) <= 2); };
  return [body, ...[...body.querySelectorAll("*")].filter(el => el.children.length > 1 && !el.closest("table, svg") && wide(el))];
}

function gutterRules(add) {
  for (const column of pageColumns()) {
    try {   // one odd element never costs the page's other findings
      const list = bands(column);
      if (list.length > 1) compareBands(add, list);
    } catch (e) { continue; }
  }
}

if (typeof module !== "undefined") module.exports = gutterRules;
