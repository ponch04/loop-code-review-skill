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
a findings file. A happy-path smoke is worthless here: one stayed green while three fixed
defects were reverted. Any change to a gate adds or updates a case in the same commit, and
a new case must be shown to fail on the defect it covers before it is trusted.

`evals/evals.json` is not a runnable suite — the prompts are executed against a real runtime
and judged against `expected_output`. There is no CI.

## Decisions that must not be broken

- **Zero dependencies, single file.** The script must stay stdlib-only, Python 3.8+
  compatible, and self-contained. A skill that needs `pip install` will not be installed.
- **The script never touches git state or source files.** It only reads git (`diff`,
  `ls-files`, `rev-parse`, and `hash-object -t tree` for the empty-tree id — without `-w`
  that writes nothing), and it only writes inside `.loop-review/`: `state.json`,
  `project.json`, their `.tmp` files, the `findings/` directory it creates, the rename of a
  previous review to `<slug>.prev.md` that `project next` performs there, and the removals
  `project close`/`reset` perform there. Keep this list exhaustive — it is what an audit of
  "the script does not touch files" is checked against. No add/commit/stash/reset/push, no edits to source — in the script or in the
  workflow, unless the user explicitly asks. The one deliberate hole is `validate`, which
  runs a command the agent chose: that command can do anything, which is why a run that
  moves the scope is recorded as non-evidence rather than trusted.
- **The script never spawns or talks to a model.** Reviewer output is transcribed into it by
  the orchestrating agent. Keeping the loop runtime-agnostic is why it is portable.
- **The score never gates acceptance.** 1–10 is a progress signal — this covers `--score`
  and `--test-score` alike. Neither may appear in `blockers()`, and no finding may be created
  or kept to justify a number. Findings win. Test evidence gates through the categorical
  `--test-evidence` verdict, never through the test score.
- **Acceptance = the four conditions in SKILL.md** (green validation on the current state,
  zero unresolved findings, credible understanding, trustworthy or justified test evidence).
  Do not add a fifth condition, and do not let any of them become advisory. All four are
  checked in `blockers()`; understanding and test evidence are agent-recorded verdicts
  (`--understood`, `--test-evidence`) because the script cannot judge them itself.
- **Fingerprint identity.** A pass, a validation run and an acceptance are all bound to a
  hash of the reviewed state. In `changes` mode that is the scoped diff against `--base`
  (default HEAD; the empty tree on an unborn branch) plus untracked contents; in `project`
  mode it is the contents of every file under the area, because an inherited area has no
  diff — its identity is what it contains. Validation from a different
  fingerprint is not evidence; a review of an unchanged fingerprint is not a new pass.
  Within one fingerprint the **latest run of each command wins**, so re-running clears an
  environmental or flaky failure; distinct commands are never conflated. Evidence may not
  shrink: a new pass must carry every check the previous pass rested on, unless that check
  was retracted on the current state.
- **Reviewer context isolation.** The reviewer receives `references/reviewer-prompt.md`
  filled in, and nothing else — no author rationale, no suspected issues, no prior reviewer
  output. Any change that leaks author context into the reviewer defeats the skill.
- **Two things cross that boundary, and only two**: the task brief, as requirements and
  acceptance criteria, and the scope note (`--scope-note`), as which hunks of a scoped file
  are task-owned. Both are recorded in `state.json`, echoed verbatim by `status` and
  `pass-start`, and copied into their own prompt fields — never recalled, so they cannot
  drift between passes. The same discipline governs both: facts about the task, never
  reasoning about the change. Adding a third channel is a change to the product.
- **The task brief** carries requirements and acceptance criteria, and nothing else. The line is: what the task had to achieve is a fact about the
  task; how the author achieved it, what they suspect is wrong, and what they propose to do
  are author reasoning and stay out. Without a brief the reviewer can only check that the
  implementation is self-consistent — it cannot report a missed requirement, which is why
  requirement conformance is a review dimension that goes silent when none was recorded.
  The brief is recorded once in `state.json` by `init --task-brief` and echoed by `status`
  and `pass-start`, for the same reason the pass count is: a brief retyped per pass drifts,
  and a drifting brief silently redefines what "satisfies the task" means between passes.
- **`.loop-review/` is excluded in Python, never by a `:(exclude)` pathspec on `ls-files`.**
  `git ls-files --cached` truncates every pathspec item by the common directory prefix of
  the *positive* items, and negative items do not contribute to that prefix; for a scope
  like `apps/portal/src/lib` the exclusion is read past its end, matches everything, and the
  command returns nothing. The area then hashes as empty, no fix ever moves the fingerprint,
  and `accept` signs off a review of code that has since been rewritten. `:(exclude,top)`
  does not survive it either. `git diff` and `ls-files --others` are unaffected, so the
  pathspec form stays in `scoped()` for the diff only; every `ls-files` caller filters with
  `outside_state_dir()`. Fixtures for this must use paths several directories deep — a
  shallow one cannot reach the bug.
