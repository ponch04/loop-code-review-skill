# AGENTS.md — loop-code-review

Project passport for agents working on this repository without a human in the loop.
Decisions and constraints only; the code is the description of itself.

## What this is

An **Agent Skill package**, not an application. The deliverable is a directory that is
copied into `~/.claude/skills/` (Claude Code), `~/.codex/skills/` (Codex CLI) or any
Agent Skills–compatible runtime. Its consumer is a model, not a user.

It gives a coding agent one workflow: review the changes of the *current task* through a
**fresh, context-isolated reviewer sub-agent**, fix, re-validate, repeat, and stop only
when the exit condition holds.

Why it exists: the prompt-only version of this workflow failed the way long prompts always
fail — the model forgot pass counts, re-reviewed identical code, and accepted with red
validation. `scripts/loop_review.py` moves the gates out of the prompt into a state machine
that can say no. **The script's job is to refuse**, not to review.

The second load-bearing idea: a reviewer that shares the author's context mostly re-confirms
the author's reasoning. Independence is the product.

## Source of truth on conflict

1. `SKILL.md` + `references/*.md` — the **normative spec**. This is the contract the model
   executes at runtime; intent lives here.
2. `scripts/loop_review.py` — authoritative for **mechanics only**: exit codes, state schema,
   what is actually enforced. If the script contradicts SKILL.md on *policy*, the script is
   the bug. If SKILL.md claims a gate the script does not implement, that is also a script
   bug — SKILL.md must not promise enforcement that does not exist.
3. `evals/evals.json` — the behavioural spec of the *agent*, not of the code. Changing
   workflow behaviour without updating the affected eval is an incomplete change.
4. `README.md` — derived, human-facing, lowest priority. Never resolve a question from it;
   when it disagrees with anything above, README is the one that is wrong.

## Layout

```
SKILL.md                            workflow, both scopes
scripts/loop_review.py              state machine + project ledger
scripts/selftest.py                 regression check: asserts the refusals
references/reviewer-prompt.md       task-mode reviewer (a diff) + the adjudicator prompt
references/reviewer-prompt-area.md  project-mode reviewer (an inherited area)
references/project-mode.md          rules for the area queue, read when one is running
references/review-dimensions.md     what counts as an actionable finding
evals/evals.json                    behavioural spec for the agent
LICENSE                             MIT; keeps the upstream notice this skill derives from
.gitignore                          .loop-review/ — the skill is run on this repo too
```

## Build and test

No build, no package manager, no dependencies. Python 3.8+ stdlib and `git` only.

```sh
python3 scripts/selftest.py                    # the check that matters; exit 0 = green
python3 scripts/loop_review.py --help          # argument surface parses
```

`selftest.py` builds throwaway repositories and asserts the **refusals** — empty scope in
both modes, red validation, an unchanged fingerprint, shrinking evidence, a validation run
that edited what it validated, `validate-drop`'s never-ran-vs-ran distinction, out-of-range
arguments, `amend` lowering findings, an interrupted state write, report-only close without
a findings file. Every invocation the CLI *recommends* is parsed too — the prose in the docs
and the commands quoted in the script's own `die()`/`print()` messages alike: `project close`
sent the operator to a `--force` flag that `reset` has never had, and only the parser saw it. A
happy-path smoke is worthless here: one stayed green while three fixed defects were reverted. Any change to a gate adds or updates a case in the same commit, and
a new case must be shown to fail on the defect it covers before it is trusted.

Assert the blocker, not the exit code. A case that checks only `returncode != 0` is usually
satisfied by a *different* refusal, so it reads as coverage while the gate it names can be
deleted with the suite green — three of the four acceptance conditions were in that state at
once. Name the expected reason.

`evals/evals.json` is not a runnable suite — the prompts are executed against a real runtime
and judged against `expected_output`. There is no CI.

## Decisions that must not be broken

- **Zero dependencies, single file.** The script stays stdlib-only, Python 3.8+ compatible
  and self-contained. A skill that needs `pip install` will not be installed.
- **The script never touches git state or source files.** It reads git and writes only inside
  `.loop-review/` (`state.json`, `project.json`, their `.tmp` files, the `findings/`
  directory, the `<slug>.prev.md` rename, and its own removals). No add/commit/stash/push, no
  edits to source — in the script or in the workflow, unless the user asks. The one hole is
  `validate`, which runs a command the agent chose; a run that moves the scope is therefore
  recorded as evidence for neither state.
