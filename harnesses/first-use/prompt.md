You are using this website for the first time. Nobody has shown you how it works, and you have not read its
code or the pull request that changed it. The site is running at SITE_URL.
If qae-inputs/site.md exists, read it before anything else: this repository's workflow wrote it, and it says how
to sign in, which logins there are and what state the site starts in. Read no other file. All you know is the
task below and what the pages show you.

The task, in the words of the person who asked for it:

TASK

1. Open SITE_URL and sign in as qae-inputs/site.md says, as the login whose role best fits the person the task
   describes (the first login it lists when none does). The page you are on once you are signed in is where you
   start. Save a screenshot to qae-first-use/step-1.png, then write the first line of qae-first-use/walk.md:
     - step 1: start: <the page you are on> -> <what it shows>
2. Do the task as a person who has never seen this site would: read the page, choose what looks like the way
   forward, type into the fields it shows. Use the playwright browser tools. A step is one action that changes
   what is on screen. For every step, first save a screenshot to qae-first-use/step-k.png, then append one line
   to qae-first-use/walk.md of the form
     - step k: <what you did> -> <what you saw>
   Never write a step line without its screenshot. From step 1 on, never type an address into the browser, never
   use the browser's Back or Forward, never open, switch or close a tab, and never run a script in the page: a
   person uses what the page shows, and a gate fails the walk on any of those calls. Where the task needs a value
   (an email address, a name), use the kind a real person doing this task would type, never an address a test
   environment would accept and that person would not use. Where a page answers with an error, read what it says as
   that person would: does it say what went wrong and what to do? A test copy of a site may refuse a value a real
   one takes (an outside email address, a real card). When the page says so and says what it does take, try once
   more with a value it says it takes: that is not a snag. When it refuses without saying why, that is a snag.
3. When the task is done, or you cannot go any further, go back to the page you started on the way you came:
   page by page, using what each page itself shows (a breadcrumb, a back link, a Cancel or Close button), not
   the site's main menu, the browser's Back or a typed address. A page the main menu links to is one a person
   reaches and leaves by that menu, so going to or from it by the menu is fine. Where a page the menu does not
   link to (a record opened from a list, a step of a form) has no way back to the page before it, use the menu
   and note it as a snag (step 4). Log those steps the same way. The last step's line names it:
     - step k: back: <what you did> -> <the page you are on>
4. Write qae-first-use/verdict.md with these lines and nothing else:
     first-use: DONE -- <what you got done> (walk.md::<the step line that showed it, exactly as written after "- ">)
   or, when you could not finish the task:
     first-use: STUCK -- <where you stopped and why> (walk.md::<the step line where you stopped>)
   then
     first-use-back: DONE -- <how you got back> (walk.md::<the back step's line>)
   or, when you could not get back to where you started:
     first-use-back: STUCK -- <why not> (walk.md::<your last step's line>)
   then one line for each thing that would trip a person doing this task for the first time, and none when
   nothing did:
     first-use-snag: <what tripped you> (walk.md::<the step line where it happened>)
   A snag is a step you could not see how to take; a control that did something other than its label said; two
   controls you could not tell apart; an error that did not say what went wrong or what to do; a page with no way
   back to the page before it but the main menu, when the menu does not link to that page itself; a second place
   for one job (a new list or page for records the site already lists elsewhere, or the task's control put on a
   page other than the one where that kind of work is already done); words only the site's builders would know. A
   wish for a feature the task did not ask for is not a snag.
   A person reads these sentences in the pull request's QA review: plain words, with no em dash, URL, file path,
   commit hash or @-mention in them. The text after :: starts with "step k:" and must be copied verbatim from the
   step line, because a gate checks that it is there.
5. Do not post anything. Write only under qae-first-use/. Do not change any other file.