- **Scope widens, never narrows, and only through `scope --`.** A reviewer regularly points
  at the cause in a neighbouring file, and the fix lands outside the recorded scope: the
  fingerprint never moves, the review stays "current", and `accept` signs off code no
  reviewer saw. Widening moves the fingerprint — which is what makes the next pass mandatory
  — and keeps the pass history, unlike `init --force`, the only mechanical answer before,
  which destroys it. Narrowing would retire evidence a recorded pass rested on, so it stays
  a human decision like every other retirement.
- **"No executable check applies" is a recordable answer, not a `true`.** An inherited area
  of prose, config or fixtures has no test, typecheck, lint or build, and `pass-start`
  refuses without a validation record — a refusal `--force` does not cover. The only move
  left was `validate -- true`, which satisfies exit condition 1 vacuously *and* is
  transcribed into the reviewer's prompt as a check the author claims to have run.
  `validate --none --reason ...` records the absence with its reason instead; the gate is
  satisfied and everyone downstream can see there was nothing to run.
- **A `--base` ref is pinned to its commit id at `init`, never kept symbolic.** `HEAD~1` is
  relative to HEAD, and the workflow may commit at the user's request (or the user may, in
  another terminal); the moment it does, the same string names a different commit, the
  scoped diff narrows to the newest work, and `accept` signs off a task most of which no
  reviewer saw. Pinning removes the class instead of warning about it in prose.
- **A queue never blocks on an area it cannot read.** `project next` records an area whose
  paths match nothing as `incomplete` and moves on. Leaving it `pending` meant every later
  `project next` hit the same area, so one renamed directory made every remaining area
  unreachable, recoverable only by rebuilding the ledger and discarding outcomes already
  earned — the precise opposite of what SKILL.md promises.
- **A gate belongs on the path the workflow actually walks.** The empty-scope check sat in
  `cmd_init` while SKILL.md sent every project review through `project init`/`project next`,
  which never called it — and the test that "covered" it exercised `init --mode project`, a
  command the workflow never runs, so the gap read as tested. Both project commands check
  now (queue time reports every bad path at once; open time catches an area emptied since),
  and a new case must be written against the documented command sequence, not a convenient
  one.
- **The agent never supplies a value the reviewer withheld.** `pass-record` requires only
  `--findings`; the score and the test-evidence verdict are optional precisely so that a
  reviewer which omitted one cannot force the agent to choose between inventing it and
  stalling. The documented remedy — ask once, then `amend` — was unexecutable while those
  flags were required, because the pass could not exist before the missing values did. A
  missing verdict still blocks acceptance in `blockers()`; a missing score blocks nothing,
  because the score gates nothing.
- **A loop is bound to the area that opened it.** `project close` verifies the loop on disk
  is this area's — project mode, and a scope covering the area's paths — because `init
  --force` inside a live area silently replaced it with an unrelated `changes` loop, and the
  area was then recorded `accepted`, with a score, from a review of other code. `init` now
  refuses inside a live area exactly as `reset` and `project init` already did.
- **Every gate `accept` applies, `project close` applies too.** Report-only never reaches
  `blockers()`, so an area rewritten or deleted after its pass closed `reviewed` with an
  empty blocker list, and closing then deleted the only state that could contradict it. Both
  branches now check staleness and emptiness, and both require the findings file — a clean
  area's review is the one that vanishes completely without it.
- **Assert the blocker, not the exit code.** A selftest case that only checks `returncode
  != 0` is usually satisfied by a *different* refusal, so it reads as coverage while the gate
  it names can be deleted with the suite green. Three of the four acceptance conditions were
  in that state at once. Name the expected reason in the assertion.
- **Refuse what the agent can fix now; record what it cannot.** `project close` in
  report-only refuses only while the findings file is missing — that is fixable on the spot,
  and closing deletes the state the review lives in. An area with no recorded pass or no
  credible understanding is settled as `incomplete` with its blockers and the queue moves
  on, with no `--force`: a hatch there stalled the whole project on one bad area, on the
  mode `references/project-mode.md` recommends by default. Conflating the two kinds of problem is the bug.
- **A review invalidated mid-flight is aborted, never invented.** If the scope moves while
  the reviewer reads it, `pass-abort --reason ...` discards the open pass: it counts as
  used, it stays in `state.json` with its reason, and it can never satisfy acceptance. The
  alternatives it replaces were both corrupting — fabricating a result for a review nobody
  trusts, or `reset`, which destroys the loop history (and strands a project area).
- **Red validation opens a pass in `project` mode, both submodes — never in task mode.** An
  inherited area is reviewed as it stands, so "fix red validation first" has no addressee
  and refusing would make a failing repository unreviewable, which is the kind most likely
  to be sent for a full review. Nothing is waved through: the red commands are stamped on
  the pass, `accept` still refuses on them (so a fix-mode area closes `incomplete` with them
  as its blocker), and in report-only `project close` requires the findings file even when
  the reviewer reported nothing. Each is stamped `inherited` or `regressed` from the loop's
  own history, because a check this loop's fixes broke is a defect in the change, and
  telling a fresh reviewer it is a property of the area hands it the wrong conclusion.
