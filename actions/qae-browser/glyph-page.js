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
  measure.font = [s.fontStyle, s.fontWeight, s.fontSize, s.fontFamily].join(" ");   // s.font is "" when a font-variant is set
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

// The pseudo's whole 2D transform: CSS applies translate, rotate and scale, then transform. Null for a 3D one.
function pseudoMatrix(s, w, h) {
  const m = new DOMMatrix(), len = (v, of) => v.endsWith("%") ? parseFloat(v) / 100 * of : parseFloat(v);
  if (s.translate !== "none") { const [x, y = "0px"] = s.translate.split(" "); m.translateSelf(len(x, w), len(y, h)); }
  if (s.rotate !== "none") { if (!/^-?[\d.]+deg$/.test(s.rotate)) return null; m.rotateSelf(parseFloat(s.rotate)); }
  if (s.scale !== "none") { const [x, y = x] = s.scale.split(" ").map(parseFloat); m.scaleSelf(x, y); }
  return s.transform === "none" ? m : m.multiplySelf(new DOMMatrix(s.transform));
}

// The ink of a CSS-drawn tick: a ::before or ::after with no fill and a border on two adjacent sides only (a
// rotated corner), through the pseudo's transform, in the page. A knob or a status dot is a fill, not a mark.
function pseudoInk(el, which) {
  const s = getComputedStyle(el, which);
  if (s.content === "none" || s.position !== "absolute" || !/px$/.test(s.left) || !/px$/.test(s.top) || !CLEAR.test(s.backgroundColor)) return null;
  const side = k => !CLEAR.test(s["border" + k + "Color"]) && s["border" + k + "Style"] !== "none" ? parseFloat(s["border" + k + "Width"]) : 0;
  const [t, r, b, l] = ["Top", "Right", "Bottom", "Left"].map(side);
  if ([t, r, b, l].filter(Boolean).length !== 2 || (t && b) || (l && r)) return null;
  const pad = k => parseFloat(s["padding" + k]);
  const content = s.boxSizing !== "border-box";
  const w = parseFloat(s.width) + (content ? pad("Left") + pad("Right") + l + r : 0);
  const h = parseFloat(s.height) + (content ? pad("Top") + pad("Bottom") + t + b : 0);
  if (!w || !h) return null;
  const strips = [t && [0, 0, w, t], r && [w - r, 0, w, h], b && [0, h - b, w, h], l && [0, 0, l, h]].filter(Boolean);
  const m = pseudoMatrix(s, w, h);
  if (!m) return null;
  const host = el.getBoundingClientRect(), hs = getComputedStyle(el), [ox, oy] = s.transformOrigin.split(" ").map(parseFloat);
  const x0 = host.left + parseFloat(hs.borderLeftWidth) + parseFloat(s.left) + parseFloat(s.marginLeft);
  const y0 = host.top + parseFloat(hs.borderTopWidth) + parseFloat(s.top) + parseFloat(s.marginTop);
  const points = strips.flatMap(([a, c, d, e]) => [[a, c], [d, c], [a, e], [d, e]])
    .map(([x, y]) => { const p = m.transformPoint(new DOMPoint(x - ox, y - oy)); return [x0 + ox + p.x, y0 + oy + p.y]; });
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  return box(Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys));
}

// The one mark inside a badge, and what it is: its own short text, a lone svg or img (through wrappers that
// draw nothing), or a CSS-drawn tick. Anything else is not a badge's mark.
function mark(el, measure) {
  let n = el;
  for (let up = 0; up < 3; up++) {
    const text = [...n.childNodes].filter(c => c.nodeType === 3 && c.textContent.trim());
    const kids = [...n.children].filter(visible);
    if (text.length || kids.length !== 1) return ownMark(el, n, text, kids, measure);
    n = kids[0];
    const icon = iconInk(n);
    if (icon !== undefined) return icon;
  }
  return null;
}

// `n`'s own mark when it does not hold exactly one element: its short text, or, on the badge itself, a tick.
function ownMark(el, n, text, kids, measure) {
  if (text.length === 1 && !kids.length) return text[0].textContent.trim().length <= 3 ? textInk(text[0], measure) : null;
  if (text.length || kids.length) return null;
  return n === el ? pseudoInk(el, "::after") || pseudoInk(el, "::before") : null;
}

// An svg's or img's ink; null for a badge inside the badge (judged on its own); undefined for a wrapper to look into.
function iconInk(n) {
  if (n.tagName.toLowerCase() === "svg") return svgInk(n);
  if (n.tagName === "IMG") return n.getBoundingClientRect();
  return boxed(n) ? null : undefined;
}

function glyphRules(add) {
  const measure = document.createElement("canvas").getContext("2d");
  if (!measure) return;
  for (const el of document.querySelectorAll("body *")) {
    const r = el.getBoundingClientRect();   // size first: it is the cheap test, and most elements fail it
    if (r.height < 10 || r.height > 64 || r.width > 3 * r.height || el.closest("svg") || !visible(el) || !badge(el, r)) continue;
    let ink = null;
    try { ink = mark(el, measure); } catch (e) { continue; }   // one odd element never costs the page's other findings
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

if (typeof module !== "undefined") module.exports = glyphRules;
