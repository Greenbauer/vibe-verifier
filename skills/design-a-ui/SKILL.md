---
name: design-a-ui
description: Plan, build and review a screen so the person using it can do their task without getting lost, re-entering what the app already knows, or guessing what a control does. Use before building or changing any page, form, dialog, list or flow, when writing the plan for one, and when reviewing a pull request that changes one.
---

# Design a UI people can use

A screen can type-check, pass its tests, match the design system and still confuse the person using it.
The design system and layout checks cover how a screen looks. These rules cover how it works: where
the feature lives, how many steps the task takes, what each control does, and what each state says.

A repository's own rules come first. Its design system decides components, tokens and styling, and a
legacy-parity rule decides layout and behavior wherever it applies. Use these rules for everything
those leave open.

## Before building: write the UI brief

Put five lines in the plan or the pull request body before writing code:

- **User and role.** Who does this task, and which other roles will see the screen.
- **Task.** What they are trying to get done, in their words, not the data model's.
- **Main action.** The one control that finishes the task.
- **Where it lives.** The page where this kind of work is already done, and how the user gets there:
  the navigation entry or the record they will be on.
- **Closest existing screen.** A screen in this app doing the same kind of job, whose pattern this one
  copies.

If "where it lives" names a new page, a home-page box, or a second list for records that already have
one, stop and ask whether the existing page should hold the feature instead. That is the most common
reason a finished feature gets moved.

## The rules

Grade a rule only when the change touches what it covers. A finding needs a `file:line` or the text on
screen, and one sentence on how it would confuse or slow the user.

**Placement**

- **R17. It lives where the job is already done, and the user can reach it.** It goes on the page or
  flow for that task, not a new page, a home-page box, or a separate list or detail page for records
  that already have one. It is linked from the navigation or from the record the user is on. Create,
  edit and cancel return the user to where they started. Every screen and dialog has a way back.

**Task and structure**

- **R1. One main action per screen or step.** The action that finishes the task is the most visible
  control. Secondary actions are quieter. Two controls with the same label are a defect.
- **R2. Follow the user's existing workflow and words.** Steps, order and labels follow how the job is
  done today, in the system people already use, not how the data is stored.
- **R3. Fewest steps.** No extra page, dialog, confirm or click unless it protects the user from losing
  work or making a change they cannot undo.
- **R4. Reuse the app's pattern for the same job.** A list, filter, edit form, detail page or picker
  works like its siblings elsewhere in the app.
- **R5. Show what matters, hide the rest until needed.** The fact the user came for stands out. Rare
  options and details sit behind a disclosure.
- **R6. Look things up without leaving.** Checking a detail does not drop the user's place, an unsaved
  form, or a filtered list.

**State and feedback**

- **R7. Every view has its states.** Loading, empty, error, partial and no-permission, each saying what
  happened and what to do next. A failed load never looks like an empty list.
- **R8. Show what the system is doing.** After an action, the user sees it worked or failed. A long job
  shows progress.
- **R9. Errors say how to fix it**, next to where it happened, in plain words. A locked or disabled field
  says why and where to change it.
- **R10. Safe to try.** Typed input survives an error, a navigation and a refresh. Confirm or undo only
  for changes that cannot be taken back. Nothing acts on a value the user typed but did not save.

**Controls and content**

- **R11. Controls say what they do.** A button is a verb naming the result ("Send quote", not "Submit"
  or a bare noun). No unlabeled icon-only control.
- **R12. No system words on screen.** No enum values, internal codes, ticket numbers, table names or
  field keys.
- **R13. One name, one place, one look** for the same thing, across the change and the app. Two counts
  of the same thing on one screen agree.
- **R14. Show, don't make them remember.** Options are shown, not typed from memory. Selections and
  filters stay visible. Context carries between steps: "New" from a record's row starts with that record.
- **R15. Forms.** A label on every field, defaults and prefills from what the app already knows, a picker
  instead of free text where the options are a fixed list, the expected format shown, paste allowed.
- **R16. Each role sees only what it can use.** A read-only user sees no write controls. Nothing is
  shown that the user's role will then be refused.

## After building: walk it as the user

On the preview, as each role in the brief:

1. Start where the user starts (the navigation or the record), not at a typed URL.
2. Do the task with only what the pages show.
3. Get back to the start page the same way. A typed URL or the browser's Back button means the way back
   is missing.
4. Check each state from R7, and each role's controls from R16.

Anything that made you stop, guess or retry is a finding. Fix it in this pull request when it is on the
screens this pull request adds or changes, and list the rest in the body.

## Where these rules come from

- [NN/g, complex application design](https://www.nngroup.com/articles/complex-application-design/):
  R5, R6, R10.
- [Nielsen's ten usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/):
  R2, R8, R9, R12 to R14.
- [Vercel Web Interface Guidelines](https://vercel.com/design/guidelines): R9, R11, R15.
- R1, R3, R4, R7, R16 and R17 are this catalog's own, written from agent-built screens that confused
  people, and tested by the backtest below.
- Backtest, 2026-11-04: R1 to R16 were graded blind, from code, against 41 past UI pull requests in two
  private repositories, 10 of which people had found confusing. They flagged every clearly confusing one
  but named the actual complaint in only one of four, and flagged 9 of 31 that drew no complaint (several
  looked like real problems nobody had reported). R17 was added after: it flagged three of the four
  clearly confusing pull requests, all placement complaints, and flagged 1 of 31.

Building a new site or app with no design system yet? A visual-direction skill such as Anthropic's
`frontend-design` fits there. Inside an existing app, it conflicts with the app's tokens and components;
use this skill and the repository's design system instead.
