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
       [--task-brief TEXT | --task-brief-file PATH] [--scope-note TEXT] -- <paths...>
                                           start a loop over paths (task diff or whole area)
  project init [--max-passes N] [--report-only] [--allow-empty] -- <area paths...>
                                           queue a whole-project review, one loop per area
  project next                             open the loop for the next pending area
  project close                            record the current area's outcome, free the loop
  project status [--json]                  ledger of areas and outcomes
  validate -- <command...>                 run a validation command, record result
  validate --none --reason TEXT            record that no executable check applies here
  scope -- <paths...>                      widen the open loop's scope (never narrows it)
  validate-drop [--force] -- <command...>  retract a record for a command that never ran,
                                           or (--force) retire a check inherited from the last pass
  pass-start                               open a full-review pass (checks gates)
  pass-abort [--reason TEXT]               discard an open pass whose review went stale
  brief [--force] [--task-brief TEXT | --task-brief-file PATH] [--scope-note TEXT]
                                           record the brief, or the hunk-level scope note,
                                           on a loop started without one
  pass-record --findings N [--score S] [--test-evidence {trusted|justified-absent|inadequate}]
              [--understood] [--test-score T] [--note TEXT]
                                           only --findings is required: a reviewer that
                                           omitted a verdict must not force you to invent
                                           one — record what it gave, then `amend` the rest
  amend [--understood] [--test-evidence V] [--score S] [--findings N] [--test-score T] [--note TEXT]
                                           add missing reviewer output to the last pass
  resolve [--fixed N] [--withdrawn N] [--adjudicated-invalid N]
  accept                                   exit 0 if accepted, else 1 + reasons
  status [--json]
  fingerprint                              print hash of the scoped diff
  reset [--project]                        delete the loop state (and the project ledger)
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

# The recorded stand-in for "no executable check applies here", kept distinct from any real
# command so it can never be confused with one that ran.
NO_CHECK = "<no executable check applies>"

TEST_EVIDENCE = ("trusted", "justified-absent", "inadequate")
TEST_EVIDENCE_BLOCKING = "inadequate"

# `init` on an empty scoped diff is not a failure of the tool, it is an outcome of the
# workflow: there is nothing task-owned to review. It gets its own exit code so the agent
# can tell it apart from a usage error and report `no-changes` instead of looping blind.
NO_CHANGES = 2


STATE_DIR = ".loop-review"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PROJECT_FILE = os.path.join(STATE_DIR, "project.json")
FINDINGS_DIR = os.path.join(STATE_DIR, "findings")
MODES = ("changes", "project")


# ---------- helpers ----------

def positive_int(v):
    """argparse type: a count that must be at least 1."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not an integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def nonneg_int(v):
    """argparse type: a count of things that happened, so never negative."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not an integer")
    if n < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {n}")
    return n


def score_value(v):
    """argparse type: the 1-10 progress signal. Range only - the score gates nothing."""
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number")
    if not 1.0 <= f <= 10.0:
        raise argparse.ArgumentTypeError(f"must be within the 1-10 anchors, got {f}")
    return f


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
    """Write the state atomically: temp file in the same directory, then os.replace.

    state.json is the whole loop - scope, brief, passes, findings, validation history. A
    partial write from a Ctrl-C or a full disk would leave it truncated, and every later
    command would refuse to load it, so an interrupted run would cost the entire review
    history. os.replace is atomic on POSIX and Windows, so readers see either the old file
    or the new one, never a half-written one.
    """
    atomic_json(STATE_FILE, state)


def atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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


def outside_state_dir(path):
    """True when a raw bytes path from git is not inside the loop's own state directory.

    The exclusion is applied here, in Python, and never by a `:(exclude)` pathspec, because
    `git ls-files --cached` truncates every pathspec item by the common directory prefix of
    the *positive* items — negative ones do not contribute to that prefix. For a scope like
    `apps/portal/src/lib` the 16-character prefix is stripped from `:(exclude).loop-review`
    too, the remainder matches everything, and the command returns nothing. That is not a
    cosmetic failure: an area of tracked files then hashes as if it were empty, so a fix
    never moves the fingerprint and `accept` signs off a review of code that no longer
    exists. `:(exclude,top)` does not survive it either (verified, git 2.50.1).
    """
    p = os.fsdecode(path)
    return p != STATE_DIR and not p.startswith(STATE_DIR + os.sep) and not p.startswith(STATE_DIR + "/")


def scoped(paths):
    """Pathspecs for `git diff`, where a `:(exclude)` item is safe and does what it says.

    Excluded because every command writes `state.json`: if `.loop-review/` is inside the
    reviewed scope, recording a validation result is itself a change to the reviewed state,
    the record lands on the fingerprint computed after the write, and `pass-start` then
    demands a validation that can never exist. Reviewing a repository from its root
    (`init -- .`, `project init -- .`) is an ordinary thing to ask for, so the state machine
    excludes its own bookkeeping rather than relying on the reviewed repository to gitignore
    it. `ls-files` callers must use `outside_state_dir()` instead — see there.
    """
    return [*paths, f":(exclude,glob){STATE_DIR}/**", f":(exclude){STATE_DIR}"]


