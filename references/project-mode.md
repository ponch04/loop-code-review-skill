# Project mode — the rules for a queue of area loops

Read this before running a whole-project review. SKILL.md carries the command sequence; the
decisions live here, because they are needed only once a project review is actually running.

## P0. Map the areas

Derive them from the repository structure — packages, apps, services, top-level modules —
never from `git status`. Aim for areas a single reviewer can actually read (a few thousand
lines); split big packages by subdirectory, and order them so shared libraries come before
their consumers. Show the list to the user once, then run without further questions.

Neither `--task-brief` nor `--scope-note` applies here, and both commands refuse them: an
inherited area has no task to satisfy and no task-owned hunks — every file under its paths is
in scope, which is what makes it an area. The area prompt has no field for either.

**Never include `.loop-review/`.** It holds this loop's state and earlier reviewers' findings,
so an area containing it hands the next "fresh" reviewer the output of the last one — the one
thing reviewer isolation exists to prevent.

An area is one **or several** paths one reviewer reads as a whole: a standalone `+` glues
neighbours into one area — `-- packages/domain apps/portal/src/lib + apps/portal/src/lib-tests`.
Use that to split a flat package by file groups without tearing code away from its tests, or to
group siblings by meaning.

An area matching no file is refused at `project init`: fix the path rather than passing
`--allow-empty`.

## P1. Fix mode or `--report-only`

`--report-only` collects findings without fixing. Use it by default for a first full review, or
whenever a fix in one area could ripple into another. Without it the loop fixes each area as in
task mode, and validation after each batch must cover the whole project, not just the area.

In `--report-only` an area ends after step 3: record the pass, write the findings file,
`project close`. Fix nothing and do not run steps 5–6 — the fingerprint never moves, so a second
`pass-start` is refused; `accept` is simply not the gate here, since the ledger records
`reviewed` either way and running it only invites a contradictory report. `amend` stays
available: it completes the reviewer's own output, it does not resolve anything. If the area's
checks were already red, the pass still opens — inherited red is a property of the area you
inherited, not evidence about a change — and the findings file must say so. When an area has no executable check at all,
record that with `validate --none --reason "..."` and pass the reason to the reviewer verbatim;
never stand in a green `true`.

In fix mode the area runs all six steps and closes on `accept`. Inherited red opens the pass
there too, but `accept` still refuses on it: fix it as part of the area, or close the area
`incomplete` with it as the named blocker. Never force the gate.

## Closing

`project close` refuses in **both** modes while the findings file is missing. The path repeats
on every review of an area, so `project next` moves any earlier review aside to
`<slug>.prev.md` before the loop opens: whatever is at the live path was written during *this*
review, and an identical verdict on a second clean run is fine. Write this reviewer's whole
output there. Closing deletes the loop state, so that file is all that survives, and a clean
area needs it as much as a noisy one.

Never `reset` inside an area: `project close` is what records its outcome and frees the loop.
If the open loop turns out to belong to something else — another scope entirely — `project
close --force` settles this area `incomplete` without touching that loop, and `reset` then
frees it.

If a fix lands outside the area — the reviewer named the cause in a neighbouring file — widen
the loop with `scope --` exactly as in task mode, so the next pass covers it. Say so in the
report: the ledger records the widened paths as `reviewed_paths`, and if the file belongs to an
area already closed, that area's outcome no longer describes what is on disk, so name it as a
residual risk. In `--report-only` nothing is fixed, so this cannot arise.

Everything else settles the area instead of blocking it: no recorded pass or no credible
understanding closes it `incomplete` with its blocker, and an area that exhausted its passes
closes on whatever it has. An area whose paths no longer match any file is recorded `incomplete`
by `project next` itself, which exits 2 — run `project next` again for the one behind it. **Never stop the
project on one stuck area.**

## P2. Resume

`project status` shows where it stopped. If an area is still `in_progress`, `project next`
refuses until you settle it: finish it normally, or — when its loop state was lost with it —
`project close --force` records it `incomplete` and frees the queue. That `--force` is the one
the workflow expects you to run on your own; every other one needs the user.

When every area is closed and reported, `reset --project` clears the ledger. The findings
files under `.loop-review/findings/` are the deliverable and stay.
