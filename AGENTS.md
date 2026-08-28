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
3. `README.md` — derived, human-facing, lowest priority. Never resolve a question from it.
4. `evals/evals.json` — the behavioural spec of the *agent*, not of the code. Changing
   workflow behaviour without updating the affected eval is an incomplete change.

## Layout

```
SKILL.md                            workflow, both scopes
scripts/loop_review.py              state machine + project ledger
references/reviewer-prompt.md       task-mode reviewer (a diff)
references/reviewer-prompt-area.md  project-mode reviewer (an inherited area)
references/review-dimensions.md     what counts as an actionable finding
evals/evals.json                    behavioural spec for the agent
```

## Build and test

No build, no package manager, no dependencies. Python 3.8+ stdlib and `git` only.

```sh
python3 scripts/loop_review.py --help          # smoke: argument surface parses
# end-to-end smoke of the state machine (from a clean worktree):
python3 scripts/loop_review.py init -- README.md
python3 scripts/loop_review.py validate -- true
python3 scripts/loop_review.py pass-start
python3 scripts/loop_review.py pass-record --score 9.5 --findings 0 --understood --test-evidence trusted
python3 scripts/loop_review.py accept          # expect exit 0
python3 scripts/loop_review.py reset          # removes .loop-review/ as well
```

`evals/evals.json` is not a runnable suite — the prompts are executed against a real runtime
and judged against `expected_output`. There is no CI.

## Decisions that must not be broken

- **Zero dependencies, single file.** The script must stay stdlib-only, Python 3.8+
  compatible, and self-contained. A skill that needs `pip install` will not be installed.
- **The script never touches git state or source files.** It reads (`diff`, `ls-files`,
  `rev-parse`) and writes only `.loop-review/state.json`. No add/commit/stash/reset/push,
  no edits — in the script or in the workflow, unless the user explicitly asks.
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
- **SKILL.md stays short (~100 lines).** It is loaded into the agent's context every time.
  Detail belongs in `references/`, which is read on demand. Growth there is a real cost.
- **Scope is task-owned paths, decided from task history — never `git status` alone.** When
  ownership is ambiguous, the workflow asks the user. Unrelated worktree work is untouched.
- **Project review is a queue of area loops, not one big loop.** `project.json` is a ledger
  only: which areas exist, in what order, and each one's outcome. Every area runs the same
  state machine with the same gates and the same per-area pass limit; the project finishes
  when every area has an outcome, never when a count runs out. Areas come from the agent's
  reading of the repository structure; the script does not derive them. `--report-only`
  exists because a fix in one area can break another: in that mode an area is `reviewed`
  when its findings are on disk in `.loop-review/findings/`, and nothing is edited.
- **Escape hatches stay explicit.** `--force` on `pass-start`/`init`/`validate-drop` exists
  so a human can override; it must remain opt-in and never be used by the agent on its own
  initiative. `validate-drop` without `--force` retracts only records whose exit status means
  the command never ran (126/127); retracting a real failure is a gate bypass, and retracted
  records stay in `state.json` with a reason rather than being deleted. Retiring a check the
  last pass rested on always needs `--force`, and is stamped on the current fingerprint —
  recorded history is never rewritten.

## Not a problem — do not file these

- **No unit tests for `loop_review.py`, no CI, no lint config.** Deliberate: the artifact is
  a prompt-plus-state-machine judged by the evals, and test scaffolding would be copied into
  every user's skills directory.
- **No `.gitignore`.** `.loop-review/` is created inside the *user's* repository, not this
  one; ignoring it is the user's step, documented in README.md.
- **`shell=True` in `validate`.** The command comes from the operating agent and is echoed
  before running. This is a local developer tool; it is not an injection boundary.
- **Findings are counts, not records.** `--fixed/--withdrawn/--adjudicated-invalid` are
  aggregates and `resolved` is clamped to `findings`. Per-finding tracking was rejected as
  bookkeeping the model would get wrong; the reviewer's text is the record.
- **`state.json` is not concurrency-safe.** One agent per worktree is the assumption.
- **Score anchors appear in both SKILL.md and `references/review-dimensions.md`.** Intended
  duplication: progressive disclosure, so the anchors survive without the reference file.
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
