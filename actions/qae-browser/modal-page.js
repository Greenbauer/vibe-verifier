// The page side of the modal-dismiss rule (modal-dismiss.js judges it; ui-audit-page.js's header lists every rule): the
// modals open now, a point on a modal's backdrop a click may press, whether a person has edited a modal's fields, and
// the control a person pressed, named so a window of the hook's own can find it again. modal-dismiss.js runs it beside
// ui-audit-page.js and name-page.js as one script, so `visible`, `cssPath` and `buttonName` come from there.
//
// A modal is a role=dialog or alertdialog with aria-modal="true", a <dialog> opened with showModal(), or a role=dialog or
// alertdialog over a backdrop: a fixed box as large as the window, laid over the page, holding nothing to read or
// press outside the dialog. An aria-modal dialog laid out in the page's flow (a sign-in card marked as one) is a
// page, with nothing behind it to return to, and is not a modal.
const MODAL = "dialog, [role=dialog], [role=alertdialog]";
const OPENER = "button, a[href], [role=button], [role=link], [role=menuitem], [role=tab], summary, input[type=button]";
// What a person presses: a backdrop holds none outside its dialog.
const ACTIVE = "a[href], button, input, select, textarea, label, summary, [role=button], [role=link], [role=menuitem], [role=tab], [role=checkbox], [role=switch], [role=option], [contenteditable=true]";
const BACKDROP_GAP = 24;   // px a backdrop click lands outside the dialog's box, so it cannot graze its edge or shadow

const coversWindow = r => r.left <= 1 && r.top <= 1 && r.right >= innerWidth - 1 && r.bottom >= innerHeight - 1;
const ownText = el => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());

// The box a person sees as the dialog: the dialog itself, or, when its role sits on a wrapper as large as the window (a
// backdrop holding its card) or on a root of no size (a portal's, whose backdrop and card are fixed inside it), the
// nearest box holding everything it shows. null when it shows nothing.
function dialogBox(dialog) {
  if (visible(dialog) && !coversWindow(dialog.getBoundingClientRect())) return dialog;
  const shown = [...dialog.querySelectorAll("*")].filter(el => visible(el) && (el.matches(ACTIVE) || ownText(el)));
  if (!shown.length) return null;
  let box = shown[0];
  while (box !== dialog && !shown.every(el => box.contains(el))) box = box.parentElement;
  return box === dialog || coversWindow(box.getBoundingClientRect()) ? null : box;
}

// Whether `layer` holds nothing a person reads or presses outside `box`: a backdrop, not an app laid out full-window.
function holdsNothing(layer, box) {
  const skipBox = { acceptNode: n => (box.contains(n) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) };
  const walker = document.createTreeWalker(layer, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, skipBox);
  for (let n = walker.currentNode; n; n = walker.nextNode()) {
    const el = n.nodeType === 3 ? n.parentElement : n;
    if ((n.nodeType === 3 ? n.textContent.trim() : n.matches(ACTIVE)) && visible(el)) return false;
  }
  return true;
}

// The backdrop `hit` is part of at (x, y): its nearest fixed box as large as the window, holding nothing outside `box`,
// over a box of the page (a child or grandchild of the body, neither around it nor in it) at that point: a fixed
// background behind an empty page is not a backdrop. Geometry, not hit-testing: a modal may set pointer-events: none on
// the page under it. null when there is none.
function overlayOf(hit, box, x, y) {
  let layer = hit;
  while (layer && !(getComputedStyle(layer).position === "fixed" && coversWindow(layer.getBoundingClientRect()))) layer = layer.parentElement;
  if (!layer || !holdsNothing(layer, box)) return null;
  const under = el => {
    const r = el.getBoundingClientRect();
    return !el.contains(layer) && !layer.contains(el) && visible(el) && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  };
  return [...document.querySelectorAll("body > *, body > * > *")].some(under) ? layer : null;
}

// Whether a click at (x, y) lands on the dialog's backdrop: a native modal's ::backdrop (which hits as the dialog
// itself), or an overlay, which holds no control a click could press.
function backdropAt(dialog, box, x, y) {
  const hit = document.elementFromPoint(x, y);
  if (dialog.matches(":modal")) return hit === dialog;   // the point is outside its box, so on its ::backdrop
  return Boolean(hit) && !box.contains(hit) && Boolean(overlayOf(hit, box, x, y));
}

