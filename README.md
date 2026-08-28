# loop-code-review

Agent Skill: review the current task's git changes through fresh, context-isolated reviewer sub-agents, fix, validate, repeat — with the exit condition enforced by a script instead of by prompt discipline.

Works in Claude Code, Codex CLI, and any Agent Skills-compatible runtime.

## Layout

```
SKILL.md                           workflow + exit condition, both scopes
scripts/loop_review.py             state machine: scope, brief, passes, validation, accept gate
scripts/selftest.py                regression check: asserts every refusal (`python3 scripts/selftest.py`)
references/reviewer-prompt.md      self-contained prompt for the reviewer / adjudicator
references/reviewer-prompt-area.md the same, for an inherited area in project mode
references/project-mode.md         rules for the area queue
references/review-dimensions.md    what counts as an actionable finding
evals/evals.json                   test prompts
```

## Install

The repository *is* the skill directory; install it under the skill's own name.

```sh
git clone https://github.com/ponch04/loop-code-review-skill.git
rm -rf loop-code-review-skill/.git                    # the skill, without the repository
rm -rf ~/.claude/skills/loop-code-review              # Claude Code
cp -R loop-code-review-skill ~/.claude/skills/loop-code-review
```

Both `rm -rf` lines matter, and this is also the update command. `cp -R src dst` copies
*into* `dst` when it already exists, so without the second one you get a nested copy and the
runtime keeps loading the old `SKILL.md`; without the first, git's object files are
read-only and `cp` fails partway on the second run. For Codex, use `~/.codex/skills/` — it
also reads `~/.agents/skills`, and per project `.agents/skills`.

Requires Python 3.8+ and git — no dependencies, no build step. Loop state is kept in `.loop-review/` inside the repository you are reviewing; add it to that repository's `.gitignore`.

## Why a script

The original prompt-only version relied on the model to remember pass counts, notice unchanged diffs, and refuse to accept with red validation. Models on long instructions cut corners. `loop_review.py` makes those gates mechanical: `pass-start` refuses without green validation or on an unchanged scope, `accept` lists exactly what is blocking, and the pass limit is a hard stop unless the user asked for persistence.

The same idea covers more that a prompt cannot hold:

- **Evidence may not shrink.** A pass records which checks it rested on. After a fix batch, re-running only the fast one does not buy another pass — the script names the checks still missing. Re-running a command supersedes its own earlier result, so a flaky failure clears without weakening anything else.
- **The task brief lives in the state, not in the agent's head.** `init --task-brief` records the requirements once; `status` and `pass-start` echo them so every reviewer of the loop is briefed identically. A brief retyped per pass drifts, and a drifting brief silently redefines what "satisfies the task" means. With one recorded, a missed requirement is a finding; without one, the reviewer can only check that the code is self-consistent, and `status` says so.
- **An empty scope is refused, in both modes.** A loop on an empty scope passes every gate — nothing runs against no change, the fingerprint never moves, `accept` sees no blocker. `init` exits `2` and tells you to use `--base`, fix the paths, or report `no-changes` and stop; a project area matching no file is refused the same way, so a mistyped path cannot be closed as reviewed.
- **A validation run that edits what it validates is not evidence.** A formatter or a snapshot updater moves the scope while it checks it; the result is recorded, marked, and ignored by the gate until the command is re-run on a settled state.
- **A review that goes stale is aborted, not invented.** If the files move while the reviewer is reading them, `pass-abort --reason ...` discards that pass — no fabricated score, and no `reset` that would throw away the loop's history.

## Whole-project review

`project init -- <areas...>` queues one loop per area, each with its own passes and the same gates, and runs until every area has an outcome. `--report-only` collects findings into `.loop-review/findings/` without editing anything — the right default when a fix in one area can break another. A standalone `+` glues neighbouring paths into a single area (`-- pkg/domain app/lib + app/lib-tests`).

## Credits and license

[MIT](LICENSE). The workflow, the exit condition and parts of the reviewer prose derive from [di-sukharev/loop-code-review-skill](https://github.com/di-sukharev/loop-code-review-skill) (MIT). The state machine, project mode and the task brief are original here.
