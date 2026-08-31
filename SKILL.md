---
name: loop-code-review
description: Iterative review-and-fix loop using fresh, context-isolated reviewer sub-agents, with a script-enforced exit condition (green validation + zero unresolved findings). Two scopes — the current task's changes, or a whole project reviewed area by area as a queue of loops. Use whenever the user asks for a looped or repeated code review, "review until clean", "keep fixing until a reviewer signs off", an independent/sub-agent review of their changes, a full review of the project or codebase, or invokes /loop-code-review — even if they don't say "loop".
---

# Loop Code Review

Review the current task's changes through a **fresh reviewer sub-agent** with none of your conversation history, fix what it finds, validate, repeat until the exit condition holds. Independence is the point: an agent reviewing its own reasoning mostly re-confirms it.

`scripts/loop_review.py` holds the loop state, not your memory: scope, brief, passes, validation and findings live in `.loop-review/` inside the reviewed repository, and `accept` refuses until the exit condition holds. Use it at every step. Run it by its path **inside this skill's directory** — `scripts/…` relative to the reviewed repository may not exist, or may be a different copy. If an earlier loop is still active the first command refuses: finish it, or `reset` it once the user agrees it is dead (`--project` discards an abandoned ledger too; inside a live project area `reset` refuses — `project close` frees it). Mention `.loop-review/` in the report so the user can gitignore or remove it.

## Exit condition (script-enforced)

The loop is **accepted** only when all of these hold:

1. Last validation for the touched surface is green.
2. The latest pass is *of the current state* and reports zero unresolved findings — a review the scope has moved past is stale, not a pass.
3. The reviewer produced a credible understanding summary (`--understood`).
4. Test evidence is trustworthy, or its absence concretely justified.

The score (1–10) is a **progress signal only**: it never triggers work, never blocks acceptance, and no finding may be created or kept to justify it.

## Workflow

**0. Scope and brief.** Decide the scope: "my changes / this task / this PR" → task mode below; "the project / the codebase / everything / module X" → **project mode** at the end, the same loop once per area.

Task mode: decide which files and hunks belong to *this task* from the task history and your own edits, not from `git status` alone. If ownership is ambiguous, ask the user. Never stage, commit, stash, reset or push unless asked.

Record the requirements and acceptance criteria with `--task-brief`: without them a reviewer can only check that the implementation is self-consistent, never that a requirement was missed. Requirements are facts about the task; your rationale, suspicions and proposed fixes are not, and stay out. Hunk-level ownership is not a requirement either — record it with `--scope-note`, which `status` and `pass-start` echo into the prompt's scope-notes field.

`LR` below is `python3 <this skill's directory>/scripts/loop_review.py`.

```bash
LR init --max-passes 5 --task-brief "<requirements and acceptance criteria>" [--scope-note "<hunk ownership>"] -- <task-owned paths...>
LR init --task-brief-file <path> --base <ref> -- <paths...>  # committed work: the parent of its first commit
                                                             # (pinned to a commit id, so a later commit cannot narrow the scope)
LR validate -- <command>                                     # step 1, per command; exits with its status
LR validate --none --reason "<why nothing runs here>"        # step 1, when no check applies
LR pass-start                                                # step 2
LR pass-record --findings 3 [--score 8.5] [--understood] \
   [--test-evidence trusted|justified-absent|inadequate] [--test-score 7]   # step 3
LR resolve --fixed 2 --withdrawn 1                           # step 4
LR amend --understood [--score 9.5] [--findings 2]           # step 4, reviewer completing its own output
LR pass-abort --reason "<why>"                               # step 3, a review you will not record
LR scope -- <paths...>                                       # step 5, a fix that landed outside
LR accept                                                    # step 6; exit 0 accepted, exit 1 prints blockers
LR status --json                                             # the final report, verbatim
```

If either surfaces later, record it with `LR brief --task-brief "..."` / `--scope-note "..."`; it refuses to overwrite one already recorded, because terms that change between passes silently redefine what "satisfies the task" means. Never re-run `init` for this: it rebuilds the loop and destroys the pass history.

`init` exits 2 on an empty scope — no diff in `changes` mode, no matching file in `project` mode. Re-run with `--base` if the work is committed, fix wrong paths, or report **no-changes** and stop. `--allow-empty` overrides it and leaves every gate blind; never reach for it on your own initiative.

**1. Validate** the current state with the smallest meaningful checks for the touched surface (focused tests, typecheck, lint, build). When it genuinely has none — prose, config, fixtures — record `validate --none --reason "..."` and give the reviewer that reason verbatim. Never stand in a green `true`: it satisfies condition 1 vacuously *and* reaches the reviewer as a check you claim to have run.

*In task mode:* fix red validation before asking for a review; narrow a check that is also red before your change to the delta rather than recording a repository-wide invariant. *In project mode:* the pass opens over a failing check, since an inherited area is reviewed as it stands — `status` marks each `[inherited]` or `[regressed]`, and you pass that marker on. `accept` refuses on red in both. `references/project-mode.md` has the rest.

A command that never ran (typo, missing tool) blocks `pass-start` in both modes — evidence about nothing, not an inherited failure: fix and re-run it, or retract it with `LR validate-drop -- <command>` (`--force` once a pass rested on it); `--none` withdraws a mistaken `validate --none`. One that *edited* the scope counts as evidence for neither state: re-run once the files settle.