- **The script never spawns or talks to a model.** Reviewer output is transcribed into it by
  the orchestrating agent. That is what keeps the loop runtime-agnostic.
- **The score never gates acceptance.** 1–10 is a progress signal, `--score` and
  `--test-score` alike. Neither may appear in `blockers()`, and no finding may be created or
  kept to justify a number. Findings win.
- **Acceptance = the four conditions in SKILL.md**, all checked in `blockers()`. Do not add a
  fifth, and do not let one become advisory. Understanding and test evidence are recorded
  verdicts (`--understood`, `--test-evidence`) because the script cannot judge them itself.
- **Fingerprint identity.** A pass, a validation run and an acceptance are bound to a hash of
  the reviewed state: the scoped diff against `--base` plus untracked contents in `changes`
  mode, the contents of every file under the area in `project` mode. `--base` is pinned to a
  commit id at `init`, never kept symbolic. Validation from another fingerprint is not
  evidence; a review of an unchanged fingerprint is not a new pass; within one fingerprint
  the latest run of each command wins, and distinct commands are never conflated. Evidence
  may not shrink: a new pass carries every check the previous one rested on.
- **Reviewer context isolation is the product.** The reviewer receives the filled prompt and
  nothing else — no author rationale, no suspected issues, no prior reviewer output, and
  never `.loop-review/`. Exactly two things cross that boundary: the task brief (requirements
  and acceptance criteria) and the scope note (which hunks of a scoped file are task-owned).
  Both are recorded in `state.json` and echoed verbatim by `status`, so they cannot drift
  between passes. Facts about the task, never reasoning about the change. A third channel is
  a change to the product.
- **The agent never supplies a value the reviewer withheld.** `pass-record` requires only
  `--findings`; a missing verdict blocks acceptance, a missing score blocks nothing.
- **A review invalidated mid-flight is aborted, never invented.** `pass-abort` discards the
  open pass; it counts as used and can never satisfy acceptance. An unusable reviewer or
  adjudicator likewise never resolves a finding — silence is not a verdict, and an unmet
  condition is reported INCOMPLETE with its blocker.
- **An empty scope is an outcome, not a warning**, and it is checked on every gate rather
  than at `init` alone: a scope empties whenever work is committed, reverted or stashed, or an
  area is deleted, and from then on the loop passes every gate over nothing. `init` exits `2`
  (`no-changes`); a path git cannot see is warned about, because it contributes nothing to the
  fingerprint.
- **Scope widens, never narrows, and only through `scope --`.** A fix landing outside the
  recorded scope changes nothing the fingerprint covers; widening moves the fingerprint and
  keeps the pass history, which `init --force` would destroy. Narrowing retires evidence and
  stays a human decision. A recorded `--fixed` that left the fingerprint on the reviewed state
  blocks acceptance either way: the script cannot tell "fixed next door" from "not fixed at
  all" — the honest answers are `scope --` or making the change — and it must not guess,
  because guessing once let `resolve --fixed` typed before any edit accept the defect itself.
- **"No executable check applies" is a recordable answer** (`validate --none --reason`), not a
  green `true` — which satisfies condition 1 vacuously *and* reaches the reviewer as a check
  the author claims to have run.
- **Red validation opens a pass in `project` mode, never in task mode.** An inherited area is
  reviewed as it stands, and refusing would make a failing repository unreviewable. Nothing is
  waved through: `accept` still refuses, and each check is stamped `inherited` or `regressed`
  from the loop's own history, because one this loop broke is a defect in the change.
- **Scope is task-owned paths, decided from task history — never `git status` alone.** When
  ownership is ambiguous, ask the user. Unrelated worktree work is untouched.