- **The scope is checked for emptiness on every gate, not only at `init`.** A scope empties
  mid-loop whenever the work is committed, reverted or stashed, or an area's directory is
  deleted — ordinary events the workflow does not forbid. From that moment the loop reviews
  nothing while passing every gate exactly as it would on an empty `init`, and in project
  mode that lie outlives the loop in the ledger. `blockers()` and `pass-start` both ask. A
  path git cannot see (ignored, or mistyped) hides behind its healthy neighbours the same
  way — it contributes nothing to the fingerprint, so edits to it never make a review stale
  — so `init` and `scope` warn per path.
- **An empty scope is an outcome, not a warning.** `init` in `changes` mode exits `2`
  (`NO_CHANGES`) on an empty fingerprint, because such a loop passes every gate: nothing
  runs against no change, the diff never moves, and `accept` sees no blocker. The workflow
  answer is `--base` for committed work, corrected paths, or reporting `no-changes` and
  stopping. `--allow-empty` is the explicit hatch and belongs to the human, like `--force`.
  Ask `scope_is_empty()`, which inspects git directly; never compare against a stored hash.
  A constant compared against the fingerprint silently stops matching the moment the hash
  format moves — adding length-prefixed fields already changed the hash of an empty
  `changes` scope once — and a comparison against an unscoped `fingerprint([])` is worse
  still: that hashes the whole repository's diff, byte-identical to the scoped one whenever
  the scope is the only thing changed, so it reported "empty" exactly when the scope was
  healthy.
- **An unusable reviewer or adjudicator never resolves a finding.** Each is asked once for
  what it left out; an unusable reviewer may be replaced once by a fresh one. If output is
  still unusable, the finding stays unresolved and the outcome is INCOMPLETE. Silence is
  not a verdict.
- **SKILL.md stays short — budget it in words, not lines (~2000).** Line count hides the
  cost: the file once fell from 133 lines to 124 while growing from 1782 to 2590 words,
  because the lines got longer. SKILL.md is loaded into the agent's context on every
  invocation, so its size is a standing cost; detail that only one mode needs belongs in
  `references/`, which is read on demand — that is why `project-mode.md` exists.
  `scripts/selftest.py` enforces this number by parsing it out of this bullet, so the budget
  and the file cannot drift apart again. The budget was ~100 while the file was smaller than the workflow it
  describes — three dead ends (a stale review with no way out, report-only on a red
  repository, no mention of the state directory or `reset`) existed because the prose had
  no room for them. The command examples are already consolidated into one block per mode
  rather than one fence per step; take that saving first, and cut prose before dropping a
  rule an agent needs at runtime.
- **Scope is task-owned paths, decided from task history — never `git status` alone.** When
  ownership is ambiguous, the workflow asks the user. Unrelated worktree work is untouched.
- **Project review is a queue of area loops, not one big loop.** `project.json` is a ledger
  only: which areas exist, in what order, and each one's outcome. Every area runs the same
  state machine with the same gates and the same per-area pass limit; the project finishes
  when every area has an outcome, never when a count runs out. Areas come from the agent's
  reading of the repository structure; the script does not derive them. `--report-only`
  exists because a fix in one area can break another: in that mode an area is `reviewed`
  when a pass is recorded with a credible understanding and the reviewer's whole output is
  on disk in `.loop-review/findings/`. A clean area needs the file too — closing deletes the
  loop state, so for an area with no findings that file is the entire surviving record.
  Nothing is edited, and the loop's own `accept` is not required, because nothing is meant
  to be fixed.
- **Escape hatches stay explicit.** `--force` exists on seven commands — `init`,
  `pass-start`, `validate-drop`, `brief`, `reset`, `project init` and `project close` — so a
  human can override. Keep this list complete: it is the audit surface, and two of the seven
  were missing from it while `reset --force`, the only way to discard an area's state
  mid-loop, was documented nowhere but its own refusal message. All of them stay opt-in, and the agent uses none on its own initiative, with
  one carved-out exception: `project close --force` on an area whose loop state was lost,
  which is the documented way to record it `incomplete` and free the queue (SKILL.md P2).
  That exception exists because the alternative is a stuck project, which SKILL.md forbids;
  it settles an area as failed, it never lets one pass. `--allow-empty` on `init` is the
  same kind of hatch under a different name, on `init` and `project init`, and follows the
  same rule; it is persisted in `state.json`, because emptiness is now checked on every gate
  and a deliberately blind loop has to stay legal. `validate-drop` without `--force` retracts only records whose exit status means
  the command never ran (126/127); retracting a real failure is a gate bypass, and retracted
  records stay in `state.json` with a reason rather than being deleted. Retiring a check the
  last pass rested on always needs `--force`, and is stamped on the current fingerprint —
  recorded history is never rewritten.

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