def fingerprint(paths, mode="changes", base=None):
    """Hash identifying the reviewed state, scoped to paths.

    changes: diff of the worktree against `base` (HEAD by default) plus untracked contents —
             the task's own edits. Empty when the work is already committed and no base is
             given; pass `init --base <ref>` for a committed task.
    project: contents of every tracked and untracked file under the paths. The area is
             reviewed as it is, so its identity is its content, not a diff.
    """
    h = hashlib.sha256()

    def field(b):
        """Feed one length-prefixed field, so the boundary between fields is unambiguous.

        Concatenating a path with its contents lets distinct states hash alike: file `a`
        holding "bc" and file `ab` holding "c" both produce b"abc". Two different states
        with one fingerprint is the gate-breaking lie — `pass-start` would call the change
        unchanged, and a stale review would still count as current.
        """
        h.update(str(len(b)).encode() + b"\0" + b)

    if mode == "project":
        out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths)
        for p in sorted({p for p in out.split(b"\0") if p and outside_state_dir(p)}):
            field(p)
            try:
                with open(p, "rb") as f:
                    field(f.read())
            except OSError as e:
                field(f"<unreadable:{e.errno}>".encode())
        return h.hexdigest()[:16]
    # check=True on both git calls: a failure here must be loud. A silently empty hash is
    # indistinguishable from "nothing changed", which is the one lie that breaks every gate.
    field(git("diff", base or diff_base(), "--", *scoped(paths)))
    # -z: NUL-separated and unquoted, so paths with spaces, newlines or non-ASCII
    # characters survive intact. Splitting plain `ls-files` output would corrupt them
    # and silently drop those files from the hash.
    out = git("ls-files", "-z", "--others", "--exclude-standard", "--", *paths)
    # Kept as bytes: a filename need not be decodable, and open() takes bytes paths.
    untracked = [p for p in out.split(b"\0") if p and outside_state_dir(p)]
    for p in sorted(untracked):
        field(p)
        try:
            with open(p, "rb") as f:
                field(f.read())
        except OSError as e:
            # Fold the failure in: an unreadable file must not hash like an empty one.
            field(f"<unreadable:{e.errno}>".encode())
    return h.hexdigest()[:16]


def unseen_paths(paths):
    """Scope paths git has no record of: ignored, or simply not there.

    Emptiness is asked of the scope as a whole, so one such path hides behind its healthy
    neighbours: it contributes nothing to the fingerprint, and rewriting it therefore leaves
    the review "current". A path the operator named as task-owned must not be invisible to
    every gate without a word.
    """
    out = []
    for p in paths:
        listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", p)
        if not [x for x in listed.split(b"\0") if x and outside_state_dir(x)]:
            out.append((p, "exists but is ignored by git" if os.path.exists(p) else "does not exist"))
    return out


def warn_unseen(paths):
    unseen = unseen_paths(paths)
    for p, why in unseen:
        print(f"warning: `{p}` {why} — it contributes nothing to the fingerprint, so changes "
              "to it will not make the review stale", file=sys.stderr)
    return unseen


def scope_is_empty(paths, mode="changes", base=None):
    """True when the scope contains nothing to review.

    Asked structurally, never by comparing fingerprints. The hash format is an internal
    detail — adding length-prefixed fields already changed the hash of an empty `changes`
    scope once — and a constant compared against it turns into a silent no-op the moment
    the format moves, precisely where a wrong answer disables every gate. Comparing against
    an unscoped `fingerprint([])` is worse still: that hashes the whole repository's diff,
    which is byte-identical to the scoped one whenever the scope is the only thing changed.

    `changes`: empty when the diff against the base has no bytes and no untracked file
    falls inside the scope. `project`: empty when the paths match no file at all — a
    mistyped or renamed area, which would otherwise be queued, reviewed by nobody and
    closed as accepted.
    """
    def listed(*flags):
        out = git("ls-files", "-z", *flags, "--exclude-standard", "--", *paths)
        return [p for p in out.split(b"\0") if p and outside_state_dir(p)]

    if mode == "project":
        return not listed("--cached", "--others")
    if git("diff", base or diff_base(), "--", *scoped(paths)).strip():
        return False
    return not listed("--others")


def fp_of(state):
    return fingerprint(state["scope"], state.get("mode", "changes"), state.get("base"))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def last_pass(state):
    return state["passes"][-1] if state["passes"] else None


def open_pass(state):
    """The last pass if it is still awaiting a result. An aborted one is finished, not open."""
    lp = last_pass(state)
    return lp if lp is not None and lp.get("result") is None and not lp.get("aborted") else None


def last_recorded_pass(state):
    """The last pass that produced a reviewer result — what acceptance is judged on."""
    return next((x for x in reversed(state["passes"]) if x.get("result")), None)


def validation_at(state, fp):
    """Validation runs at fingerprint `fp`, latest non-retracted result per command.

    Re-running the same command on unchanged files supersedes its earlier result, so a
    flaky or environment failure can be cleared by re-running it — not only by editing.
    Distinct commands are tracked separately: a green re-run of the tests does not
    absolve a red lint.
    """
    latest = {}
    for v in state["validation"]:
        if v["fingerprint"] == fp and not v.get("retracted") and not v.get("moved_scope"):
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
    lp = last_recorded_pass(state)
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


def red_provenance(state, cmd):
    """`inherited` if this command has never been green in this loop, else `regressed`.

    Project mode opens a pass over a failing check because an inherited area is reviewed as
    it stands. But that licence belongs to a check that was already failing: one the loop's
    own fix batch broke is a regression, and telling a fresh reviewer it is "a property of
    the area you inherited" hands it the wrong conclusion about the very code it is there to
    judge. The loop's own history answers this, so the agent never has to remember it.
    """
    for v in state["validation"]:
        if v["cmd"] == cmd and v["exit"] == 0 and not v.get("retracted"):
            return "regressed"
    return "inherited"


def print_validation(cur, header="validation on current state", state=None):
    """One rendering of validation state for every text-mode command.

    `references/reviewer-prompt.md` is filled from `status`, and its validation section
    needs each command with its exit code — an aggregate GREEN/RED line cannot be
    transcribed into the reviewer's prompt.
    """
    print(f"{header}: {len(cur)} command(s), " +
          ("GREEN" if cur and all(v["exit"] == 0 for v in cur) else "RED/none"))
    for v in cur:
        if v["cmd"] == NO_CHECK and v.get("reason"):
            print(f"  - no executable check applies: {v['reason']}")
        elif v["exit"] != 0 and state is not None:
            print(f"  - {v['cmd']}  -> exit {v['exit']}  [{red_provenance(state, v['cmd'])}]")
        else:
            print(f"  - {v['cmd']}  -> exit {v['exit']}")