**2. Open a review pass.** The script refuses if a pass is open, the limit is reached, validation is red *in task mode*, a check the previous pass rested on was not re-run, or the scope is unchanged since the last *recorded* pass — a second opinion on identical code is not a new pass — clarify with the same reviewer. An aborted pass never counted as recorded, so replacing an unusable reviewer needs no `--force`.

Then spawn a reviewer with the platform's fresh-context mechanism (Claude Code: `Task`/sub-agent; Codex: a new agent — never one that inherits history). Give it **only** the prompt from `references/reviewer-prompt.md`, filled from `status`: repo path, scope, base, brief, scope note and validation lines. The base matters: for the documented case — work already committed — `git diff HEAD` is empty, so a reviewer not told what to compare against sees no change at all. Do not pass your rationale, suspected issues, or earlier reviewer output.

**3. Record the result** exactly as the reviewer returned it — but decide first whether it is usable. A review of a state the files have moved past, or one still unusable after a single clarification, is not recorded: `pass-abort --reason ...` discards the open pass, and the next `pass-start` needs no `--force` because an aborted pass never counted as recorded. It does consume one of the limit. Once you record a review, that judgement is spent.

Only `--findings` is required: record what the reviewer returned, and if it omitted the score or the verdict, ask once and `amend` — never supply either yourself. `--understood` and `--test-evidence` are its verdicts, and each clears an exit condition on its own; omit `--understood` when the summary is not credible.

`--test-evidence` is its verdict on condition 4: `trusted` = the tests exercise the changed behaviour, `justified-absent` = it accepted a concrete reason for having none, `inadequate` = neither, which blocks acceptance until a later pass or `amend` changes it.

**4. Triage findings** as review comments, not orders. Fix the concrete actionable ones as one batch. Reject one only with repository or validation evidence, asking the *same* reviewer to reconsider; if the disagreement stays material, spawn one fresh adjudicator for that finding alone. A finding stays unresolved until fixed, withdrawn or adjudicated invalid — an unusable reviewer or adjudicator never resolves one. Ask each once for what it left out; if output stays unusable, stop **incomplete** with that finding as the blocker.

If the reviewer omitted required output, or hinted at concerns without concrete findings, ask once in the same conversation and record what it supplies with `amend`, not a new pass.

`pass-abort` (step 3) is the exit for both unusable cases. Never record an invented result, and never `reset`, which throws away the whole loop.

**5. Repeat** from step 1 after any fix batch, re-running the whole set of checks the last pass rested on, not just the quick one: a later pass is not granted on weaker evidence than the one before it. `accept` refuses a recorded fix that moved nothing, so a fix that landed *outside* the scope needs `LR scope -- <paths>`: widening moves the fingerprint and makes the next pass mandatory. Widening only: no path can be dropped, so if one turns out not to be task-owned, say so in the report and leave it. Replacing a recorded brief or scope note is the same kind of call (`brief --force`), and the report must say so. Stop looping for marginal polish: if what remains is preference, not risk, it isn't a finding.

**6. Finish** with `accept`, then `status --json`, and deliver the report. In task mode `reset` the finished loop: the ban on `reset` covers a loop in progress, not one you have just reported. **In project mode never `reset` here** — `project close` records the area's outcome and frees its loop, and resetting discards an area that passed every gate. `reset --project` comes once, after every area is closed and reported; the findings files stay. Reaching the pass limit without acceptance is an **incomplete outcome** — report the exact blocker, don't lower the bar; go past the limit only if the user explicitly asked for persistence.

## Project mode — the whole codebase, one area at a time

A project is too large for one reviewer: "review everything" means a **queue of area loops**, each running the workflow above with its own pass limit, until every area has an outcome. Read `references/project-mode.md` first — mapping areas, `--report-only`, closing, resuming.

```bash
LR project init --max-passes 5 [--report-only] -- <areas in order>
LR project next     # opens the next pending area; prints its findings-file path
# then the workflow above with references/reviewer-prompt-area.md instead of reviewer-prompt.md:
#   steps 1–3 in --report-only (nothing is fixed), all six in fix mode (the area closes on accept),
# writing the reviewer's WHOLE output verbatim to that file — `project close` deletes the
# loop state, so that file is all that survives of the review
LR project close    # records accepted / reviewed / incomplete, frees the loop
LR project status [--json]
```

Report per area when nothing is pending: outcome, passes, findings (resolved/total), score, test-evidence verdict, blocker, findings-file path — `project status --json` has it all.

## Review dimensions

Reviewers apply these; you use them when triaging. `references/review-dimensions.md` has the detail — read it when a finding is disputed or its actionability is unclear.

**Requirement conformance** (silent with no brief; never invent one) · **comprehensibility and change safety** (a specific symbol or flow, and the maintenance scenario it endangers) · **correctness and operational risk** (regressions, invalid assumptions, security, data integrity, failure handling) · **test evidence** (passing ≠ good) · **reuse and local fit** (name the candidate and the benefit) · **architecture and conventions** (only against an identifiable rule or precedent).

## Final report

- What changed and why; findings resolved, understanding confirmed, test-evidence verdict, validation green.
- Passes used; outcome — `accepted`, `no-changes`, `incomplete` (name the blocker), `reviewed` (a report-only area), `interrupted`.
- Latest score as a progress signal (mark "pre-adjudication" if adjudication later invalidated a finding).
- Validation commands and results; test quality score if tests changed.
- Findings intentionally not changed, with reason. Remaining risks.

`status --json` gives most of this verbatim.
