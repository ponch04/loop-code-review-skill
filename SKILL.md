---
name: loop-code-review
description: Iterative review-and-fix loop over the current task's git changes using fresh, context-isolated reviewer sub-agents, with a script-enforced exit condition (green validation + zero unresolved findings). Use whenever the user asks for a looped or repeated code review, "review until clean", "keep fixing until a reviewer signs off", independent/sub-agent review of their changes, or invokes /loop-code-review — even if they don't say "loop".
---

# Loop Code Review

Review the current task's changes through a **fresh reviewer sub-agent** that has none of your conversation history, fix what it finds, validate, and repeat until the exit condition holds. The reviewer's independence is the whole point: an agent that reviews its own reasoning mostly re-confirms it.

The loop state lives in `scripts/loop_review.py`, not in your memory. It records scope, pass count, validation results and findings, and refuses `accept` until the exit condition is met. Use it for every step; don't keep a parallel count in your head.

## Exit condition (script-enforced)

The loop is **accepted** only when all of these hold:

1. Last validation run for the touched surface is green.
2. The latest full-review pass reports zero unresolved actionable findings.
3. The reviewer produced a credible understanding summary of the change (recorded as `--understood`).
4. Test evidence for changed behaviour is trustworthy, or its absence was concretely justified.

The numeric score (1–10) is a **progress signal only**. It never triggers work, never blocks acceptance, and the reviewer must not create or keep a finding to justify it.

## Workflow

**0. Scope.** Decide which files/hunks belong to *this task* from the task history and your own edits — not from `git status` alone. If ownership is ambiguous, ask the user. Never stage, commit, stash, reset or push unless asked.

```bash
python3 scripts/loop_review.py init --max-passes 5 -- <task-owned paths...>
```

**1. Validate** the current state with the smallest meaningful checks for the touched surface (focused tests, typecheck, lint, build). Fix red validation before asking for a review.

```bash
python3 scripts/loop_review.py validate -- <command>      # repeat per command
```

A command that never ran (typo, missing tool) is still recorded and will block `pass-start`; retract that record with `validate-drop -- <command>`. A command that ran and failed is a result — fix it or re-run it.

**2. Open a review pass.** The script refuses if the pass limit is reached, if validation is red, if a check the previous pass relied on has not been re-run on the current state, or if the scoped diff is unchanged since the last pass (a second opinion on identical code is not a new pass — clarify with the same reviewer instead).

```bash
python3 scripts/loop_review.py pass-start
```

Then spawn a reviewer with the platform's fresh-context mechanism (Claude Code: `Task`/sub-agent; Codex: a new agent — never a mode that inherits history). Give it **only** the prompt from `references/reviewer-prompt.md` filled with repo path, scope from `status`, and validation commands. Do not pass your rationale, suspected issues, or earlier reviewer output.

**3. Record the result** exactly as the reviewer returned it:

```bash
python3 scripts/loop_review.py pass-record --score 8.5 --findings 3 --understood \
    --test-evidence trusted|justified-absent|inadequate [--test-score 7]
```

`--test-evidence` is the reviewer's verdict on exit condition 4, not your own: `trusted` = the tests exercise the changed behaviour, `justified-absent` = the reviewer accepted a concrete reason for having none, `inadequate` = neither, which blocks acceptance until a later pass or an `amend` changes it.

**4. Triage findings** as review comments, not orders. Fix all concrete actionable ones as one coherent batch. Reject a finding only with repository/validation evidence, and ask the *same* reviewer to reconsider; if the disagreement stays material, spawn one fresh adjudicator for that single finding (read-only, evidence-based verdict). A finding stays unresolved until fixed, withdrawn, or adjudicated invalid:

```bash
python3 scripts/loop_review.py resolve --fixed 2 --withdrawn 1
```

If the reviewer omitted required output or hinted at concerns without concrete findings, ask it once for the missing part in the same conversation. Don't spawn a new full pass for that — record what it supplies against the same pass:

```bash
python3 scripts/loop_review.py amend --understood [--score 9.5] [--findings 2]
```

**5. Repeat** from step 1 after any fix batch touched scoped files — re-run the whole set of checks the last pass rested on, not just the quick one; a later pass may not be granted on weaker evidence than the pass before it. Stop looping for marginal polish — if what remains is preference, not risk, it isn't a finding.

**6. Finish.**

```bash
python3 scripts/loop_review.py accept     # exit 0 = accepted, exit 1 = prints what's blocking
python3 scripts/loop_review.py status --json
```

Reaching the pass limit without acceptance is an **incomplete outcome**; report the exact blocker, don't lower the bar. Continue past the limit only if the user explicitly asked for persistence until acceptance.

## Review dimensions

Reviewers apply these; you use them when triaging. Full detail in `references/review-dimensions.md` — read it if a finding is disputed or you're unsure whether something is actionable.

- **Comprehensibility / change safety** — can a future maintainer reconstruct responsibility, flow, invariants and failure behaviour from the code? A finding needs a specific symbol/flow and a concrete maintenance scenario it endangers.
- **Correctness & operational risk** — regressions, invalid assumptions, security/privacy exposure, data integrity, failure handling.
- **Test evidence** — tests exercise the changed behaviour, would fail on plausible regression, assert an observable contract, mock only at real boundaries. Passing ≠ good.
- **Reuse & local fit** — a finding must name an existing candidate and the practical benefit.
- **Architecture & conventions** — only deviations from an identifiable project rule or precedent.

## Final report

- What changed and why.
- Acceptance signal: findings resolved, understanding confirmed, test evidence status, validation green.
- Passes used; outcome (accepted / incomplete / interrupted).
- Latest score as progress signal (mark "pre-adjudication" if a later adjudication invalidated a finding).
- Validation commands + results; test quality score if tests changed.
- Findings intentionally not changed, with reason. Remaining risks.

`status --json` gives you most of this verbatim.
