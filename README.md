# loop-code-review

Agent Skill: review the current task's git changes through fresh, context-isolated reviewer sub-agents, fix, validate, repeat — with the exit condition enforced by a script instead of by prompt discipline.

Works in Claude Code, Codex CLI, and any Agent Skills-compatible runtime.

## Layout

```
SKILL.md                      workflow + exit condition (~100 lines)
scripts/loop_review.py        state machine: scope, passes, validation, accept gate
references/reviewer-prompt.md self-contained prompt for the reviewer / adjudicator
references/review-dimensions.md what counts as an actionable finding
evals/evals.json              test prompts
```

## Install

```sh
cp -R loop-code-review ~/.claude/skills/      # Claude Code
cp -R loop-code-review ~/.codex/skills/       # Codex CLI
```

Requires Python 3.8+ and git. State is kept in `.loop-review/state.json` inside the repo — add it to `.gitignore`.

## Why a script

The original prompt-only version relied on the model to remember pass counts, notice unchanged diffs, and refuse to accept with red validation. Models on long instructions cut corners. `loop_review.py` makes those gates mechanical: `pass-start` refuses without green validation or on an unchanged scope, `accept` lists exactly what is blocking, and the pass limit is a hard stop unless the user asked for persistence.
