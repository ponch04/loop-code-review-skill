#!/usr/bin/env python3
"""Loop-review state machine.

Keeps the loop honest: tracks scope, validation, passes and findings in
.loop-review/state.json inside the repo, and refuses to accept until the
exit condition holds. No dependencies beyond git and Python 3.8+.

Two scopes share one loop: `changes` (default) fingerprints the scoped diff against a base
commit — the task's own edits; `project` fingerprints the *contents* of every file under the
scoped paths — an inherited area reviewed as a whole. A project review is a queue of such
area loops, tracked in .loop-review/project.json by the `project` commands.

Commands:
  init [--max-passes N] [--mode changes|project] [--base REF] [--allow-empty]
       [--task-brief TEXT | --task-brief-file PATH] -- <paths...>
                                           start a loop over paths (task diff or whole area)
  project init [--max-passes N] [--report-only] -- <area paths...>
                                           queue a whole-project review, one loop per area
  project next                             open the loop for the next pending area
  project close                            record the current area's outcome, free the loop
  project status [--json]                  ledger of areas and outcomes
  validate -- <command...>                 run a validation command, record result
  validate-drop [--force] -- <command...>  retract a record for a command that never ran,
                                           or (--force) retire a check inherited from the last pass
  pass-start                               open a full-review pass (checks gates)
  pass-record --score S --findings N --test-evidence {trusted|justified-absent|inadequate}
              [--understood] [--test-score T] [--note TEXT]
  amend [--understood] [--test-evidence V] [--score S] [--findings N] [--test-score T] [--note TEXT]
                                           add missing reviewer output to the last pass
  resolve [--fixed N] [--withdrawn N] [--adjudicated-invalid N]
  accept                                   exit 0 if accepted, else 1 + reasons
  status [--json]
  fingerprint                              print hash of the scoped diff
  reset                                    delete state
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

# Exit condition 4 in SKILL.md, made mechanical. The script cannot judge whether test
# evidence is trustworthy; it can only require the agent to record the reviewer's verdict
# and refuse to accept on the one value that means "no adequate evidence, no justification".
# Shell exit codes meaning the command was never executed: not found / not executable.
# Only these may be retracted — a command that ran and failed is a gate, not a mistake.
NEVER_RAN = (126, 127)

TEST_EVIDENCE = ("trusted", "justified-absent", "inadequate")
TEST_EVIDENCE_BLOCKING = "inadequate"

# `init` on an empty scoped diff is not a failure of the tool, it is an outcome of the
# workflow: there is nothing task-owned to review. It gets its own exit code so the agent
# can tell it apart from a usage error and report `no-changes` instead of looping blind.
NO_CHANGES = 2

# fingerprint() of a scope with no diff and no untracked files: sha256 of nothing. The test
# must be against this constant, never against `fingerprint([])` — an unscoped fingerprint
# hashes the diff of the *whole* repository, which is byte-identical to the scoped one
# whenever the scoped paths are the only thing changed. That is the common case, so the
# comparison reported "empty scope" exactly when the scope was healthy.
EMPTY_FINGERPRINT = hashlib.sha256().hexdigest()[:16]

STATE_DIR = ".loop-review"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PROJECT_FILE = os.path.join(STATE_DIR, "project.json")
FINDINGS_DIR = os.path.join(STATE_DIR, "findings")
MODES = ("changes", "project")


# ---------- helpers ----------

def git(*args, check=True):
    """Run git and return raw stdout **bytes**.

    Neither diffs nor path names are guaranteed to be UTF-8: a latin-1 source file or a
    filename in another encoding makes `text=True` raise UnicodeDecodeError and kill the
    whole loop. Decoding is therefore the caller's decision, and `fingerprint()` skips it
    entirely — it hashes the bytes.
    """
    r = subprocess.run(["git", *args], capture_output=True)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def git_text(*args, **kw):
    """git output as text, for paths and display. Undecodable bytes survive as surrogates
    (os.fsdecode), so a path can still be handed back to open()/os.chdir() unchanged."""
    return os.fsdecode(git(*args, **kw))


def repo_root():
    return git_text("rev-parse", "--show-toplevel").strip()


def to_repo_relative(path, root):
    """Interpret a scope path against the caller's cwd, store it relative to the repo root.

    `init` may be run from a subdirectory, but every later command chdirs to the root, so
    a path kept as typed would resolve against the wrong directory and silently scope the
    loop to nothing.
    """
    rel = os.path.relpath(os.path.abspath(path), root)
    if rel.split(os.sep)[0] == os.pardir:
        # cwd or root may be reached through a symlink; compare resolved paths too.
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    if rel.split(os.sep)[0] == os.pardir:
        die(f"path is outside the repository: {path}")
    return rel.replace(os.sep, "/")


def die(msg, code=1):
    print(f"loop-review: {msg}", file=sys.stderr)
    sys.exit(code)


def load():
    if not os.path.exists(STATE_FILE):
        die("no active loop; run `init` first")
    with open(STATE_FILE) as f:
        try:
            return json.load(f)
        except ValueError as e:
            die(f"{STATE_FILE} is not valid JSON ({e}); run `reset` and start the loop again")


def save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def diff_base():
    """`HEAD`, or the empty tree while the repository has no commit yet.

    `git diff HEAD` exits 128 on an unborn branch. With `check=False` that produced an
    empty diff, so a staged file — invisible to `ls-files --others` — left the fingerprint
    constant and the loop blind to every change in it. The empty-tree id is asked of git
    rather than hardcoded, so this also holds in sha256 repositories.
    """
    if git("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
        return "HEAD"
    return git_text("hash-object", "-t", "tree", os.devnull).strip()


def fingerprint(paths, mode="changes", base=None):
    """Hash identifying the reviewed state, scoped to paths.

    changes: diff of the worktree against `base` (HEAD by default) plus untracked contents —
             the task's own edits. Empty when the work is already committed and no base is
             given; pass `init --base <ref>` for a committed task.
    project: contents of every tracked and untracked file under the paths. The area is
             reviewed as it is, so its identity is its content, not a diff.
    """
    h = hashlib.sha256()
    if mode == "project":
        out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths)
        for p in sorted({p for p in out.split(b"\0") if p}):
            h.update(p)
            try:
                with open(p, "rb") as f:
                    h.update(f.read())
            except OSError as e:
                h.update(f"<unreadable:{e.errno}>".encode())
        return h.hexdigest()[:16]
    # check=True on both git calls: a failure here must be loud. A silently empty hash is
    # indistinguishable from "nothing changed", which is the one lie that breaks every gate.
    h.update(git("diff", base or diff_base(), "--", *paths))
    # -z: NUL-separated and unquoted, so paths with spaces, newlines or non-ASCII
    # characters survive intact. Splitting plain `ls-files` output would corrupt them
    # and silently drop those files from the hash.
    out = git("ls-files", "-z", "--others", "--exclude-standard", "--", *paths)
    # Kept as bytes: a filename need not be decodable, and open() takes bytes paths.
    untracked = [p for p in out.split(b"\0") if p]
    for p in sorted(untracked):
        h.update(p)
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError as e:
            # Fold the failure in: an unreadable file must not hash like an empty one.
            h.update(f"<unreadable:{e.errno}>".encode())
    return h.hexdigest()[:16]


def fp_of(state):
    return fingerprint(state["scope"], state.get("mode", "changes"), state.get("base"))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def last_pass(state):
    return state["passes"][-1] if state["passes"] else None


def validation_at(state, fp):
    """Validation runs at fingerprint `fp`, latest non-retracted result per command.

    Re-running the same command on unchanged files supersedes its earlier result, so a
    flaky or environment failure can be cleared by re-running it — not only by editing.
    Distinct commands are tracked separately: a green re-run of the tests does not
    absolve a red lint.
    """
    latest = {}
    for v in state["validation"]:
        if v["fingerprint"] == fp and not v.get("retracted"):
            latest[v["cmd"]] = v
    return latest


def current_validation(state):
    return list(validation_at(state, state["fingerprint_current"]).values())


def missing_since_last_pass(state):
    """Commands the last pass rested on that have no record on the current state.

    Evidence may not silently shrink: after a fix batch, re-running one quick check would
    otherwise be enough to open the next pass, and acceptance only ever looks at the
    commands present on the accepted fingerprint. So a later pass may not be granted on a
    weaker set than the pass before it (SKILL.md steps 2 and 5).
    """
    lp = last_pass(state)
    if lp is None or lp["fingerprint"] == state["fingerprint_current"]:
        return []
    prior = validation_at(state, lp["fingerprint"])
    present = validation_at(state, state["fingerprint_current"])
    # A record retracted on the current state is a deliberate retirement of that check —
    # the command does not exist here any more (or a human forced it). Without this the
    # set could only ever grow, and `validate-drop` could not do what its message promises.
    retired = {v["cmd"] for v in state["validation"]
               if v["fingerprint"] == state["fingerprint_current"] and v.get("retracted")}
    return [c for c in prior if c not in present and c not in retired]


def print_validation(cur, header="validation on current state"):
    """One rendering of validation state for every text-mode command.

    `references/reviewer-prompt.md` is filled from `status`, and its validation section
    needs each command with its exit code — an aggregate GREEN/RED line cannot be
    transcribed into the reviewer's prompt.
    """
    print(f"{header}: {len(cur)} command(s), " +
          ("GREEN" if cur and all(v["exit"] == 0 for v in cur) else "RED/none"))
    for v in cur:
        print(f"  - {v['cmd']}  -> exit {v['exit']}")


def blockers(state):
    """Return list of reasons acceptance is not possible. Empty list == accepted."""
    reasons = []
    lp = last_pass(state)
    if lp is None:
        reasons.append("no full-review pass recorded")
        return reasons
    if lp.get("result") is None:
        reasons.append(f"pass {lp['n']} opened but not recorded (pass-record)")
        return reasons
    if state["fingerprint_current"] != lp["fingerprint"]:
        reasons.append("scoped changes moved since the last pass; review is stale")
    cur = current_validation(state)
    red = [v for v in cur if v["exit"] != 0]
    if not cur:
        reasons.append("no validation run against the current state")
    elif red:
        reasons.append(f"validation red: {red[-1]['cmd']}")
    unresolved = lp["result"]["findings"] - lp["result"]["resolved"]
    if unresolved > 0:
        reasons.append(f"{unresolved} unresolved actionable finding(s) from pass {lp['n']}")
    if not lp["result"]["understood"]:
        reasons.append("reviewer did not demonstrate a credible understanding of the change")
    evidence = lp["result"].get("test_evidence")
    if evidence is None:
        reasons.append(f"test evidence not assessed for pass {lp['n']} (record it with `amend --test-evidence ...`)")
    elif evidence == TEST_EVIDENCE_BLOCKING:
        reasons.append("test evidence for the changed behaviour is inadequate and its absence is not justified")
    return reasons


# ---------- commands ----------

def cmd_init(a):
    if not a.paths:
        die("init needs at least one task-owned path after `--`")
    root = repo_root()
    # Deduplicate: `f.py ./f.py f.py` is one path. git would collapse it anyway, but the
    # list is echoed into the reviewer prompt and must read as the agent meant it.
    scope = list(dict.fromkeys(to_repo_relative(p, root) for p in a.paths))
    os.chdir(root)
    if os.path.exists(STATE_FILE) and not a.force:
        die("loop already active; use `reset` or `--force`")
    if (a.task_brief or a.task_brief_file) and a.mode == "project":
        # An inherited area has no task and therefore no acceptance criteria to miss;
        # a brief here would only be the operator's opinion smuggled past isolation.
        die("--task-brief applies to --mode changes only")
    if a.base:
        if a.mode == "project":
            die("--base applies to --mode changes only")
        if not git("rev-parse", "--verify", "--quiet", a.base + "^{commit}", check=False).strip():
            die(f"--base {a.base} is not a commit in this repository")
    brief = read_task_brief(a)
    state = {
        "created": now(),
        "mode": a.mode,
        "base": a.base,
        "scope": scope,
        "task_brief": brief,
        "max_passes": a.max_passes,
        "passes": [],
        "validation": [],
    }
    state["fingerprint_current"] = fp_of(state)
    # Refuse rather than warn. A loop on an empty fingerprint passes every gate — validation
    # is green because nothing runs against a change, the diff never moves, and `accept`
    # sees no blocker — so the one thing the script exists to prevent is exactly what an
    # empty scope produces. The workflow outcome here is `no-changes`, not a review.
    if a.mode == "changes" and state["fingerprint_current"] == EMPTY_FINGERPRINT:
        if not a.allow_empty:
            die("no task-owned changes in scope — outcome is `no-changes`. If the task is already "
                "committed, re-run with --base <ref> (e.g. the parent of the first task commit). "
                "If the paths are wrong, fix them. Only pass --allow-empty when you intend a loop "
                "whose every gate is blind.", code=NO_CHANGES)
        print("warning: scoped diff is empty and --allow-empty was given; every gate is blind",
              file=sys.stderr)
    save(state)
    print(f"loop initialised ({a.mode}{' vs ' + a.base if a.base else ''}): {len(scope)} path(s), max {a.max_passes} passes, fingerprint {state['fingerprint_current']}")
    for p in scope:
        print(f"  - {p}")
    print_task_brief(state)


def read_task_brief(a):
    """The task's requirements and acceptance criteria, verbatim, or None.

    Recorded once at `init` and echoed by `status` and `pass-start` so every reviewer of
    this loop is briefed identically. Kept out of the agent's head for the same reason the
    pass count is: a brief retyped per pass drifts, and a drifting brief silently changes
    what "the change satisfies the task" means between passes.

    This is requirements only. Author rationale, suspected issues and proposed fixes stay
    out of it — they are what reviewer isolation exists to withhold.
    """
    if a.task_brief and a.task_brief_file:
        die("use --task-brief or --task-brief-file, not both")
    if a.task_brief_file:
        try:
            with open(a.task_brief_file, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            die(f"--task-brief-file {a.task_brief_file}: {e.strerror}")
        if not text:
            die(f"--task-brief-file {a.task_brief_file} is empty")
        return text
    if a.task_brief:
        return a.task_brief.strip() or None
    return None


def print_task_brief(state):
    brief = state.get("task_brief")
    if brief:
        print("task brief (give this to the reviewer verbatim):")
        for line in brief.splitlines():
            print(f"  | {line}")
    elif state.get("mode", "changes") == "changes":
        # Silent in project mode: an inherited area has no task, so no brief is missing.
        print("task brief: none recorded — the reviewer cannot report a missed requirement; "
              "record one with `init --task-brief` if the task has acceptance criteria")


def join_cmd(parts):
    """The state key for a validation command. `validate` and `validate-drop` must build
    it identically, or a retraction would silently miss the record it names."""
    return " ".join(parts) if len(parts) == 1 else shlex.join(parts)


def cmd_validate(a):
    os.chdir(repo_root())
    state = load()
    if not a.command:
        die("validate needs a command after `--`")
    cmd = join_cmd(a.command)
    print(f"$ {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True)
    state["fingerprint_current"] = fp_of(state)
    state["validation"].append({"cmd": cmd, "exit": r.returncode, "at": now(), "fingerprint": state["fingerprint_current"]})
    save(state)
    print(f"loop-review: validation {'GREEN' if r.returncode == 0 else 'RED'} (exit {r.returncode})")
    sys.exit(r.returncode)


def cmd_validate_drop(a):
    """Retract a validation record for a command that never executed.

    A mistyped command (`pytset`) is recorded under its own key, so re-running the correct
    one cannot supersede it and its exit 127 blocks `pass-start` for as long as the
    fingerprint holds. Retracting it is bookkeeping, not a bypass — which is why only
    "never ran" statuses qualify. A command that ran and failed is the gate doing its job:
    fix the code, or re-run it if the failure was environmental.
    """
    os.chdir(repo_root())
    state = load()
    if not a.command:
        die("validate-drop needs a command after `--`")
    cmd = join_cmd(a.command)
    state["fingerprint_current"] = fp_of(state)
    hits = [v for v in state["validation"]
            if v["cmd"] == cmd and v["fingerprint"] == state["fingerprint_current"] and not v.get("retracted")]
    lp = last_pass(state)
    inherited = (not hits and lp is not None and lp["fingerprint"] != state["fingerprint_current"]
                 and cmd in validation_at(state, lp["fingerprint"]))
    if not hits and not inherited:
        print(f"loop-review: no validation record for `{cmd}` at the current state", file=sys.stderr)
        cur = current_validation(state)
        if cur:
            print("recorded commands:", file=sys.stderr)
            for v in cur:
                print(f"  - {v['cmd']}  -> exit {v['exit']}", file=sys.stderr)
        sys.exit(1)
    if inherited:
        # A check carried over from the last pass, not re-run here. `pass-start` only opens
        # a pass on green validation, so anything in that set passed — retiring it weakens
        # the evidence the review was granted on, and that is a human call, never the
        # agent's. The record itself is never rewritten: history stays append-only and the
        # retirement is stamped on the state where it was decided.
        if not a.force:
            die(f"`{cmd}` was green for pass {lp['n']} and has not been re-run on the current state; "
                "re-run it, or retire it with --force (that weakens the evidence set)")
        state["validation"].append({
            "cmd": cmd, "exit": None, "at": now(), "fingerprint": state["fingerprint_current"],
            "retracted": {"at": now(), "reason": a.reason or f"retired: inherited from pass {lp['n']}, not re-run"},
        })
        save(state)
        print(f"retired `{cmd}` — inherited from pass {lp['n']}, not re-run on this state")
        print_validation(current_validation(state))
        return
    ran = [v for v in hits if v["exit"] not in NEVER_RAN]
    if ran and not a.force:
        die(f"`{cmd}` ran and returned exit {ran[-1]['exit']} — that is a result, not a mistake; "
            "fix it or re-run the command (--force to retract anyway)")
    for v in hits:
        v["retracted"] = {"at": now(), "reason": a.reason or ("forced" if a.force else "never ran")}
    save(state)
    print(f"retracted {len(hits)} record(s) for `{cmd}`")
    print_validation(current_validation(state))


def cmd_pass_start(a):
    os.chdir(repo_root())
    state = load()
    state["fingerprint_current"] = fp_of(state)
    lp = last_pass(state)
    if lp and lp.get("result") is None:
        die(f"pass {lp['n']} is still open; record it first")
    n = len(state["passes"]) + 1
    if n > state["max_passes"] and not a.force:
        die(f"pass limit {state['max_passes']} reached — outcome is INCOMPLETE unless the user asked for persistence (then use --force)")
    if lp and lp["fingerprint"] == state["fingerprint_current"] and not a.force:
        die("scoped changes are unchanged since the last pass; clarify with the same reviewer instead of opening a new full review (--force to override)")
    cur = current_validation(state)
    if not cur:
        die("no validation recorded for the current state; run `validate` first")
    if any(v["exit"] != 0 for v in cur):
        die("validation is red for the current state; fix it, or re-run the command if the failure was environmental "
            "(if it never ran at all — typo, missing tool — retract it with `validate-drop`)")
    missing = missing_since_last_pass(state)
    if missing and not a.force:
        die(f"pass {lp['n']} rested on {len(missing)} check(s) not re-run since the change: "
            + "; ".join(f"`{c}`" for c in missing)
            + " — re-run them. A check that never ran here (typo, missing tool) can be retracted with "
              "`validate-drop`; retiring one that was green for the last pass needs "
              "`validate-drop --force`, a human decision.")
    state["passes"].append({"n": n, "opened": now(), "fingerprint": state["fingerprint_current"], "result": None})
    save(state)
    print(f"pass {n}/{state['max_passes']} opened on fingerprint {state['fingerprint_current']}")
    print("scope:")
    for p in state["scope"]:
        print(f"  - {p}")
    print_task_brief(state)
    print_validation(cur, "validation for this state")


def cmd_pass_record(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None:
        die("no open pass to record")
    if lp.get("result") is not None:
        die(f"pass {lp['n']} is already recorded; use `amend` to add output the reviewer supplied afterwards")
    if a.findings < 0:
        # `unresolved = findings - resolved` feeds blockers(); a negative count would read
        # as "already resolved" and pass exit condition 2 without a single fix.
        die(f"--findings must not be negative ({a.findings}); it counts findings the reviewer reported")
    lp["result"] = {
        "score": a.score,
        "findings": a.findings,
        "resolved": 0,
        "understood": bool(a.understood),
        "test_evidence": a.test_evidence,
        "test_score": a.test_score,
        "note": a.note,
        "recorded": now(),
    }
    save(state)
    print(f"pass {lp['n']} recorded: score {a.score}, {a.findings} finding(s), understood={bool(a.understood)}, test evidence {a.test_evidence}")
    if a.findings == 0 and a.score < 9.5:
        print("note: score below 9.5 with zero findings — treat the number as calibration noise; findings control the loop")


def cmd_amend(a):
    """Add output the reviewer supplied on request, without opening a new pass.

    SKILL.md step 4: when a reviewer omits required output (understanding summary, score)
    or turns a vague concern into concrete findings, the agent asks the *same* reviewer in
    the same conversation. That is not a new pass, and the unchanged fingerprint rightly
    blocks `pass-start` — so the recorded pass must be completable in place.

    Amendment may only add information. `--findings` can rise (the reviewer named issues
    it had only hinted at) but never fall: retiring a finding is `resolve`, which demands
    a fix, a withdrawal or an adjudication.
    """
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("result") is None:
        die("no recorded pass to amend")
    r = lp["result"]
    given = {k: v for k, v in (("score", a.score), ("findings", a.findings),
                               ("test_evidence", a.test_evidence),
                               ("test_score", a.test_score), ("note", a.note)) if v is not None}
    if a.understood:
        given["understood"] = True
    if not given:
        die("amend needs at least one of --understood / --test-evidence / --score / --findings / --test-score / --note")
    if "findings" in given and given["findings"] < 0:
        die(f"--findings must not be negative ({given['findings']})")
    if "findings" in given and given["findings"] < r["findings"]:
        die(f"amend cannot lower findings ({r['findings']} -> {given['findings']}); "
            "record a fix, withdrawal or adjudication with `resolve` instead")
    r.update(given)
    r.setdefault("amendments", []).append({**given, "at": now()})
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"pass {lp['n']} amended: " + ", ".join(f"{k}={v}" for k, v in given.items()))
    print(f"pass {lp['n']}: score {r['score']}, {r['resolved']}/{r['findings']} resolved, understood={r['understood']}, test evidence {r.get('test_evidence')}")
    if state["fingerprint_current"] != lp["fingerprint"]:
        print("note: scoped changes moved since this pass — the review is stale; validate and open a new pass")


def cmd_resolve(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("result") is None:
        die("no recorded pass to resolve against")
    counts = (("--fixed", a.fixed), ("--withdrawn", a.withdrawn), ("--adjudicated-invalid", a.adjudicated_invalid))
    # Each flag counts something that happened, so it cannot be negative, and a call that
    # reports nothing is a no-op that would still land in the log as a resolution step.
    for name, value in counts:
        if value < 0:
            die(f"{name} must not be negative ({value}); it counts resolutions that happened")
    total = a.fixed + a.withdrawn + a.adjudicated_invalid
    if total == 0:
        die("resolve needs at least one of --fixed / --withdrawn / --adjudicated-invalid")
    r = lp["result"]
    claimed = r["resolved"] + total
    r["resolved"] = min(r["findings"], claimed)
    r.setdefault("resolution_log", []).append({"fixed": a.fixed, "withdrawn": a.withdrawn, "adjudicated_invalid": a.adjudicated_invalid, "at": now()})
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"pass {lp['n']}: {r['resolved']}/{r['findings']} resolved")
    if claimed > r["findings"]:
        print(f"note: {claimed} resolutions claimed against {r['findings']} finding(s) — clamped; "
              "if the reviewer named more findings than were recorded, `amend --findings` first")
    if state["fingerprint_current"] != lp["fingerprint"]:
        print("scoped changes moved: validate, then open a new pass with a fresh reviewer")


def cmd_accept(a):
    os.chdir(repo_root())
    state = load()
    state["fingerprint_current"] = fp_of(state)
    save(state)
    reasons = blockers(state)
    lp = last_pass(state)
    if reasons:
        print("NOT ACCEPTED:")
        for r in reasons:
            print(f"  - {r}")
        if lp and len(state["passes"]) >= state["max_passes"]:
            print(f"pass limit reached ({state['max_passes']}) — report as INCOMPLETE")
        sys.exit(1)
    print(f"ACCEPTED after {len(state['passes'])} pass(es); last score {lp['result']['score']} (progress signal only)")


def cmd_status(a):
    os.chdir(repo_root())
    state = load()
    state["fingerprint_current"] = fp_of(state)
    state["blockers"] = blockers(state)
    if a.json:
        print(json.dumps(state, indent=2))
        return
    print(f"mode: {state.get('mode', 'changes')}" + (f" vs {state['base']}" if state.get("base") else ""))
    print(f"scope ({len(state['scope'])}): " + ", ".join(state["scope"]))
    print_task_brief(state)
    print(f"passes: {len(state['passes'])}/{state['max_passes']}")
    for p in state["passes"]:
        r = p.get("result")
        if r:
            print(f"  #{p['n']}  score {r['score']}  findings {r['resolved']}/{r['findings']} resolved  understood={r['understood']}  test-evidence={r.get('test_evidence')}  test-score={r.get('test_score')}")
        else:
            print(f"  #{p['n']}  (open)")
    print_validation(current_validation(state))
    print("blockers: " + ("none — ready to accept" if not state["blockers"] else "; ".join(state["blockers"])))


def cmd_fingerprint(a):
    os.chdir(repo_root())
    print(fingerprint(load()["scope"]))


def cmd_reset(a):
    os.chdir(repo_root())
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    try:
        os.rmdir(STATE_DIR)
    except OSError:
        # Empty-only by design: rmdir refuses a non-empty directory, and the script has no
        # business deleting anything a user put in there. Missing directory is fine too.
        ours = {os.path.basename(PROJECT_FILE), os.path.basename(FINDINGS_DIR)}
        if os.path.isdir(STATE_DIR) and not set(os.listdir(STATE_DIR)) <= ours:
            print(f"note: {STATE_DIR}/ kept — it holds files this script did not create")
    print("state cleared")


# ---------- project mode ----------

def load_project():
    if not os.path.exists(PROJECT_FILE):
        die("no project review; run `project init` first")
    with open(PROJECT_FILE) as f:
        try:
            return json.load(f)
        except ValueError as e:
            die(f"{PROJECT_FILE} is not valid JSON ({e})")


def save_project(p):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PROJECT_FILE, "w") as f:
        json.dump(p, f, indent=2)


def area_paths(x):
    # Ledgers written before multi-path areas store a single "path" string.
    return x["paths"] if "paths" in x else [x["path"]]


def area_name(x):
    return " + ".join(area_paths(x))


def area_slug(x):
    return "__".join(p.strip("/").replace("/", "__") for p in area_paths(x)) or "root"


def parse_areas(raw, root):
    """`--` paths form areas; a standalone `+` glues neighbours into one multi-path area.

    `a b + c d` -> [a], [b, c], [d]. A flat package split across files, or sibling edge
    functions grouped by meaning, are one area even though they are several paths.
    """
    areas, cur = [], []
    for tok in raw:
        if tok == "+":
            if not cur:
                die("`+` must follow a path")
            continue_last = True
        else:
            continue_last = bool(cur) and prev == "+"
            if not continue_last and cur:
                areas.append(cur)
                cur = []
            cur.append(to_repo_relative(tok, root))
        prev = tok
    if cur:
        areas.append(cur)
    out, seen = [], set()
    for area in areas:
        key = tuple(dict.fromkeys(area))
        if key not in seen:
            seen.add(key)
            out.append(list(key))
    return out


def cmd_project_init(a):
    """Queue a whole-project review, one loop per area, in order.

    An area is one or more paths one reviewer reads as a whole: `-- a b + c d` queues
    [a], [b, c], [d]. The five-pass limit is per area, not per project: a project is
    finished when every area has an outcome, however many loops that takes. Areas are
    decided by the agent from the repository structure — the script only keeps the ledger.
    """
    if not a.paths:
        die("project init needs at least one area path after `--`")
    root = repo_root()
    areas = parse_areas(a.paths, root)
    os.chdir(root)
    if os.path.exists(PROJECT_FILE) and not a.force:
        die("project review already active; `project status` to continue it, or --force to start over")
    if os.path.exists(STATE_FILE):
        die("an area loop is still open (state.json); `project close` or `reset` it first")
    p = {
        "created": now(),
        "max_passes": a.max_passes,
        "report_only": bool(a.report_only),
        "areas": [{"paths": x, "status": "pending"} for x in areas],
    }
    save_project(p)
    print(f"project review queued: {len(areas)} area(s), {a.max_passes} passes each, "
          + ("report-only (no fixes)" if a.report_only else "fix mode"))
    for x in areas:
        print(f"  - {' + '.join(x)}")


def cmd_project_next(a):
    os.chdir(repo_root())
    p = load_project()
    if os.path.exists(STATE_FILE):
        die("an area loop is still open; `project close` it before moving on")
    cur = next((x for x in p["areas"] if x["status"] == "in_progress"), None)
    if cur:
        die(f"area `{area_name(cur)}` is in_progress but has no state; `project close` it or `project init --force`")
    nxt = next((x for x in p["areas"] if x["status"] == "pending"), None)
    if nxt is None:
        print("no pending areas — project review complete; `project status` for the ledger")
        return
    nxt["status"] = "in_progress"
    nxt["started"] = now()
    save_project(p)
    state = {
        "created": now(), "mode": "project", "base": None, "scope": area_paths(nxt),
        "max_passes": p["max_passes"], "passes": [], "validation": [],
    }
    state["fingerprint_current"] = fp_of(state)
    save(state)
    done = sum(1 for x in p["areas"] if x["status"] not in ("pending", "in_progress"))
    print(f"area {done + 1}/{len(p['areas'])}: {area_name(nxt)}  (fingerprint {state['fingerprint_current']})")
    print(f"findings file: {os.path.join(FINDINGS_DIR, area_slug(nxt) + '.md')}")
    if p["report_only"]:
        print("report-only: record the pass, write the findings file, then `project close` — do not fix")


def cmd_project_close(a):
    """Record the outcome of the current area from the loop state, then free the loop.

    fix mode:    accepted when blockers() is empty, else incomplete with the blockers.
    report-only: reviewed when a pass is recorded with a credible understanding and its
                 findings (if any) are written to the findings file; the loop's own
                 acceptance is not required because nothing is meant to be fixed.
    """
    os.chdir(repo_root())
    p = load_project()
    cur = next((x for x in p["areas"] if x["status"] == "in_progress"), None)
    if cur is None:
        die("no area in progress")
    if not os.path.exists(STATE_FILE):
        # The loop state vanished mid-area (crash, manual reset). The area cannot be judged;
        # with --force it is closed as incomplete so the queue can move on and it stays
        # visible in the ledger for a re-run.
        if not a.force:
            die("no loop state for the current area — it was lost mid-loop; "
                "`project close --force` marks it incomplete and continues the queue")
        cur.update({"status": "incomplete", "blockers": ["loop state lost mid-area"], "closed": now(),
                    "passes": None, "findings": None, "resolved": None, "score": None, "findings_file": None})
        save_project(p)
        print(f"area `{area_name(cur)}` closed: incomplete — loop state lost")
        return
    state = load()
    state["fingerprint_current"] = fp_of(state)
    lp = last_pass(state)
    ffile = os.path.join(FINDINGS_DIR, area_slug(cur) + ".md")
    r = lp.get("result") if lp else None
    if p["report_only"]:
        problems = []
        if r is None:
            problems.append("no recorded pass")
        else:
            if not r["understood"]:
                problems.append("reviewer did not demonstrate a credible understanding")
            if r["findings"] > 0 and not os.path.exists(ffile):
                problems.append(f"{r['findings']} finding(s) reported but {ffile} is missing")
        if problems and not a.force:
            die("cannot close area: " + "; ".join(problems) + " (--force to close as incomplete)")
        cur["status"] = "reviewed" if not problems else "incomplete"
        cur["blockers"] = problems
    else:
        reasons = blockers(state)
        cur["status"] = "accepted" if not reasons else "incomplete"
        cur["blockers"] = reasons
        if r and r["findings"] - r["resolved"] > 0 and not os.path.exists(ffile):
            print(f"note: unresolved findings but no {ffile} — write it so the ledger stays readable")
    cur["closed"] = now()
    cur["passes"] = len(state["passes"])
    # Totals across every pass of this area: a fixed finding from pass 1 is part of the
    # area's story even though pass 2 reports zero.
    recorded = [x["result"] for x in state["passes"] if x.get("result")]
    cur["findings"] = sum(x["findings"] for x in recorded) if recorded else None
    cur["resolved"] = sum(x["resolved"] for x in recorded) if recorded else None
    cur["score"] = r["score"] if r else None
    cur["findings_file"] = ffile if os.path.exists(ffile) else None
    save_project(p)
    os.remove(STATE_FILE)
    print(f"area `{area_name(cur)}` closed: {cur['status']}"
          + (f" — {'; '.join(cur['blockers'])}" if cur["blockers"] else ""))
    left = sum(1 for x in p["areas"] if x["status"] == "pending")
    print(f"{left} area(s) pending" if left else "all areas closed — `project status` for the ledger")


def cmd_project_status(a):
    os.chdir(repo_root())
    p = load_project()
    if a.json:
        print(json.dumps(p, indent=2))
        return
    counts = {}
    for x in p["areas"]:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    print("project review: " + ("report-only" if p["report_only"] else "fix mode")
          + f", {p['max_passes']} passes/area — " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for x in p["areas"]:
        line = f"  [{x['status']:<11}] {area_name(x)}"
        if x.get("passes") is not None:
            line += f"  passes {x['passes']}  findings {x.get('resolved')}/{x.get('findings')}  score {x.get('score')}"
        if x.get("blockers"):
            line += "  — " + "; ".join(x["blockers"])
        print(line)


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(prog="loop_review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--max-passes", type=int, default=5); s.add_argument("--mode", choices=MODES, default="changes"); s.add_argument("--base", metavar="REF", help="commit to diff against (changes mode); use for already-committed work"); s.add_argument("--task-brief", metavar="TEXT", help="the task's requirements and acceptance criteria, for the reviewer — never rationale or suspected issues"); s.add_argument("--task-brief-file", metavar="PATH", help="same, read from a file"); s.add_argument("--allow-empty", action="store_true", help="proceed on an empty scoped diff instead of reporting `no-changes`"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("validate"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("validate-drop"); s.add_argument("--force", action="store_true"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate_drop)
    s = sub.add_parser("pass-start"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_pass_start)
    s = sub.add_parser("pass-record"); s.add_argument("--score", type=float, required=True); s.add_argument("--findings", type=int, required=True); s.add_argument("--test-evidence", choices=TEST_EVIDENCE, required=True); s.add_argument("--understood", action="store_true"); s.add_argument("--test-score", type=float); s.add_argument("--note"); s.set_defaults(fn=cmd_pass_record)
    s = sub.add_parser("amend"); s.add_argument("--understood", action="store_true"); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--score", type=float); s.add_argument("--findings", type=int); s.add_argument("--test-score", type=float); s.add_argument("--note"); s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("resolve"); s.add_argument("--fixed", type=int, default=0); s.add_argument("--withdrawn", type=int, default=0); s.add_argument("--adjudicated-invalid", type=int, default=0); s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("accept"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fingerprint"); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser("reset"); s.set_defaults(fn=cmd_reset)

    pr = sub.add_parser("project").add_subparsers(dest="pcmd", required=True)
    s = pr.add_parser("init"); s.add_argument("--max-passes", type=int, default=5); s.add_argument("--report-only", action="store_true", help="collect findings per area without fixing"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_project_init)
    s = pr.add_parser("next"); s.set_defaults(fn=cmd_project_next)
    s = pr.add_parser("close"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_project_close)
    s = pr.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_project_status)

    # Paths and validation commands can carry undecodable bytes (surrogates from
    # os.fsdecode / argv). Printing them must not be the thing that kills the loop.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")

    # allow "--" separated trailing args for init/validate
    argv = sys.argv[1:]
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
