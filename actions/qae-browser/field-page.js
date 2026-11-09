// The cramped-field rule of the layout audit (ui-audit-page.js, whose header lists every rule): a field too small for
// what it asks. A subscribed app's quote table showed, once a row's "No bid" box was ticked, an input for the reason
// squeezed into a column about 90px wide, so "No stock" already scrolled its start out of view (2026-11-07).
// ui-audit.js runs this file beside ui-audit-page.js as one script, so `visible` comes from there. A field is cramped
// when:
// - it asks for free text (its accessible name, label, title, placeholder, or else its name or id, names a reason, a
//   note, a comment...) and shows fewer than ROOM average characters of its own font, or is a textarea under two lines;
// - or what it holds is wider than it shows, whatever it asks: what was typed is already cut off. The hook measures
//   again once the explorer stops typing, and when a field appears. A field already as wide as it can be (it fills its
//   container, and each container up to the main region fills its own) has no room to show more, so a long value in it
//   is not judged: a 51-character name in a phone's full-width box (2026-11-09).
// A read-only, disabled or number field's value cut off is clipped-value's.

// Words whose answer is a phrase or a sentence. Beside a word in SHORT_ANSWER they name a short answer instead
// ("Reason code", "Note title", "Search comments").
const FREE_TEXT = /\b(reasons?|notes?|comments?|descriptions?|describe|explain|explanation|why|messages?|details|feedback|summary|instructions?|memo|remarks?)\b/i;
const SHORT_ANSWER = /\b(name|title|subject|code|number|id|email|phone|city|zip|postcode|search|qty|quantity|amount|price|date|sku)\b/i;
// Five words of everyday English (4.7 letters and a space each), so a person can read back the phrase they are typing.
// Measured 2026-11-07: the reason field that set this off shows 16 (its copy in tests/fixtures/ui-audit/bad.html), and a
// subscribed app's contact form shows 36 in its message box at a phone's width and 99 at 1280.
const ROOM = 30;
const SAMPLE = "the supplier is out of stock until next month";   // what an average character of the field's font is measured on
const FREE_TYPES = ["text", "search"];   // the input types a sentence goes in; a missing or unknown type reads as "text"
const TYPED_TYPES = ["text", "search", "email", "url", "tel"];   // a field a person types a line into
const SHORT_MODES = /^(numeric|decimal|tel|email|url)$/;   // a keyboard for a number or an address, not a sentence

// What the field says it asks for: its accessible name, labels, title and placeholder, or, with none of those, its name
// and id split into words ("noBidReason", "no_bid_reason").
function asksFor(el) {
  const named = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean).map(id => document.getElementById(id)).filter(Boolean);
  const said = [...named.map(n => n.textContent), el.getAttribute("aria-label"), ...[...(el.labels || [])].map(l => l.textContent), el.title, el.placeholder]
    .filter(Boolean).join(" ").trim();
  return (said || [el.name, el.id].join(" ")).replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_\-.[\]:]+/g, " ");
}

// The element that names the field, if one does: a finding names it too, so a pull request that adds a label is told.
const labelOf = el => (el.labels && el.labels[0]) || document.getElementById((el.getAttribute("aria-labelledby") || "").split(/\s+/)[0]) || undefined;

// A textarea that grows with what is typed: an autosize script sets its height inline, or CSS sizes it to its content.
const grows = (el, s) => Boolean(el.style.height) || s.fieldSizing === "content";

// Why `el`, a field a person types into, is too small for free text, or null.
function tooSmall(el, area, measure) {
  if (!(area || FREE_TYPES.includes(el.type)) || SHORT_MODES.test(el.inputMode)) return null;
  const words = asksFor(el);
  if (!FREE_TEXT.test(words) || SHORT_ANSWER.test(words)) return null;
  const s = getComputedStyle(el);
  measure.font = [s.fontStyle, s.fontWeight, s.fontSize, s.fontFamily].join(" ");   // s.font is "" when a font-variant is set
  const char = measure.measureText(SAMPLE).width / SAMPLE.length;
  const wide = el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight);
  if (!(char > 0) || (el.maxLength > 0 && el.maxLength * char <= wide)) return null;   // it never takes more than it shows
  if (wide < ROOM * char) return `it asks for free text and shows ${Math.floor(wide / char)} characters at a time; a sentence needs ${ROOM}`;
  const line = s.lineHeight === "normal" ? 1.2 * parseFloat(s.fontSize) : parseFloat(s.lineHeight);
  const tall = el.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
  if (area && !grows(el, s) && tall < 2 * line - 1) return "it asks for free text in one line; a sentence needs two";
  return null;
}

// Whether `el` is as wide as it can be: it fills its container's content box, and so does each container up to the
// main region (or the body), whatever their padding. A box in a table cell, a grid column, a sidebar or a centered
// dialog stops short somewhere on the way up, so there is room to widen it.
const boxless = e => getComputedStyle(e).display === "contents";   // laid out as its children, with no box of its own

function fillsColumn(el) {
  for (let n = el; n.parentElement; n = n.parentElement) {
    if (n.matches("main, [role=main], body")) return true;
    if (boxless(n)) continue;
    let p = n.parentElement;
    while (p.parentElement && boxless(p)) p = p.parentElement;
    const s = getComputedStyle(p);
    if (n.getBoundingClientRect().width < p.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight) - 2) return false;
  }
  return true;
}

function crampedRules(add) {
  const measure = document.createElement("canvas").getContext("2d");
  if (!measure) return;
  for (const el of document.querySelectorAll("input, textarea")) {
    const area = el.tagName === "TEXTAREA";
    if (!(area || TYPED_TYPES.includes(el.type)) || el.readOnly || el.disabled || !visible(el)) continue;
    try {   // one odd element never costs the page's other findings
      const over = el.value ? el.scrollWidth - el.clientWidth : 0;
      const why = over > 1 && !fillsColumn(el) ? `what it holds is ${over}px wider than it shows, so part of it is out of view` : tooSmall(el, area, measure);
      if (why) add("cramped-field", el, why, labelOf(el));
    } catch (e) { continue; }
  }
}

if (typeof module !== "undefined") module.exports = crampedRules;