def blockers(state):
    """Return list of reasons acceptance is not possible. Empty list == accepted."""
    reasons = []
    # Checked here, not only at `init`. A scope can empty *after* the loop starts — the work
    # is committed, a change is reverted or stashed, an area's directory is deleted — and
    # from that moment the loop reviews nothing while passing every gate exactly as it would
    # on an empty `init`. In project mode the lie then outlives the loop in the ledger.
    if not state.get("allow_empty") and scope_is_empty(state["scope"], state.get("mode", "changes"),
                                                       state.get("base")):
        reasons.append("the scope is empty now — nothing is under review "
                       "(reverted, stashed, committed, or the area's files are gone)")
    op = open_pass(state)
    if op:
        reasons.append(f"pass {op['n']} opened but not recorded (pass-record, or pass-abort if it is stale)")
        return reasons
    lp = last_recorded_pass(state)
    if lp is None:
        reasons.append("no full-review pass recorded")
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
    elif lp["result"]["resolved"]:
        # Fixes were recorded on some state; if the scope is back on the fingerprint the
        # review found defective, they are not on disk any more.
        fixed_on = {x.get("fingerprint") for x in lp["result"].get("resolution_log", [])
                    if x.get("fixed")}
        if fixed_on and state["fingerprint_current"] == lp["fingerprint"] \
                and lp["fingerprint"] not in fixed_on:
            reasons.append("the scope is back on the state the review found defective — the "
                           "recorded fixes are not present (reverted, stashed or checked out)")
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
    # Read the brief before the chdir below: a relative --task-brief-file names a file next
    # to the caller, exactly like a scope path, and resolving it afterwards silently reads a
    # same-named file at the repository root instead. Briefing every reviewer of the loop
    # identically with the wrong document is worse than recording no brief at all.
    brief = read_task_brief(a)
    # Deduplicate: `f.py ./f.py f.py` is one path. git would collapse it anyway, but the
    # list is echoed into the reviewer prompt and must read as the agent meant it.
    scope = list(dict.fromkeys(to_repo_relative(p, root) for p in a.paths))
    os.chdir(root)
    if os.path.exists(STATE_FILE) and not a.force:
        die("loop already active; use `reset` or `--force`")
    # `--force` means "replace the loop", but inside a project area it also detaches the area
    # from its ledger: the replacement loop can be a `changes` diff of anything, and
    # `project close` would then record that area `accepted` from a review of something else.
    # `reset` already refuses this exact situation; so does `project init`.
    if os.path.exists(PROJECT_FILE) and os.path.exists(STATE_FILE):
        try:
            with open(PROJECT_FILE, encoding="utf-8") as f:
                busy = next((x for x in json.load(f)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            busy = None
        if busy:
            die(f"area `{area_name(busy)}` is mid-loop — `project close` records its outcome "
                "first. Re-initialising here would close that area from a review of something "
                "else; `--force` does not cover that.")
    if (a.task_brief or a.task_brief_file) and a.mode == "project":
        # An inherited area has no task and therefore no acceptance criteria to miss;
        # a brief here would only be the operator's opinion smuggled past isolation.
        die("--task-brief applies to --mode changes only")
    base = None
    if a.base:
        if a.mode == "project":
            die("--base applies to --mode changes only")
        resolved = git("rev-parse", "--verify", "--quiet", a.base + "^{commit}", check=False).strip()
        if not resolved:
            die(f"--base {a.base} is not a commit in this repository")
        # Pinned to the commit id, never kept as the symbolic form the operator typed.
        # `HEAD~1` is relative to HEAD: the moment anything is committed — by the workflow at
        # the user's request, or by the user in another terminal — it names a different
        # commit, the scoped diff silently narrows to the newest work, and `accept` signs off
        # a task most of which no reviewer ever saw.
        base = os.fsdecode(resolved)
        if base != a.base:
            print(f"base {a.base} pinned to {base[:12]}", file=sys.stderr)
    state = {
        "created": now(),
        "mode": a.mode,
        "base": base,
        "scope": scope,
        "allow_empty": bool(a.allow_empty),
        "task_brief": brief,
        "scope_note": (a.scope_note or "").strip() or None,
        "max_passes": a.max_passes,
        "passes": [],
        "validation": [],
    }
    state["fingerprint_current"] = fp_of(state)
    # Refuse rather than warn, in both modes. A loop on an empty scope passes every gate —
    # validation is green because nothing runs against a change, the fingerprint never
    # moves, and `accept` sees no blocker — so the one thing the script exists to prevent
    # is exactly what an empty scope produces. In `changes` mode the workflow outcome is
    # `no-changes`; in `project` mode an empty area means a path that matches no file, and
    # closing it as reviewed would put a lie in the ledger.
    if scope_is_empty(scope, a.mode, a.base):
        if not a.allow_empty:
            if a.mode == "project":
                die("the scoped area matches no file — check the paths; an area nobody can read "
                    "would still pass every gate. --allow-empty to proceed anyway.", code=NO_CHANGES)
            die("no task-owned changes in scope — outcome is `no-changes`. If the task is already "
                "committed, re-run with --base <ref> (e.g. the parent of the first task commit). "
                "If the paths are wrong, fix them. Only pass --allow-empty when you intend a loop "
                "whose every gate is blind.", code=NO_CHANGES)
        print("warning: the scope is empty and --allow-empty was given; every gate is blind",
              file=sys.stderr)
    warn_unseen(scope)
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


def print_scope_note(state):
    note = state.get("scope_note")
    if note:
        print("scope note (repeat verbatim in the reviewer's hunk fields):")
        for line in note.splitlines():
            print(f"  | {line}")


def print_task_brief(state):
    brief = state.get("task_brief")
    if brief:
        print("task brief (give this to the reviewer verbatim):")
        for line in brief.splitlines():
            print(f"  | {line}")
    elif state.get("mode", "changes") == "changes":
        # Silent in project mode: an inherited area has no task, so no brief is missing.
        print("task brief: none recorded — the reviewer cannot report a missed requirement; "
              "record one with `brief --task-brief \"...\"` if the task has acceptance criteria")


def join_cmd(parts):
    """The state key for a validation command. `validate` and `validate-drop` must build
    it identically, or a retraction would silently miss the record it names."""
    return " ".join(parts) if len(parts) == 1 else shlex.join(parts)


def cmd_scope(a):
    """Widen the scope of the open loop.

    A reviewer routinely points at the cause in a neighbouring file, and the fix lands
    outside the recorded scope. That fix was invisible to every gate: the fingerprint never
    moved, so the review stayed "current", and `accept` signed off code no reviewer had
    seen. Rebuilding the loop with `init --force` was the only mechanical answer and it
    destroys the pass history. Widening keeps the history and moves the fingerprint, which
    is what makes the next pass mandatory rather than optional.

    Only widening: dropping a path would retire evidence a recorded pass rested on, which is
    what `--force` exists for elsewhere and is a human decision.
    """
    if not a.paths:
        die("scope needs at least one path after `--`")
    root = repo_root()
    added = [to_repo_relative(x, root) for x in a.paths]
    os.chdir(root)
    state = load()
    new = [x for x in dict.fromkeys(added) if x not in state["scope"]]
    if not new:
        die("every path given is already in scope")
    warn_unseen(new)
    before = fp_of(state)
    state["scope"] = state["scope"] + new
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"scope widened by {len(new)} path(s):")
    for x in new:
        print(f"  + {x}")
    if state["fingerprint_current"] != before:
        print("the reviewed state moved; the last review is now stale — re-validate and open "
              "a new pass so a reviewer actually sees the added paths")


def cmd_validate(a):
    os.chdir(repo_root())
    state = load()
    if a.none:
        # An inherited area of prose, config or fixtures has no test, typecheck, lint or
        # build. `pass-start` refuses without a validation record and `--force` does not
        # cover that refusal, so the only remaining move was `validate -- true`: a green
        # record that satisfies exit condition 1 vacuously AND is transcribed into the
        # reviewer's prompt as a check the author claims to have run. Recording the absence
        # honestly is strictly better — the gate is satisfied, and everyone downstream can
        # see there was nothing to run and why.
        if not a.reason:
            die("`validate --none` needs --reason: say why no executable check applies here")
        state["fingerprint_current"] = fp_of(state)
        state["validation"].append({"cmd": NO_CHECK, "exit": 0, "at": now(),
                                    "fingerprint": state["fingerprint_current"],
                                    "reason": a.reason})
        save(state)
        print(f"recorded: no executable check applies — {a.reason}")
        print("tell the reviewer exactly this in the validation section of its prompt; "
              "do not present it as a check that passed")
        return
    if not a.command:
        die("validate needs a command after `--`, or `--none --reason ...` when none applies")
    cmd = join_cmd(a.command)
    before = fp_of(state)
    print(f"$ {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True)
    after = fp_of(state)
    # A formatter, a snapshot-updating test run or a codegen step edits the scope while it
    # validates it. Recording the result against `after` would attach green evidence to a
    # state nothing ran on, and `pass-start` would then open the pass on it — manufacturing
    # the one thing the fingerprint exists to forbid. Neither end is evidence, so the record
    # is kept, marked, and ignored by the gate until the command is re-run on a settled
    # state. Recording it against `before` would be worse: that state no longer exists.
    moved = before != after
    state["fingerprint_current"] = after
    rec = {"cmd": cmd, "exit": r.returncode, "at": now(), "fingerprint": after}
    if moved:
        rec["moved_scope"] = True
        rec["fingerprint_before"] = before
    state["validation"].append(rec)
    save(state)
    if moved:
        print(f"loop-review: the command changed the scope ({before} -> {after}); this run is not "
              "evidence for either state — re-run it now that the files have settled",
              file=sys.stderr)
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
    lp = last_recorded_pass(state)
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
        print_validation(current_validation(state), state=state)
        return
    ran = [v for v in hits if v["exit"] not in NEVER_RAN]
    if ran and not a.force:
        die(f"`{cmd}` ran and returned exit {ran[-1]['exit']} — that is a result, not a mistake; "
            "fix it or re-run the command (--force to retract anyway)")
    for v in hits:
        v["retracted"] = {"at": now(), "reason": a.reason or ("forced" if a.force else "never ran")}
    save(state)
    print(f"retracted {len(hits)} record(s) for `{cmd}`")
    print_validation(current_validation(state), state=state)


def cmd_pass_start(a):
    os.chdir(repo_root())
    state = load()
    state["fingerprint_current"] = fp_of(state)
    op = open_pass(state)
    if op:
        die(f"pass {op['n']} is still open; record it, or `pass-abort --reason ...` if the "
            "scope moved while the reviewer was reading and its review is of a state that "
            "no longer exists")
    lp = last_recorded_pass(state)
    n = len(state["passes"]) + 1
    if n > state["max_passes"] and not a.force:
        die(f"pass limit {state['max_passes']} reached — outcome is INCOMPLETE unless the user asked for persistence (then use --force)")
    if lp and lp["fingerprint"] == state["fingerprint_current"] and not a.force:
        die("scoped changes are unchanged since the last pass; clarify with the same reviewer instead of opening a new full review (--force to override)")
    if not state.get("allow_empty") and scope_is_empty(state["scope"],
                                                       state.get("mode", "changes"),
                                                       state.get("base")):
        die("the scope is empty now — nothing is under review. It emptied after `init`: the "
            "work was committed (re-`init` with `--base`), reverted or stashed, or the area's "
            "files are gone. A pass over nothing is not a review.", code=NO_CHANGES)
    cur = current_validation(state)
    if not cur:
        die("no validation recorded for the current state; run `validate` first")
    red = [v for v in cur if v["exit"] != 0]
    # A 126/127 record means the command never executed. Project mode tolerates a *failing*
    # check because an inherited area is reviewed as it stands — but a typo or a missing
    # tool is not a property of the area, and letting it through would stamp it `[inherited]`
    # and tell a fresh reviewer that the area's own checks fail. Refuse in every mode.
    never_ran = [v for v in red if v["exit"] in NEVER_RAN]
    if never_ran:
        die("these command(s) never ran (missing tool or typo), so they are evidence about "
            "nothing: " + "; ".join(f"`{v['cmd']}` (exit {v['exit']})" for v in never_ran)
            + " — fix the command and re-run it, or retract the record with `validate-drop`.")
    if red and state.get("mode", "changes") != "project":
        die("validation is red for the current state; fix it, or re-run the command if the failure was environmental "
            "(if it never ran at all — typo, missing tool — retract it with `validate-drop`)")
    if red:
        # An inherited area is reviewed as it stands, so "fix red validation before asking
        # for a review" has no addressee: the failing check is a property of the area, not
        # evidence about a change, and refusing here would make a long-red repository — the
        # kind most likely to be sent for a full review — unreviewable in either project
        # mode. The pass opens and the red checks are stamped on it. Nothing is waved
        # through: in report-only `project close` insists the findings file exists so they
        # are written down, and in fix mode `accept` still refuses on red, so an area whose
        # inherited failures were not fixed closes `incomplete` with them as its blocker.
        print("note: opening a project pass over red validation. Tell the reviewer which is "
              "which — an inherited failure is a property of the area, a regressed one was "
              "broken inside this loop and is about the change:", file=sys.stderr)
        for v in red:
            print(f"  - {v['cmd']} -> exit {v['exit']}  [{red_provenance(state, v['cmd'])}]",
                  file=sys.stderr)
        print("  in report-only write them into the findings file; in fix mode they still "
              "block `accept`, so fix them or close the area incomplete with them as the "
              "blocker", file=sys.stderr)
    missing = missing_since_last_pass(state)
    if missing and not a.force:
        die(f"pass {lp['n']} rested on {len(missing)} check(s) not re-run since the change: "
            + "; ".join(f"`{c}`" for c in missing)
            + " — re-run them. A check that never ran here (typo, missing tool) can be retracted with "
              "`validate-drop`; retiring one that was green for the last pass needs "
              "`validate-drop --force`, a human decision.")
    entry = {"n": n, "opened": now(), "fingerprint": state["fingerprint_current"], "result": None}
    if red:
        entry["opened_over_red"] = [v["cmd"] for v in red]
        entry["red_provenance"] = {v["cmd"]: red_provenance(state, v["cmd"]) for v in red}
    state["passes"].append(entry)
    save(state)
    print(f"pass {n}/{state['max_passes']} opened on fingerprint {state['fingerprint_current']}")
    print("scope:")
    for p in state["scope"]:
        print(f"  - {p}")
    print_task_brief(state)
    print_scope_note(state)
    print_validation(cur, "validation for this state", state)


def cmd_brief(a):
    """Record the task brief on a loop that was started without one.

    `status` and `pass-start` nag for a brief on every call, and until now the only way to
    supply one was `init --force`, which rebuilds the loop and destroys its pass history.
    Setting one is allowed; changing one is not, without --force: every reviewer of a loop
    must be briefed identically, and a brief that moves between passes silently redefines
    what "satisfies the task" means.
    """
    os.chdir(repo_root())
    state = load()
    if state.get("mode", "changes") == "project":
        die("an inherited area has no task and no acceptance criteria; --task-brief is changes-mode only")
    brief = read_task_brief(a)
    note = (a.scope_note or "").strip() or None
    if not brief and not note:
        die("brief needs --task-brief TEXT, --task-brief-file PATH or --scope-note TEXT")
    for field, value, label in (("task_brief", brief, "brief"), ("scope_note", note, "scope note")):
        if value is None:
            continue
        if state.get(field) and not a.force:
            die(f"a {label} is already recorded and every pass of this loop was given it; "
                "--force to replace it, and say so in the report — later passes will have "
                "been reviewed against different terms than earlier ones")
        state[field] = value
    save(state)
    print_task_brief(state)
    print_scope_note(state)


def cmd_pass_abort(a):
    """Discard an open, unrecorded pass.

    The scoped files can move while the reviewer is reading them — the user edits, a
    formatter runs, a background tool writes. That review is of a state that no longer
    exists, so it must not be recorded; but `pass-start` refuses while a pass is open, and
    `pass-record` demands a score, findings and a verdict. Without this command the only
    exits were inventing a result for a review nobody trusts, or `reset`, which throws away
    the whole loop. The aborted pass stays in `state.json` with its reason: it happened, and
    the report should say so.
    """
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("aborted"):
        die("no open pass to abort")
    if lp.get("result") is not None:
        die(f"pass {lp['n']} is already recorded; `pass-abort` only discards an open one")
    lp["result"] = None
    lp["aborted"] = now()
    lp["abort_reason"] = a.reason or "unspecified"
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"pass {lp['n']} aborted ({lp['abort_reason']}); it counts as used. "
          f"{state['max_passes'] - len(state['passes'])} of {state['max_passes']} left")
    if lp["fingerprint"] == state["fingerprint_current"]:
        print("note: the scope has not moved since that pass opened; the next `pass-start` is "
              "still granted, because an aborted pass never counted as recorded", file=sys.stderr)


def cmd_pass_record(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("aborted"):
        die("no open pass to record; `pass-start` opens one")
    if lp.get("result") is not None:
        die(f"pass {lp['n']} is already recorded; use `amend` to add output the reviewer supplied afterwards")
    if a.findings < 0:
        # `unresolved = findings - resolved` feeds blockers(); a negative count would read
        # as "already resolved" and pass exit condition 2 without a single fix.
        die(f"--findings must not be negative ({a.findings}); it counts findings the reviewer reported")
    # --score and --test-evidence are deliberately optional. A reviewer sometimes returns
    # findings without a verdict or a number, and SKILL.md's remedy is to ask it once and
    # `amend`; that is impossible if the pass cannot be recorded until the missing values
    # exist. The alternative was inventing them, which is the one thing this loop exists to
    # stop. A missing test-evidence verdict already blocks acceptance in blockers(); a
    # missing score blocks nothing, because the score gates nothing.
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
    print(f"pass {lp['n']} recorded: score {a.score if a.score is not None else 'not given'}, "
          f"{a.findings} finding(s), understood={bool(a.understood)}, "
          f"test evidence {a.test_evidence or 'not assessed'}")
    if a.findings == 0 and a.score is not None and a.score < 9.5:
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
    lp = last_recorded_pass(state)
    if lp is None:
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
    lp = last_recorded_pass(state)
    if lp is None:
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
    state["fingerprint_current"] = fp_of(state)
    # Stamped with the state the fixes were made on. `resolved` was the one counter bound to
    # nothing, so reverting a fix batch (`git checkout --`, `stash`) put the scope back on the
    # reviewed fingerprint with the findings still counted resolved — and `accept` signed off
    # code the reviewer had called defective.
    r.setdefault("resolution_log", []).append(
        {"fixed": a.fixed, "withdrawn": a.withdrawn, "adjudicated_invalid": a.adjudicated_invalid,
         "at": now(), "fingerprint": state["fingerprint_current"]})
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
    lp = last_recorded_pass(state)
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
    print(f"repository: {os.getcwd()}")
    print(f"scope ({len(state['scope'])}): " + ", ".join(state["scope"]))
    print_task_brief(state)
    print_scope_note(state)
    print(f"passes: {len(state['passes'])}/{state['max_passes']}")
    for p in state["passes"]:
        r = p.get("result")
        if r:
            print(f"  #{p['n']}  score {r['score']}  findings {r['resolved']}/{r['findings']} resolved  understood={r['understood']}  test-evidence={r.get('test_evidence')}  test-score={r.get('test_score')}")
        elif p.get("aborted"):
            print(f"  #{p['n']}  (aborted: {p.get('abort_reason', 'unspecified')})")
        else:
            print(f"  #{p['n']}  (open)")
    print_validation(current_validation(state), state=state)
    print("blockers: " + ("none — ready to accept" if not state["blockers"] else "; ".join(state["blockers"])))


def cmd_fingerprint(a):
    os.chdir(repo_root())
    # fp_of, not fingerprint(scope): the bare helper defaults to `changes` against HEAD, so
    # in project mode or with --base it printed a hash no gate ever computes — reading as
    # "the scope is empty" or "the state moved" for a perfectly healthy loop.
    print(fp_of(load()))


def cmd_reset(a):
    os.chdir(repo_root())
    # An area frees its loop with `project close`, never with `reset`. Resetting inside one
    # leaves the ledger pointing at a loop whose state is gone, and the documented recovery
    # (`project close --force`) then records an area that passed every gate as `incomplete`
    # with its passes, score and verdict all null. Refuse instead of losing the outcome.
    if os.path.exists(PROJECT_FILE) and os.path.exists(STATE_FILE):
        try:
            with open(PROJECT_FILE, encoding="utf-8") as f:
                busy = next((x for x in json.load(f)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            busy = None
        if busy:
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    open_loop = json.load(f)
            except (OSError, ValueError):
                open_loop = {}
            # Only that area's own loop is worth protecting. A loop from another scope holds
            # no outcome for this area, and refusing to clear it was one half of a deadlock:
            # `project close` would not record from it either, so neither command could move.
            if loop_is_area(open_loop, busy):
                die(f"area `{area_name(busy)}` is mid-loop — `project close` records its outcome "
                    "and frees the loop, or `project close --force` settles it `incomplete`. "
                    "`reset` here would discard the outcome instead.")
    if a.project:
        if os.path.exists(PROJECT_FILE):
            os.remove(PROJECT_FILE)
            print("project ledger cleared")
        else:
            print("no project ledger to clear")
    # A `.tmp` can survive a crash between write and replace; it is ours, so clear it too.
    for f in (STATE_FILE, STATE_FILE + ".tmp", PROJECT_FILE + ".tmp"):
        if os.path.exists(f) and (f != PROJECT_FILE + ".tmp" or a.project or not os.path.exists(PROJECT_FILE)):
            os.remove(f)
    # Dropping the loop state of an in-progress area strands the queue: `project next` and
    # a plain `project close` both refuse afterwards, and only `project close --force` can
    # settle it. Say so here, while the operator is still looking, instead of after two
    # failed commands.
    if os.path.exists(PROJECT_FILE):
        try:
            with open(PROJECT_FILE, encoding="utf-8") as f:
                stranded = next((x for x in json.load(f)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            stranded = None
        print("note: a project ledger is still here; `project status` continues it, "
              "`reset --project` discards it", file=sys.stderr)
        if stranded:
            print(f"note: area `{area_name(stranded)}` was in progress; its loop state is now gone — "
                  "`project close --force` marks it incomplete and lets the queue continue "
                  "(a human decision, like every --force)", file=sys.stderr)
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
    """Atomic, for the same reason as save(): the ledger is the whole project review."""
    atomic_json(PROJECT_FILE, p)


def area_paths(x):
    # Ledgers written before multi-path areas store a single "path" string.
    return x["paths"] if "paths" in x else [x["path"]]


def loop_is_area(state, area):
    """True when the open loop is the one this area opened.

    `project close` records an outcome from it and `reset` refuses to discard it; both need
    the same answer, and asking it in two ways is how they came to disagree.
    """
    return state.get("mode") == "project" and set(area_paths(area)) <= set(state.get("scope") or [])


def area_name(x):
    return " + ".join(area_paths(x))


def area_slug(x):
    """Findings-file stem for an area.

    Every path contributes, so two groups sharing a first path cannot collide. An area of
    many paths would otherwise exceed the filesystem's 255-byte name limit and the findings
    file could not be written at all, so an over-long stem is truncated with a hash tail.
    """
    paths = area_paths(x)
    stem = "__".join(p.strip("/").replace("/", "__") for p in paths) or "root"
    # The hash is unconditional, not a length fallback: `["src", "lib"]` and `["src/lib"]`
    # both flatten to `src__lib`, so one area's findings file silently overwrote the other's
    # — and in report-only that file is the entire deliverable, written after `project close`
    # has already deleted the state that could contradict it.
    tail = hashlib.sha256("\0".join(paths).encode()).hexdigest()[:8]
    return (stem[:80] if len(stem) > 80 else stem) + "-" + tail


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
    # Every area is checked here, before any of them is reviewed: a mistyped or renamed
    # path matches no file, and an area nobody can read still passes every gate and closes
    # as `accepted`. Checking at queue time reports all the bad paths at once, while the
    # operator is still choosing them, instead of one per `project next`.
    empty = [x for x in areas if scope_is_empty(x, "project")]
    if empty and not a.allow_empty:
        die("these area(s) match no file — fix the paths (--allow-empty to queue them anyway):\n"
            + "\n".join(f"  - {' + '.join(x)}" for x in empty), code=NO_CHANGES)
    p = {
        "created": now(),
        "max_passes": a.max_passes,
        "report_only": bool(a.report_only),
        "allow_empty": bool(a.allow_empty),
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
    # Remember any findings file already on disk, so a leftover one cannot stand in for this
    # review's output at close.
    # A previous review's file is moved aside rather than compared against. Comparison was
    # wrong in both directions: a digest match refused a legitimately identical repeat review
    # for ever, and an mtime fallback accepted a stale file that had merely been touched.
    # Setting it aside makes the gate a plain existence check again, and keeps the old text.
    seed_file = os.path.join(FINDINGS_DIR, area_slug(nxt) + ".md")
    if os.path.exists(seed_file):
        os.replace(seed_file, seed_file[:-3] + ".prev.md")
    save_project(p)
    # Re-checked here, not only at queue time: files can be deleted or moved between
    # queueing the project and reaching this area, and an area that has become empty must
    # not open a loop whose every gate passes vacuously.
    if not p.get("allow_empty") and scope_is_empty(area_paths(nxt), "project"):
        # Record it and move on rather than refusing forever. Leaving it `pending` made
        # every later `project next` hit the same area, so one renamed directory made every
        # remaining area unreachable — the opposite of "never stop the project on one stuck
        # area", and unrecoverable except by rebuilding the ledger and losing the outcomes
        # already earned.
        nxt.update({"status": "incomplete", "closed": now(),
                    "blockers": ["area matches no file — path renamed, deleted or mistyped"],
                    "passes": 0, "findings": None, "resolved": None, "score": None,
                    "test_evidence": None, "understood": None, "findings_file": None})
        save_project(p)
        left = sum(1 for x in p["areas"] if x["status"] == "pending")
        print(f"area `{area_name(nxt)}` matches no file — recorded incomplete; "
              f"{left} area(s) still pending. Run `project next` again for the next one.",
              file=sys.stderr)
        sys.exit(NO_CHANGES)
    state = {
        "created": now(), "mode": "project", "base": None, "scope": area_paths(nxt),
        "report_only": p["report_only"], "allow_empty": bool(p.get("allow_empty")),
        "max_passes": p["max_passes"], "passes": [], "validation": [],
    }
    state["fingerprint_current"] = fp_of(state)
    save(state)
    # SKILL.md tells the agent to write the reviewer's findings here; the directory has to
    # exist for that to be possible with a plain shell redirect.
    os.makedirs(FINDINGS_DIR, exist_ok=True)
    done = sum(1 for x in p["areas"] if x["status"] not in ("pending", "in_progress"))
    print(f"area {done + 1}/{len(p['areas'])}: {area_name(nxt)}  (fingerprint {state['fingerprint_current']})")
    print(f"findings file: {os.path.join(FINDINGS_DIR, area_slug(nxt) + '.md')}")
    if p["report_only"]:
        print("report-only: record the pass, write the findings file, then `project close` — do not fix")


def findings_digest(ffile):
    """Hash of the findings file, or None when there is none. Content, never mtime.

    Timestamps are the obvious way to ask "was this written during this loop?" and the wrong
    one: `now()` has one-second resolution and a whole area can open and close inside that
    second, so the comparison silently passes exactly when the runs are back to back.
    """
    try:
        with open(ffile, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def findings_are_current(ffile, area):
    """True when a findings file exists for this review.

    `project next` moves any previous review's file to `<slug>.prev.md`, so anything at this
    path was written during this area's current loop. The path repeats on every review of an
    area, and without that move a leftover file satisfied the gate while this run's findings
    died with `state.json`.
    """
    return os.path.exists(ffile)


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
    # The loop on disk must be *this* area's. Anything else is a review of other code being
    # signed into this area's row.
    if not loop_is_area(state, cur):
        if not a.force:
            die(f"the open loop is not area `{area_name(cur)}` (mode {state.get('mode')}, scope "
                f"{state['scope']}) — its outcome cannot be recorded here. "
                "`project close --force` settles this area `incomplete` and leaves that loop "
                "alone; `reset` then frees it and the queue continues with `project next`.")
        # Settle the area from what is known — nothing — and do not touch the foreign loop:
        # it is some other review's whole history, and this command has no claim on it. The
        # area stays in the ledger, visible for a re-run.
        cur.update({"status": "incomplete", "closed": now(),
                    "blockers": ["the open loop belongs to another scope; this area was never reviewed"],
                    "passes": None, "findings": None, "resolved": None, "score": None,
                    "test_evidence": None, "understood": None, "findings_file": None})
        save_project(p)
        print(f"area `{area_name(cur)}` closed: incomplete — the open loop is not this area's")
        print(f"note: {STATE_FILE} was left untouched; `reset` discards it when that loop is done",
              file=sys.stderr)
        return
    state["fingerprint_current"] = fp_of(state)
    lp = last_recorded_pass(state)
    ffile = os.path.join(FINDINGS_DIR, area_slug(cur) + ".md")
    r = lp.get("result") if lp else None
    if p["report_only"]:
        # Two kinds of problem, and conflating them is what stalled the queue. A missing
        # findings file is fixable right now — refuse, so the review is written down before
        # the state that holds it is deleted. An area that never produced a usable pass is
        # not fixable by refusing: it is recorded `incomplete` with its blockers and the
        # queue moves on, exactly as fix mode settles a failed area without a hatch.
        terminal = []
        if r is None:
            terminal.append("no recorded pass")
        elif not r["understood"]:
            terminal.append("reviewer did not demonstrate a credible understanding")
        elif lp["fingerprint"] != state["fingerprint_current"]:
            # Report-only never reaches blockers(), so without this an area rewritten or
            # deleted after its pass closes `reviewed`, and `project close` then deletes the
            # only state that could contradict the ledger row.
            terminal.append("the area moved since the pass; the review describes code that is "
                            "no longer there")
        elif scope_is_empty(state["scope"], "project"):
            terminal.append("the area's files are gone; the review describes nothing")
        fixable = []
        # Required whatever the finding count: closing deletes state.json, so this file is
        # the only surviving record of the review — for a clean area it is the *entire*
        # record, and skipping it loses the understanding summary, the test-evidence basis
        # and the score rationale for exactly the areas with no findings to remember them by.
        if not terminal and not findings_are_current(ffile, cur):
            fixable.append(
                f"{ffile} is missing — write *this* "
                "reviewer's whole output there; closing deletes the loop state, so nothing "
                "else survives")
        if fixable and not a.force:
            die("cannot close area: " + "; ".join(fixable)
                + " (--force closes it as incomplete instead, losing the review)")
        problems = terminal + fixable
        cur["status"] = "reviewed" if not problems else "incomplete"
        cur["blockers"] = problems
    else:
        reasons = blockers(state)
        # The findings file is required here for the same reason as in report-only: closing
        # deletes `state.json`, so for a clean area that file is the entire surviving record
        # of the review — its understanding summary, test-evidence basis and score rationale.
        if r and not findings_are_current(ffile, cur) and not a.force:
            die(f"cannot close area: {ffile} is missing — write this reviewer's whole output there; closing deletes the loop state, so "
                "nothing else survives (--force closes it anyway, losing the review)")
        reasons = blockers(state)
        cur["status"] = "accepted" if not reasons else "incomplete"
        cur["blockers"] = reasons
    cur["closed"] = now()
    # The loop's scope, not the queued paths: `scope --` may have widened the area when a fix
    # landed next door, and the ledger is the deliverable — it must say what was reviewed.
    if state["scope"] != area_paths(cur):
        cur["reviewed_paths"] = state["scope"]
    cur["passes"] = len(state["passes"])
    # Totals across every pass of this area: a fixed finding from pass 1 is part of the
    # area's story even though pass 2 reports zero.
    recorded = [x["result"] for x in state["passes"] if x.get("result")]
    cur["findings"] = sum(x["findings"] for x in recorded) if recorded else None
    cur["resolved"] = sum(x["resolved"] for x in recorded) if recorded else None
    cur["score"] = r["score"] if r else None
    # Exit condition 4's verdict would otherwise die with state.json: in report-only mode
    # `blockers()` is never consulted, so the ledger is the only place it can be reported.
    cur["test_evidence"] = r.get("test_evidence") if r else None
    cur["understood"] = r.get("understood") if r else None
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
        if x.get("test_evidence"):
            line += f"  test-evidence {x['test_evidence']}"
        if x.get("passes") is not None:
            line += f"  passes {x['passes']}  findings {x.get('resolved')}/{x.get('findings')}  score {x.get('score')}"
        if x.get("blockers"):
            line += "  — " + "; ".join(x["blockers"])
        print(line)


# ---------- main ----------

def build_parser():
    """The argument surface, built separately so it can be inspected without running.

    `scripts/selftest.py` parses every command line printed in SKILL.md and the reference
    prompts against this parser. Prose that names a flag the CLI does not have, or invokes
    the script by a path that does not exist, is a defect the workflow walks straight into —
    and no amount of exercising the script itself can catch it.
    """
    p = argparse.ArgumentParser(prog="loop_review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--max-passes", type=positive_int, default=5); s.add_argument("--mode", choices=MODES, default="changes"); s.add_argument("--base", metavar="REF", help="commit to diff against (changes mode); use for already-committed work"); s.add_argument("--task-brief", metavar="TEXT", help="the task's requirements and acceptance criteria, for the reviewer — never rationale or suspected issues"); s.add_argument("--task-brief-file", metavar="PATH", help="same, read from a file"); s.add_argument("--scope-note", metavar="TEXT", help="hunk-level ownership the reviewer must be told, echoed every pass"); s.add_argument("--allow-empty", action="store_true", help="proceed on an empty scoped diff instead of reporting `no-changes`"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("validate"); s.add_argument("--none", action="store_true", help="record that no executable check applies (needs --reason)"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("scope"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_scope)
    s = sub.add_parser("validate-drop"); s.add_argument("--force", action="store_true"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate_drop)
    s = sub.add_parser("pass-start"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_pass_start)
    s = sub.add_parser("brief"); s.add_argument("--task-brief", metavar="TEXT"); s.add_argument("--task-brief-file", metavar="PATH"); s.add_argument("--scope-note", metavar="TEXT"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_brief)
    s = sub.add_parser("pass-abort"); s.add_argument("--reason"); s.set_defaults(fn=cmd_pass_abort)
    s = sub.add_parser("pass-record"); s.add_argument("--score", type=score_value); s.add_argument("--findings", type=nonneg_int, required=True); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--understood", action="store_true"); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_pass_record)
    s = sub.add_parser("amend"); s.add_argument("--understood", action="store_true"); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--score", type=score_value); s.add_argument("--findings", type=nonneg_int); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("resolve"); s.add_argument("--fixed", type=nonneg_int, default=0); s.add_argument("--withdrawn", type=nonneg_int, default=0); s.add_argument("--adjudicated-invalid", type=nonneg_int, default=0); s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("accept"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fingerprint"); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser("reset"); s.add_argument("--project", action="store_true", help="also discard the project ledger"); s.set_defaults(fn=cmd_reset)

    pr = sub.add_parser("project").add_subparsers(dest="pcmd", required=True)
    s = pr.add_parser("init"); s.add_argument("--max-passes", type=positive_int, default=5); s.add_argument("--report-only", action="store_true", help="collect findings per area without fixing"); s.add_argument("--allow-empty", action="store_true", help="queue areas that match no file"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_project_init)
    s = pr.add_parser("next"); s.set_defaults(fn=cmd_project_next)
    s = pr.add_parser("close"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_project_close)
    s = pr.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_project_status)

    return p


def main():
    p = build_parser()

    # Paths and validation commands can carry undecodable bytes (surrogates from
    # os.fsdecode / argv). Printing them must not be the thing that kills the loop.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")

    a = p.parse_args(sys.argv[1:])
    a.fn(a)


if __name__ == "__main__":
    main()