- **Project review is a queue of area loops, not one big loop.** `project.json` is a ledger:
  which areas, in what order, each one's outcome. Areas come from the agent's reading of the
  repository; the script only keeps the ledger. Every gate `accept` applies, `project close`
  applies too — a loop is bound to the area that opened it, and an area that moved since its
  pass is not reviewed. Refuse what the agent can fix now (a missing findings file, in either
  mode — closing deletes the loop state, so that file is the review's only survivor); record
  what it cannot (no pass, no understanding, unreadable paths) as `incomplete`, and never
  stop the project on one stuck area. `--report-only` exists because a fix in one area can
  break another.
- **A gate belongs on the path the workflow actually walks.** The empty-scope check once lived
  in `cmd_init` while every project review went through `project init`/`project next`, and the
  test that "covered" it exercised a command the workflow never runs. Write the case against
  the documented command sequence, not a convenient one.
- **SKILL.md stays short — budget it in words, not lines (~2000).** Line count hides the cost.
  It is loaded into the agent's context on every invocation; detail only one mode needs
  belongs in `references/`, read on demand. `scripts/selftest.py` parses the number out of
  this bullet and enforces it.
- **Escape hatches stay explicit.** `--force` exists on `init`, `pass-start`, `validate-drop`,
  `brief`, `project init` and `project close`; `--allow-empty` on `init` and `project init` is
  the same thing under another name. Keep this list complete — it is the audit surface. All
  are opt-in and the agent uses none on its own initiative, with one exception: `project close
  --force` on an area whose loop state was lost — or belongs to another scope — which settles
  it `incomplete` so the queue continues. It never lets an area pass, and it never deletes a
  loop it does not own; `reset` refuses only the in-progress area's *own* loop, because a
  foreign one holds no outcome to protect and refusing both left no way out at all. `validate-drop` without `--force` retracts only
  records whose exit status means the command never ran (126/127) **and that no recorded pass
  rested on** — a check the review was granted on stays evidence once its tool disappears, so
  retiring it is always the human's call; retracted records stay in `state.json` with a reason
  rather than being deleted.

## Not a problem — do not file these

- **No CI and no lint config.** Deliberate: there is nowhere to run them and the artifact
  is a prompt plus a state machine. **`scripts/selftest.py` is the exception, and it is not
  optional.** The old "no tests at all" rule was paid for once: three fixed defects (an
  unframed fingerprint, a non-atomic state write, missing argument validation) were reverted
  by a file copy and nothing noticed. One stdlib file with no framework is not the
  scaffolding that rule was protecting against — it is the only evidence that the refusals
  still refuse.
- **`.gitignore` exists and lists `.loop-review/`.** The old rationale — "that directory is
  created in the *user's* repository, not this one" — was simply false: the skill is run
  against this repository too, so the loop state lands here, untracked, one `git add -A`
  away from the commit the Forbidden list prohibits. The script also excludes the directory
  from every scope it fingerprints, so the two protections are independent: the pathspec
  keeps the loop working, the ignore keeps the state out of history.
- **`shell=True` in `validate`.** The command comes from the operating agent and is echoed
  before running. This is a local developer tool; it is not an injection boundary.
- **`evals/evals.json` carries no fixtures.** Every entry has `"files": []`, and the paths
  in the prompts (`src/http/client.ts`, `db/migrations/…`) are illustrative, not real. The
  eval judges the *shape* of the agent's response — which commands and flags it reaches for,
  which refusals it respects, what it declines to do — against `expected_output`, and that
  needs no repository. Keep `files` empty unless an eval genuinely cannot be judged without
  one; a fixture would have to be carried into every skills directory.
- **Findings are counts, not records.** `--fixed/--withdrawn/--adjudicated-invalid` are
  aggregates and `resolved` is clamped to `findings`. Per-finding tracking was rejected as
  bookkeeping the model would get wrong; the reviewer's text is the record.
- **`state.json` is not concurrency-safe.** One agent per worktree is the assumption.
- **Score anchors are duplicated across both reviewer prompts and
  `references/review-dimensions.md`.** Intended: the prompts are sent standalone to agents
  that never see the reference file, so the anchors have to travel with them. Keep the four
  *bands* aligned; their wording is deliberately not identical, because the area prompt
  anchors on tests where the task prompt anchors on the brief and validation — a project
  area has no brief at all (`init` refuses one in that mode), so copying the task wording
  into it would make every area reviewer grade against a document that cannot exist.
- **The state machine cannot detect a lying agent.** It gates procedure, not honesty. That
  is a known and accepted limit — do not propose "verification" of reviewer output.
- **Prose is prescriptive and repetitive.** These files are prompts. Redundancy that would
  be noise in documentation is what makes instructions survive a long context.

## Forbidden

- Do not add dependencies, a build step, or a package manifest.
- Do not give the script the ability to edit files, change git state, or call a model.
- Do not weaken or bypass a gate to make a loop finish; an unmet exit condition is reported
  as **INCOMPLETE** with the exact blocker.
- Do not commit `.loop-review/` or any state file from a smoke run.
- Do not rename commands, flags or `state.json` keys without updating SKILL.md, README.md
  and the evals in the same change — the CLI *is* the public API of this skill.
- Do not change workflow policy in `scripts/` alone; policy changes start in SKILL.md.
- Do not translate the skill files. They are English because the runtime prompts are.
