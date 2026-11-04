// The glyph-off-center rule of the layout audit (ui-audit-page.js, whose header lists every rule): the mark
// inside a badge, avatar or check circle more than 1px off the circle's middle. ui-audit.js runs this file
// beside ui-audit-page.js as one script, so `boxed`, `visible` and CLEAR come from there.
const GLYPH_TOL = 1;   // px a badge's mark may sit off its circle's middle

function radius(s, corner, r) {
  const v = s["border" + corner + "Radius"];
  return v.endsWith("%") ? parseFloat(v) / 100 * Math.min(r.width, r.height) : parseFloat(v);
}

// A badge, avatar or check: boxed, at most 64px tall, every corner rounded to half its height (a circle
// or a pill), and no wider than three times its height.
function badge(el, r) {
  if (r.height < 10 || r.height > 64 || r.width > 3 * r.height || !boxed(el)) return false;
  const s = getComputedStyle(el);
  return ["TopLeft", "TopRight", "BottomLeft", "BottomRight"].every(k => radius(s, k, r) >= r.height / 2 - 1);
}

const box = (left, top, right, bottom) => ({ left, top, right, bottom });

function textInk(node, measure) {
  const s = getComputedStyle(node.parentElement), range = document.createRange();
  range.selectNodeContents(node);
  const at = range.getBoundingClientRect();
  measure.font = s.font;
  measure.letterSpacing = s.letterSpacing === "normal" ? "0px" : s.letterSpacing;
  let text = node.textContent.trim();
  if (s.textTransform === "uppercase") text = text.toUpperCase();
  if (s.textTransform === "lowercase") text = text.toLowerCase();
  const m = measure.measureText(text);
  const baseline = (at.top + m.fontBoundingBoxAscent + at.bottom - m.fontBoundingBoxDescent) / 2;
  return box(at.left - m.actualBoundingBoxLeft, baseline - m.actualBoundingBoxAscent, at.left + m.actualBoundingBoxRight, baseline + m.actualBoundingBoxDescent);
}

function svgInk(svg) {
  let b;
  try { b = svg.getBBox(); } catch (e) { return svg.getBoundingClientRect(); }
  const ctm = svg.getScreenCTM();
  if (!ctm || !b.width || !b.height) return svg.getBoundingClientRect();
  const [p, q] = [new DOMPoint(b.x, b.y).matrixTransform(ctm), new DOMPoint(b.x + b.width, b.y + b.height).matrixTransform(ctm)];
  return box(Math.min(p.x, q.x), Math.min(p.y, q.y), Math.max(p.x, q.x), Math.max(p.y, q.y));
}

// The ink of a ::before or ::after drawn with borders or a fill (a CSS tick is a rotated corner of two
// borders): the drawn strips, through the pseudo's transform, in the page.
function pseudoInk(el, which) {
  const s = getComputedStyle(el, which);
  if (s.content === "none" || s.position !== "absolute" || !/px$/.test(s.left) || !/px$/.test(s.top)) return null;
  const w = parseFloat(s.width), h = parseFloat(s.height), side = k => !CLEAR.test(s["border" + k + "Color"]) ? parseFloat(s["border" + k + "Width"]) : 0;
  const strips = !CLEAR.test(s.backgroundColor) ? [[0, 0, w, h]] : [];
  if (side("Top")) strips.push([0, 0, w, side("Top")]);
  if (side("Right")) strips.push([w - side("Right"), 0, w, h]);
  if (side("Bottom")) strips.push([0, h - side("Bottom"), w, h]);
  if (side("Left")) strips.push([0, 0, side("Left"), h]);
  if (!strips.length || !w || !h) return null;
  const host = el.getBoundingClientRect(), hs = getComputedStyle(el);
  const x0 = host.left + parseFloat(hs.borderLeftWidth) + parseFloat(s.left), y0 = host.top + parseFloat(hs.borderTopWidth) + parseFloat(s.top);
  const m = new DOMMatrix(s.transform === "none" ? undefined : s.transform), [ox, oy] = s.transformOrigin.split(" ").map(parseFloat);
  const points = strips.flatMap(([a, b, c, d]) => [[a, b], [c, b], [a, d], [c, d]])
    .map(([x, y]) => { const p = m.transformPoint(new DOMPoint(x - ox, y - oy)); return [x0 + ox + p.x, y0 + oy + p.y]; });
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  return box(Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys));
}

// The one mark inside a badge, and what it is: its own short text, a lone svg or img (through wrappers that
// draw nothing), or a pseudo-element drawn with borders or a fill. Anything else is not a badge's mark.
function mark(el, measure) {
  let n = el;
  for (let up = 0; up < 3; up++) {
    const text = [...n.childNodes].filter(c => c.nodeType === 3 && c.textContent.trim());
    const kids = [...n.children].filter(visible);
    if (text.length === 1 && !kids.length) return text[0].textContent.trim().length <= 3 ? textInk(text[0], measure) : null;
    if (text.length || kids.length > 1) return null;
    if (!kids.length) return n === el ? pseudoInk(el, "::after") || pseudoInk(el, "::before") : null;
    n = kids[0];
    if (n.tagName === "svg" || n.tagName === "SVG") return svgInk(n);
    if (n.tagName === "IMG") return n.getBoundingClientRect();
    if (boxed(n)) return null;   // a badge inside a badge is judged on its own
  }
  return null;
}

function glyphRules(add) {
  const measure = document.createElement("canvas").getContext("2d");
  for (const el of document.querySelectorAll("body *")) {
    if (el.tagName === "svg" || el.closest("svg") || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (!badge(el, r)) continue;
    const ink = mark(el, measure);
    if (!ink) continue;
    const s = getComputedStyle(el);
    const cx = (r.left + r.right) / 2 + (parseFloat(s.borderLeftWidth) - parseFloat(s.borderRightWidth)) / 2;
    const cy = (r.top + r.bottom) / 2 + (parseFloat(s.borderTopWidth) - parseFloat(s.borderBottomWidth)) / 2;
    const dx = (ink.left + ink.right) / 2 - cx, dy = (ink.top + ink.bottom) / 2 - cy;
    if (Math.abs(dx) <= GLYPH_TOL && Math.abs(dy) <= GLYPH_TOL) continue;
    const off = [[dx, "right", "left"], [dy, "low", "high"]].filter(([d]) => Math.abs(d) > GLYPH_TOL)
      .map(([d, plus, minus]) => `${Math.abs(d).toFixed(1)}px ${d > 0 ? plus : minus}`);
    add("glyph-off-center", el, `its mark sits ${off.join(" and ")} of the circle's middle`);
  }
}
