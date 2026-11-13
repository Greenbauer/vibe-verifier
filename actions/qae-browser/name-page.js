// The duplicate-name rule of the layout audit (ui-audit-page.js, whose header lists every rule): two buttons
// a person can press in one dialog, or one form, whose names are the same, or one the other with words added
// ("Create account" and "Create account now"). A subscribed app's add-someone dialog showed a "Send invite"
// choice beside a "Send invite" submit, both blue: the person it was built for could not tell them apart, and
// its own test told them apart by position (2026-11-04). Choices are held to a narrower test: a radio, checkbox or
// option is one value of a set, and its name is that value, so two choices of one group fail only when their names
// are the same ("Bill" twice), not when one adds words ("Bill" and "Bill Payment" are two record types a filter
// offers). A choice beside an action button is still judged as two buttons. ui-audit.js runs this file beside
// ui-audit-page.js as one script, so `visible` comes from there.
const PRESSABLE = "button, [role=button], input[type=submit], input[type=button]";
const SCOPE = "[role=dialog], [role=alertdialog], dialog, [aria-modal=true]";
const ROW = "li, tr, [role=row], [role=listitem]";
const CHOICE_ROLES = new Set(["radio", "checkbox", "option", "menuitemradio", "menuitemcheckbox"]);
const CHOICE_GROUP = "[role=radiogroup], [role=group], [role=listbox], [role=menu], fieldset";

// The name assistive technology and a person read: aria-labelledby, aria-label, then what it shows.
function buttonName(el) {
  const labelled = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(" ");
  const own = el.tagName === "INPUT" ? el.value : el.innerText;
  return (labelled || el.getAttribute("aria-label") || own || el.title || "").replace(/\s+/g, " ").trim();
}

const pressable = el => visible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true";

// The nearest row of a list or table inside `scope`: one row's Remove beside another row's Remove is a
// row's control, told apart by its row.
function rowOf(el, scope) {
  const row = el.parentElement && el.parentElement.closest(ROW);
  return row && scope.contains(row) ? row : null;
}

const isChoice = el => CHOICE_ROLES.has(el.getAttribute("role"));

// The set a choice belongs to inside `scope`: two choices of different sets are not offered side by side.
const choiceGroupOf = (el, scope) => {
  const group = el.closest(CHOICE_GROUP);
  return group && scope.contains(group) ? group : scope;
};

function alike(a, b) {
  const [short, long] = a.length <= b.length ? [a, b] : [b, a];
  return short && (short === long || long.startsWith(short + " "));
}

function nameRules(add) {
  const scopes = new Map();
  for (const el of document.querySelectorAll(PRESSABLE)) {
    if (!pressable(el) || (el.parentElement && el.parentElement.closest(PRESSABLE))) continue;
    const scope = el.closest(SCOPE) || el.closest("form");
    if (scope) scopes.set(scope, [...(scopes.get(scope) || []), el]);
  }
  for (const [scope, buttons] of scopes) {
    const named = buttons.map(el => [el, buttonName(el).toLowerCase()]);
    named.forEach(([a, na], i) => named.slice(i + 1).forEach(([b, nb]) => {
      const where = scope.matches(SCOPE) ? "dialog" : "form";
      const ra = rowOf(a, scope), rb = rowOf(b, scope);
      if (ra && rb && ra !== rb) return;
      if (isChoice(a) && isChoice(b)) {
        if (!na || na !== nb || choiceGroupOf(a, scope) !== choiceGroupOf(b, scope)) return;
        add("duplicate-name", a, `"${buttonName(a)}" twice among one ${where}'s choices: a person cannot tell which choice is which`, b);
        return;
      }
      if (!alike(na, nb)) return;
      add("duplicate-name", a, `"${buttonName(a)}" and "${buttonName(b)}" in one ${where}: a person cannot tell which button does what`, b);
    }));
  }
}

if (typeof module !== "undefined") module.exports = nameRules;
