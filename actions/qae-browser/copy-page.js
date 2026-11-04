// The copy record's block finder. ui-audit.js runs this file inside a page together with
// ui-audit-page.js (for `visible` and `cssPath`), and `copyBlocks()` returns every piece of the
// page's own copy a person can read now, once per distinct text, for the copy-audit grader:
//
//     {kind: "sentence" | "label", text, at}
//
// A block is the words one box shows: each visible text node belongs to its nearest ancestor that is
// not laid out inline, so a link or a bold word inside a paragraph is part of the paragraph's
// sentence, and a button, a heading, a table header or a badge is a block of its own. A field's
// placeholder is a label too. A table cell holds a record, not copy, and code, an editor and a field
// a person types into hold their words: none of these is recorded, nor is text with no letter in it
// or a lone email address, URL or number. A sentence is a block that ends one (". ", "!", "?") with
// four words or more, or any block of eight words or more; everything else is a label.
const NOT_COPY = "td, code, pre, kbd, samp, textarea, select, svg, [contenteditable=''], [contenteditable=true], [aria-hidden=true]";
const VALUE = /^(\S+@\S+\.\S+|https?:\/\/\S+|[\d\s.,:%$€£+\-/()]+)$/;
const LONGEST = 2000;   // characters kept of one block: a wall of text is graded on its first two thousand

function owner(node) {
  for (let el = node.parentElement; el; el = el.parentElement) {
    if (!/^(inline|contents)$/.test(getComputedStyle(el).display)) return el;
  }
  return null;
}

function kindOf(text) {
  const words = text.split(" ").length;
  return words >= 8 || (words >= 4 && /[.!?](\s|$)/.test(text)) ? "sentence" : "label";
}

function copyBlocks() {
  const parts = new Map();
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.textContent.trim() || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(node.parentElement.tagName)) continue;
    const el = owner(node);
    if (!el || node.parentElement.closest(NOT_COPY)) continue;
    if (!parts.has(el)) parts.set(el, []);
    parts.get(el).push(node.textContent);
  }
  const blocks = new Map();
  const add = (el, raw) => {
    const text = raw.replace(/\s+/g, " ").trim().slice(0, LONGEST);
    if (!/\p{L}/u.test(text) || VALUE.test(text) || blocks.has(text) || !visible(el)) return;
    blocks.set(text, { kind: kindOf(text), text, at: cssPath(el) });
  };
  for (const [el, texts] of parts) add(el, texts.join(""));
  for (const field of document.querySelectorAll("input[placeholder], textarea[placeholder]")) {
    if (!field.value && field.type !== "hidden") add(field, field.placeholder);
  }
  return [...blocks.values()];
}

if (typeof module !== "undefined") module.exports = copyBlocks;