// A point at least BACKDROP_GAP outside the dialog's box that lands on its backdrop, or null: halfway from each side and
// corner of the box to the window's edge, so always inside the window.
function backdropPoint(dialog, box) {
  const r = box.getBoundingClientRect();
  for (const y of [r.top / 2, (r.top + r.bottom) / 2, (r.bottom + innerHeight) / 2]) {
    for (const x of [r.left / 2, (r.left + r.right) / 2, (r.right + innerWidth) / 2]) {
      const outside = Math.max(r.left - x, x - r.right, r.top - y, y - r.bottom);
      if (outside >= BACKDROP_GAP && backdropAt(dialog, box, x, y)) {
        return { x: Math.round(x), y: Math.round(y) };
      }
    }
  }
  return null;
}

// Whether any field of the dialog holds a value other than its default: a person's edits, which closing may lose. A
// select's default is the option its markup marks, else its first; a box's is whether its markup ticks it.
function editedIn(dialog) {
  return [...dialog.querySelectorAll("input, textarea, select")].some(field =>
    field.tagName === "SELECT" ? field.selectedIndex !== Math.max(0, [...field.options].map(o => o.defaultSelected).lastIndexOf(true))
      : /^(checkbox|radio)$/.test(field.type) ? field.checked !== field.defaultChecked
        : field.value !== field.defaultValue);
}

// The name a person reads for the dialog: its aria-labelledby or aria-label, else its first heading, else its text.
function dialogName(dialog) {
  const heading = [...dialog.querySelectorAll("h1, h2, h3, h4, h5, h6, [role=heading]")].find(visible);
  const named = dialog.getAttribute("aria-labelledby") || dialog.getAttribute("aria-label");
  const text = named ? buttonName(dialog) : heading ? heading.innerText : (dialogBox(dialog) || dialog).innerText;
  return (text || "").replace(/\s+/g, " ").trim().slice(0, 80);
}

// Whether the dialog's box is laid over the page: a native modal, or the box or a box around it fixed or absolutely
// placed, not in the page's flow.
function overPage(dialog, box) {
  if (dialog.matches(":modal")) return true;
  for (let n = box; n; n = n.parentElement) if (/^(fixed|absolute)$/.test(getComputedStyle(n).position)) return true;
  return false;
}

// Every modal open now, in page order (the last is the one opened last): {at, name, alert, edited, point}, `point` a
// point of its backdrop a click may press, or null.
function openModals() {
  const found = [];
  for (const dialog of document.querySelectorAll(MODAL)) {
    const box = dialogBox(dialog), point = box && backdropPoint(dialog, box);
    if (box && overPage(dialog, box) && (dialog.matches(":modal") || dialog.getAttribute("aria-modal") === "true" || point)) {
      found.push({ at: cssPath(dialog), name: dialogName(dialog), alert: dialog.getAttribute("role") === "alertdialog", edited: editedIn(dialog), point });
    }
  }
  return found;
}

// A submit button: pressing it submits its form, which a window of the hook's own never does.
const submits = el => el.matches("input[type=submit], input[type=image]") || (el.tagName === "BUTTON" && el.type === "submit" && Boolean(el.form));

// The control a press on `target` pressed, as {tag, name, at}, or null: a press inside a dialog, on the page itself or
// on a submit button opens nothing a window of the hook's own may press again.
function openerOf(target) {
  const el = target instanceof Element ? target.closest(OPENER) || target : null;
  if (!el || el === document.body || el === document.documentElement || el.closest(MODAL) || submits(el)) return null;
  return { tag: el.tagName.toLowerCase(), name: buttonName(el).slice(0, 80), at: cssPath(el) };
}

// The one visible control that `opener` names: by its tag and name, else, among several, by its CSS path. null when
// none or more than one matches, or it would submit a form: never a guess.
function findOpener(opener) {
  const named = [...document.querySelectorAll(opener.tag)]
    .filter(el => visible(el) && !submits(el) && buttonName(el).slice(0, 80) === opener.name);
  const at = named.length === 1 ? named : named.filter(el => cssPath(el) === opener.at);
  return at.length === 1 ? at[0] : null;
}

// In the explorer's tab, from the document's start: notes the control the last click pressed (a click by keyboard too)
// and, when a dialog appears (added, opened with showModal(), or made aria-modal), hands __vibeVerifierModal that
// control, or null. It only listens.
function watchOpeners() {
  let pressed = null;
  addEventListener("click", event => { pressed = openerOf(event.target); }, true);
  const dialogIn = node => node.nodeType === 1 && (node.matches(MODAL) || Boolean(node.querySelector(MODAL)));
  new MutationObserver(changes => {
    if (changes.some(change => change.type === "attributes" || [...change.addedNodes].some(dialogIn))) window.__vibeVerifierModal(pressed);
  }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["open", "aria-modal"] });
}

if (typeof module !== "undefined") module.exports = openModals;
