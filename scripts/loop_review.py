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
  validate-drop [--force] {-- <command...> | --none}
                                           retract a record for a command that never ran,
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

def user_path(v):
    """argparse type for a path the user typed: resolved against the *caller's* cwd.

    Every command chdirs to the repository root before it does anything, so a relative path
    left as text means something different by the time it is used. `init` guarded its two
    path arguments by hand and `brief` did not, which is the same defect twice: from a
    subdirectory, `--task-brief-file brief.md` read a same-named file at the root and briefed
    every reviewer of the loop with someone else's document, silently. Resolving at parse
    time — before any chdir — makes the ordering inside command bodies stop mattering.
    """
    return os.path.abspath(v)


def area_token(v):
    """`project init` path, or the standalone `+` that glues two areas into one."""
    return v if v == "+" else user_path(v)


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


def read_json(path):
    """Parse one of this script's own JSON files. UTF-8, never the platform locale.

    Every read and write of `state.json` and `project.json` goes through this pair. Passing
    `encoding=` at each call site is the same fix written five times, and it was already
    written unevenly: three sites kept the platform default while four had been corrected,
    so a brief or a reviewer note with a non-ASCII character was decoded by whatever locale
    the machine happened to have. Raises OSError/ValueError; the caller decides whether a
    missing or broken file is fatal.
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, payload):
    """Write JSON atomically: temp file beside it, fsync, then os.replace.

    Atomic because a partial write costs the whole loop (see save()), and UTF-8 for the same
    reason as read_json — the two must agree on the encoding whatever the machine's locale.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load():
    if not os.path.exists(STATE_FILE):
        die("no active loop; run `init` first")
    try:
        return read_json(STATE_FILE)
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
    write_json(STATE_FILE, state)


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
    def entries(p, exclude_ignored):
        args = ["ls-files", "-z", "--cached", "--others"]
        if exclude_ignored:
            args.append("--exclude-standard")
        return [x for x in git(*args, "--", p).split(b"\0") if x]

    out = []
    for p in paths:
        if [x for x in entries(p, True) if outside_state_dir(x)]:
            continue                                   # the fingerprint can see it
        # Why it is invisible decides what the operator should do about it, so the reasons
        # are told apart rather than guessed from `os.path.exists`. Calling an empty
        # directory "ignored by git" sent them to a `.gitignore` that never mentioned it,
        # when the real answer is that there is nothing in it yet.
        everything = entries(p, False)
        if not os.path.exists(p):
            why = "does not exist"
        elif not everything:
            why = ("is an empty directory" if os.path.isdir(p)
                   else "holds nothing git can see")
        elif not [x for x in everything if outside_state_dir(x)]:
            why = "holds only the loop's own state, which every scope excludes"
        else:
            why = "exists, but everything in it is ignored by git"
        out.append((p, why))
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


def is_evidence(v):
    """True when a validation record can stand for the state it was made on.

    A retracted record was withdrawn; a `moved_scope` one edited the files while checking
    them and so describes neither the state before nor the state after. Every gate that
    reads validation asks this one question, in one place — asking it separately per gate is
    how `red_provenance` came to count a voided run as proof a check had once been green.
    """
    return not v.get("retracted") and not v.get("moved_scope")


def is_declaration(v):
    """True for a `validate --none` record: a claim that no check applies, not a run.

    It satisfies exit condition 1 — the author answered the question honestly — but it is not
    evidence that anything was executed, so it never joins the set of checks a pass "rested
    on". Treating it as one inverted the rule it was caught by: replacing the declaration
    with real tests *grows* the evidence, and `pass-start` refused it as shrinkage, demanding
    the declaration be re-run or retired with a `--force` the agent may not use.
    """
    return v.get("cmd") == NO_CHECK


def evidence_set(records):
    """The commands of `records` that a later pass must carry: real runs, never declarations."""
    return {c: v for c, v in records.items() if not is_declaration(v)}


def validation_view(state, fp=None):
    """The loop's validation state as the gates need it: `(records, retired)`.

    `records` maps each command to its latest usable run **at `fp`** (default: the current
    fingerprint). Re-running a command on unchanged files supersedes its earlier result, so
    a flaky or environment failure clears by re-running — not only by editing — and distinct
    commands stay separate: a green re-run of the tests does not absolve a red lint.

    `retired` holds the commands whose most recent event in the whole loop is a retirement.
    That is deliberately *not* scoped to a fingerprint: retiring a check is a decision about
    the check, made once by a human with `validate-drop --force`, and it holds until the
    command is run again. Keyed by fingerprint instead, the decision evaporated at the next
    edit and `pass-start` demanded the same `--force` over again — turning the escape hatch
    into a routine keystroke, which is exactly how an escape hatch stops meaning anything.
    """
    fp = state["fingerprint_current"] if fp is None else fp
    at_fp, last_event = {}, {}
    for v in state["validation"]:
        # Append-only, so list order is chronological: a later run of a command supersedes
        # its retirement, and a retirement supersedes the run it was stamped on.
        last_event[v["cmd"]] = v
        if v["fingerprint"] == fp and is_evidence(v):
            at_fp[v["cmd"]] = v
    return at_fp, {c for c, v in last_event.items() if v.get("retracted")}


def validation_at(state, fp):
    return validation_view(state, fp)[0]


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
    prior = evidence_set(validation_at(state, lp["fingerprint"]))
    present, retired = validation_view(state)
    # A retired check is one a human took out of the set; without this the set could only
    # ever grow and `validate-drop` could not do what its message promises.
    return [c for c in prior if c not in present and c not in retired]


def red_provenance(state, cmd):
    """`inherited` if this command has never been green in this loop, else `regressed`.

    Project mode opens a pass over a failing check because an inherited area is reviewed as
    it stands. But that licence belongs to a check that was already failing: one the loop's
    own fix batch broke is a regression, and telling a fresh reviewer it is "a property of
    the area you inherited" hands it the wrong conclusion about the very code it is there to
    judge. The loop's own history answers this, so the agent never has to remember it.
    """
    # `is_evidence` is what keeps this honest: a `moved_scope` run cannot establish that the
    # check was ever green, and `regressed` tells a fresh reviewer that the change under
    # review broke it — a conclusion no voided run may support.
    for v in state["validation"]:
        if v["cmd"] == cmd and v["exit"] == 0 and is_evidence(v):
            return "regressed"
    return "inherited"


def metric(value, absent="not given"):
    """Render an optional metric, never as the literal `None`.

    `--score` and `--test-score` are optional by design — the agent must not invent a value
    the reviewer withheld — so every line that shows one has to say so in words. `pass-record`
    spelled that out for itself while `accept` and `status` printed `None`, which reads as a
    value and, worse, as a defect in the record rather than a verdict the reviewer never gave.
    """
    return absent if value is None else value


def print_validation(cur, header="validation on current state", state=None):
    """One rendering of validation state for every text-mode command.

    `references/reviewer-prompt.md` is filled from `status`, and its validation section
    needs each command with its exit code — an aggregate GREEN/RED line cannot be
    transcribed into the reviewer's prompt.
    """
    print(f"{header}: {len(cur)} command(s), " +
          ("GREEN" if cur and all(v["exit"] == 0 for v in cur) else "RED/none"))
    for v in cur:
        if is_declaration(v) and v.get("reason"):
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
        fixed = any(x.get("fixed") for x in lp["result"].get("resolution_log", []))
        # Standing on the reviewed fingerprint *is* the proof that the fixes are absent —
        # a fix changes the code, so it moves the hash. Excusing the case where the fix was
        # recorded on that same fingerprint excused the only way to reach it deliberately:
        # `resolve --fixed` typed before touching a file left the log pointing at the
        # reviewed state, the exemption fired, and `accept` signed off the defect itself.
        if fixed and state["fingerprint_current"] == lp["fingerprint"]:
            reasons.append("the scope is on the state the review found defective — the "
                           "recorded fixes are not present (never made, reverted, stashed, "
                           "checked out, or landed outside the scope: widen it with `scope --`)")
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
    # Path arguments arrive absolute (see `user_path`), so reading the brief here rather
    # than after the chdir is no longer what keeps it correct — it just keeps the failure
    # early, before a loop is written.
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
            busy = next((x for x in read_json(PROJECT_FILE)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            busy = None
        if busy:
            die(f"area `{area_name(busy)}` is mid-loop — `project close` records its outcome "
                "first. Re-initialising here would close that area from a review of something "
                "else; `--force` does not cover that.")
    if (a.task_brief or a.task_brief_file) and a.mode == "project":
        # An inherited area has no task and therefore no acceptance criteria to miss;
        # a brief here would only be the operator's opinion smuggled past isolation.
        named = [f for f in brief_flags_given(a) if f != "--scope-note"]
        die(" and ".join(named) + " applies to --mode changes only")
    if a.scope_note and a.mode == "project":
        # A scope note says which hunks of a scoped file are task-owned. An area has no
        # task-owned hunks: everything under its paths is in scope, which is what project
        # mode means. `reviewer-prompt-area.md` has no field for one, so a note recorded
        # here reaches nobody — while `status` still tells the agent to "repeat it verbatim
        # in the reviewer's hunk fields", inviting a third channel across the isolation
        # boundary. `brief` refuses it; init accepted it, and one of the two had to be wrong.
        die("--scope-note applies to --mode changes only: an inherited area has no "
            "task-owned hunks, every file under it is in scope")
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
    print_loop_context(state)


def brief_flags_given(a):
    """The brief/scope-note flags the operator actually typed, in the order they are documented.

    A refusal must name what was passed. `--task-brief-file` was folded into `--task-brief`
    at both refusal sites, so someone who supplied a file was told about a flag they never
    used and could not tell whether their own was allowed. Two call sites asked the same
    question, so they ask it once here.
    """
    return [flag for flag, used in (("--task-brief", a.task_brief),
                                    ("--task-brief-file", getattr(a, "task_brief_file", None)),
                                    ("--scope-note", getattr(a, "scope_note", None))) if used]


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


def print_loop_context(state):
    """Echo everything of the loop's own context that reaches the reviewer: brief, then note.

    Those two are the only things that cross into the reviewer's prompt, so every command
    that shows the loop's context must show both. Printed by hand at four sites, one of them
    — `init`, the command that records them — printed the brief and dropped the note: a
    `--scope-note` given at init was stored and never confirmed, and an agent filling the
    prompt from that output had no reason to think a note existed at all.
    """
    print_task_brief(state)
    print_scope_note(state)


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


def check_none_or_command(a, verb, empty_hint):
    """`--none` is about the declaration, a command after `--` is about a run: exactly one.

    `validate-drop` refused both-at-once; `validate` did not, and `validate --none --reason X
    -- pytest -q` silently recorded "no executable check applies" while dropping the command
    the operator wrote. That is worse than a rejected typo: the loop then tells the reviewer
    there was nothing to run, on a surface that has a test suite.
    """
    if a.none and a.command:
        die(f"{verb} takes --none or a command after `--`, not both: `--none` records that "
            "no executable check applies, a command names one to run")
    if not a.none and not a.command:
        die(empty_hint)


def cmd_validate(a):
    os.chdir(repo_root())
    state = load()
    check_none_or_command(a, "validate",
                          "validate needs a command after `--`, or `--none --reason ...` "
                          "when none applies")
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


def record_state(hits):
    """`(kind, phrase)` for a command's latest record on the current state.

    Every `validate-drop` refusal describes this state back to the operator, and each branch
    phrased it for itself. The inherited branch printed "it does not run here (exit N)" for
    any record at all, so a check re-run green read as "it does not run here (exit 0)" — a
    false statement about the very state the message was printed in, on the path where a
    human is being asked to decide whether to give up evidence.
    """
    if not hits:
        return "absent", "it has not been re-run on the current state"
    v = hits[-1]
    if is_declaration(v):
        # Not a run at all: the author declared that no executable check applies. Retracting
        # it withdraws a claim, and withdrawing it can only make the gates stricter — with no
        # validation left, `pass-start` and `accept` both refuse.
        return "declared", "it records that no check applies, which is a claim, not a run"
    if v["exit"] in NEVER_RAN:
        return "never-ran", f"it cannot start here (exit {v['exit']})"
    if v["exit"] == 0:
        return "green", "it is green on the current state"
    return "failed", f"it ran here and failed (exit {v['exit']})"


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
    check_none_or_command(a, "validate-drop",
                          "validate-drop needs a command after `--`, or --none to withdraw "
                          "a `validate --none` record")
    # `--none` names the same record `validate --none` wrote. Without it the only way to
    # withdraw a mistaken declaration was to type the script's internal marker verbatim.
    cmd = NO_CHECK if a.none else join_cmd(a.command)
    state["fingerprint_current"] = fp_of(state)
    hits = [v for v in state["validation"]
            if v["cmd"] == cmd and v["fingerprint"] == state["fingerprint_current"] and not v.get("retracted")]
    lp = last_recorded_pass(state)
    # Membership in the last pass's evidence set, decided on its own — *not* on whether the
    # command also has a record here. Reading it as "no record here" left the rule open from
    # the other side: running an inherited check so it fails to start (126/127) put it in the
    # never-ran branch, where a plain `validate-drop` retired it and `pass-start` accepted the
    # shrunken set. A check the review was granted on stays evidence even once the tool that
    # ran it is gone; what changed is the environment, and that is the human's call to weigh.
    prior = (evidence_set(validation_at(state, lp["fingerprint"]))
             if lp is not None and lp["fingerprint"] != state["fingerprint_current"] else {})
    inherited = cmd in prior
    _, retired = validation_view(state)
    if cmd in retired and not hits:
        # Already out of the set and still out — the retirement holds until the command is
        # run again. Saying "no validation record" here sent the operator to re-force a
        # decision that was never undone.
        die(f"`{cmd}` is already retired; it stays out of the set until it is run again")
    if not hits and not inherited:
        print(f"loop-review: no validation record for `{cmd}` at the current state", file=sys.stderr)
        cur = current_validation(state)
        if cur:
            print("recorded commands:", file=sys.stderr)
            for v in cur:
                print(f"  - {v['cmd']}  -> exit {v['exit']}", file=sys.stderr)
        sys.exit(1)
    if inherited:
        # A check carried over from the last pass. `pass-start` only opens a pass on green
        # validation, so anything in that set passed — retiring it weakens the evidence the
        # review was granted on, and that is a human call, never the agent's.
        if not a.force:
            kind, why = record_state(hits)
            tail = ("retiring it is still a human call (--force), and it costs the review "
                    "that evidence" if kind == "green" else
                    "re-run it, or retire it with --force (that weakens the evidence set)")
            die(f"`{cmd}` carried pass {lp['n']} and {why}; {tail}")
        if hits:
            for v in hits:
                v["retracted"] = {"at": now(), "reason": a.reason or f"retired: inherited from pass {lp['n']}"}
        else:
            # No record here to mark, so the retirement is stamped as its own entry: history
            # stays append-only and the decision belongs to the state where it was made.
            state["validation"].append({
                "cmd": cmd, "exit": None, "at": now(), "fingerprint": state["fingerprint_current"],
                "retracted": {"at": now(), "reason": a.reason or f"retired: inherited from pass {lp['n']}, not re-run"},
            })
        save(state)
        print(f"retired `{cmd}` — inherited from pass {lp['n']}")
        print_validation(current_validation(state), state=state)
        return
    kind, why = record_state(hits)
    if kind in ("green", "failed") and not a.force:
        advice = ("fix it or re-run the command" if kind == "failed"
                  else "there is nothing here to retract")
        die(f"`{cmd}`: {why} — that is a result, not a mistake; {advice} "
            "(--force to retract anyway)")
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
        # Two different exits, and naming the wrong one is how the gate got walked around:
        # a check the last pass rested on is still evidence once its tool disappears, so
        # retiring it needs `--force`; only a command no pass leaned on clears for free.
        lrp = last_recorded_pass(state)
        prior = (validation_at(state, lrp["fingerprint"])
                 if lrp is not None and lrp["fingerprint"] != state["fingerprint_current"] else {})
        carried = [v["cmd"] for v in never_ran if v["cmd"] in prior]
        die("these command(s) never ran (missing tool or typo), so they are evidence about "
            "nothing: " + "; ".join(f"`{v['cmd']}` (exit {v['exit']})" for v in never_ran)
            + " — fix the command and re-run it, or retract the record with `validate-drop`."
            + (" Note that " + "; ".join(f"`{c}`" for c in carried)
               + " carried the last pass, so retiring it takes `validate-drop --force`, "
                 "a human decision." if carried else ""))
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
            + " — re-run them. Retiring one of these weakens the evidence this review rests on, "
              "even if the tool that ran it is gone, so it needs `validate-drop --force` — "
              "a human decision. (Plain `validate-drop` only clears a command that never ran "
              "here and no pass rested on.)")
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
    print_loop_context(state)
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
        # Say which flag is being refused, not one the operator may never have typed: both
        # are changes-mode only, for two different reasons, and a message about `--task-brief`
        # left someone who passed `--scope-note` unable to tell whether it was allowed.
        given = brief_flags_given(a)
        die(" and ".join(given or ["brief"]) + ": an inherited area has no task and no "
            "acceptance criteria, and no task-owned hunks — every file under it is in "
            "scope. They are changes-mode only.")
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
    print_loop_context(state)


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
    print(f"pass {lp['n']} recorded: score {metric(a.score)}, "
          f"{a.findings} finding(s), understood={bool(a.understood)}, "
          f"test evidence {metric(a.test_evidence, 'not assessed')}")
    if a.findings == 0 and a.score is not None and a.score < 9.5:
        print("note: score below 9.5 with zero findings — treat the number as calibration noise; findings control the loop")


def require_recorded_pass(state, action, missing):
    """The last recorded pass, refused while a newer pass is open.

    `amend` and `resolve` both mutate that pass, and with a newer one open the pass they
    reach is the one it supersedes: the edit lands on a record no gate consults any more,
    while the output or the fix batch in the operator's hands belongs to the open review.
    `amend` learned to refuse; `resolve` did not, and silently moved `resolved` and the
    resolution log onto the dead record — then advised opening a new pass, which was already
    open. One guard, so the next command that edits a recorded pass cannot forget.
    """
    op = open_pass(state)
    if op is not None:
        die(f"pass {op['n']} is open — `pass-record` states its verdict, `pass-abort` "
            f"discards it. `{action}` edits the last recorded pass, which that one supersedes.")
    lp = last_recorded_pass(state)
    if lp is None:
        die(missing)
    return lp


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
    lp = require_recorded_pass(state, "amend", "no recorded pass to amend")
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
    print(f"pass {lp['n']}: score {metric(r['score'])}, {r['resolved']}/{r['findings']} resolved, "
          f"understood={r['understood']}, test evidence {metric(r.get('test_evidence'), 'not assessed')}")
    if state["fingerprint_current"] != lp["fingerprint"]:
        print("note: scoped changes moved since this pass — the review is stale; validate and open a new pass")


def cmd_resolve(a):
    os.chdir(repo_root())
    state = load()
    lp = require_recorded_pass(state, "resolve", "no recorded pass to resolve against")
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
    elif a.fixed:
        # Say it here, while the operator is still in the fix batch, rather than letting
        # them discover at `accept` that nothing they recorded is on disk. A fix changes
        # code, so it moves the fingerprint; one that did not is either still unwritten or
        # landed outside the recorded scope, where no gate can see it.
        print("note: a fix was recorded but the scope has not moved — the change is not on "
              "disk yet, or it landed outside the scope (`scope --` widens it). `accept` "
              "will refuse while the code is the state the review called defective",
              file=sys.stderr)


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
        # Not conditioned on a recorded pass: the limit counts passes *opened*, and an
        # aborted one counts as used. With every pass aborted there is no recorded pass at
        # all, which is exactly when the operator most needs to be told that the loop has
        # run out — otherwise the only hint comes from the next `pass-start` refusing.
        if len(state["passes"]) >= state["max_passes"]:
            print(f"pass limit reached ({state['max_passes']}) — report as INCOMPLETE"
                  + ("; every pass was aborted, and an aborted pass counts as used"
                     if lp is None else ""))
        sys.exit(1)
    print(f"ACCEPTED after {len(state['passes'])} pass(es); "
          f"last score {metric(lp['result']['score'])} (progress signal only)")


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
    print_loop_context(state)
    print(f"passes: {len(state['passes'])}/{state['max_passes']}")
    for p in state["passes"]:
        r = p.get("result")
        if r:
            print(f"  #{p['n']}  score {metric(r['score'])}  findings {r['resolved']}/{r['findings']} resolved"
                  f"  understood={r['understood']}  test-evidence={metric(r.get('test_evidence'), 'not assessed')}"
                  f"  test-score={metric(r.get('test_score'))}")
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


def own_artifacts():
    """Everything inside `.loop-review/` this script creates.

    One list feeds both the removal in `reset` and the "did we make this?" test that decides
    whether the directory may be removed — asking the question in two places is how they came
    apart: `project.json.tmp` survived a plain `reset`, and the leftover then made `reset`
    announce that the directory "holds files this script did not create" about its own crash
    debris.
    """
    return (STATE_FILE, STATE_FILE + ".tmp", PROJECT_FILE, PROJECT_FILE + ".tmp", FINDINGS_DIR)


def cmd_reset(a):
    os.chdir(repo_root())
    # An area frees its loop with `project close`, never with `reset`. Resetting inside one
    # leaves the ledger pointing at a loop whose state is gone, and the documented recovery
    # (`project close --force`) then records an area that passed every gate as `incomplete`
    # with its passes, score and verdict all null. Refuse instead of losing the outcome.
    if os.path.exists(PROJECT_FILE) and os.path.exists(STATE_FILE):
        try:
            busy = next((x for x in read_json(PROJECT_FILE)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            busy = None
        if busy:
            try:
                open_loop = read_json(STATE_FILE)
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
    # A `.tmp` can survive a crash between write and replace; it is debris, never the file
    # itself, so both are cleared whatever `--project` says. Only `project.json` is a record
    # worth keeping, and it is removed above when the operator asked for it.
    for f in (f for f in own_artifacts() if f.endswith(".tmp") or f == STATE_FILE):
        if os.path.exists(f):
            os.remove(f)
    # Dropping the loop state of an in-progress area strands the queue: `project next` and
    # a plain `project close` both refuse afterwards, and only `project close --force` can
    # settle it. Say so here, while the operator is still looking, instead of after two
    # failed commands.
    if os.path.exists(PROJECT_FILE):
        try:
            stranded = next((x for x in read_json(PROJECT_FILE)["areas"] if x["status"] == "in_progress"), None)
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
        ours = {os.path.basename(f) for f in own_artifacts()}
        if os.path.isdir(STATE_DIR) and not set(os.listdir(STATE_DIR)) <= ours:
            print(f"note: {STATE_DIR}/ kept — it holds files this script did not create")
    print("state cleared")


# ---------- project mode ----------

def load_project():
    if not os.path.exists(PROJECT_FILE):
        die("no project review; run `project init` first")
    try:
        return read_json(PROJECT_FILE)
    except ValueError as e:
        die(f"{PROJECT_FILE} is not valid JSON ({e})")


def save_project(p):
    """Atomic, for the same reason as save(): the ledger is the whole project review."""
    write_json(PROJECT_FILE, p)


def area_paths(x):
    # Ledgers written before multi-path areas store a single "path" string.
    return x["paths"] if "paths" in x else [x["path"]]


def loop_is_area(state, area):
    """True when the open loop is the one this area opened.

    `project close` records an outcome from it and `reset` refuses to discard it; both need
    the same answer, and asking it in two ways is how they came to disagree.

    Identity is the area the loop was opened for, stamped at `project next`, never the scope
    it currently covers. Scope cannot answer it: `scope --` legitimately widens a loop beyond
    its queued paths, so "the loop covers this area" also matched a *different* area's loop
    that merely contained these paths — and `project close` signed that review into this
    area's row without so much as a `--force`. A loop opened before this field existed is
    still matched by containment; there is nothing else to go on.
    """
    if state.get("mode") != "project":
        return False
    opened_for = state.get("area")
    if opened_for is not None:
        return list(opened_for) == list(area_paths(area))
    return set(area_paths(area)) <= set(state.get("scope") or [])


def area_name(x):
    return " + ".join(area_paths(x))


def area_slug(x):
    """Findings-file stem for an area.

    Every path contributes, so two groups sharing a first path cannot collide. An area of
    many paths would otherwise exceed the filesystem's 255-byte name limit and the findings
    file could not be written at all, so an over-long stem is truncated with a hash tail.
    """
    paths = area_paths(x)
    stem = "__".join(p.strip("/").replace("/", "__") for p in paths)
    # A leading dot would make the findings file hidden, and in report-only that file is the
    # entire surviving deliverable — `project close` deletes the loop state, so a report
    # nobody lists is a report nobody has. The repository root arrives as `.`, and any area
    # named `.github`, `.config` and so on has the same effect. Identity is unaffected: the
    # hash tail below is unconditional, so `.github` and `github` still differ.
    stem = stem.lstrip(".") or "root"
    # The hash is unconditional, not a length fallback: `["src", "lib"]` and `["src/lib"]`
    # both flatten to `src__lib`, so one area's findings file silently overwrote the other's
    # — and in report-only that file is the entire deliverable, written after `project close`
    # has already deleted the state that could contradict it.
    # os.fsencode, not .encode(): a path git handed us may hold bytes no encoding decodes,
    # which `os.fsdecode` carries as surrogates (PEP 383). Encoding those with the default
    # utf-8 raises, and it raised here — inside the slug of the findings file — so an area
    # whose name is not valid UTF-8 crashed `project next` with a traceback instead of being
    # reviewed. `fsencode` round-trips them back to the original bytes.
    tail = hashlib.sha256(os.fsencode("\0".join(paths))).hexdigest()[:8]
    return (stem[:80] if len(stem) > 80 else stem) + "-" + tail


def check_glue(raw):
    """The `+` grammar, checked as a whole before a single path is resolved.

    `+` joins the path on its left to the one on its right, so every position where one of
    those is missing is the same fault, and each was found separately: a leading `+` was
    refused from the start, a trailing one was dropped in silence until it was reported, and
    `+ +` still passed — quietly gluing the two paths around it, which is also what a command
    line that *lost a path between them* looks like. Whatever the operator meant, the queue
    they get is not the one they wrote, and the areas decide what gets reviewed.
    """
    prev = None
    for tok in raw:
        if tok == "+":
            if prev is None:
                die("`+` must follow a path; it cannot open the list")
            if prev == "+":
                die("`+ +` is not an operator — a path between them is missing")
        prev = tok
    if prev == "+":
        die("`+` must join two paths; it cannot end the list")


def parse_areas(raw, root):
    """`--` paths form areas; a standalone `+` glues neighbours into one multi-path area.

    `a b + c d` -> [a], [b, c], [d]. A flat package split across files, or sibling edge
    functions grouped by meaning, are one area even though they are several paths.
    """
    check_glue(raw)
    areas, cur, prev = [], [], None
    for tok in raw:
        if tok != "+":
            if cur and prev != "+":
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


def settle_area(area, blockers, passes=None):
    """Record an area that produced no usable review, with every metric explicitly absent.

    Three paths reach this outcome — an area whose files are gone, one whose loop state was
    lost, one whose open loop belongs to somewhere else — and each spelled the row out by
    hand until one drifted: the lost-state branch left `test_evidence` and `understood`
    unset, so the ledger, which is the deliverable of a project review, held rows of two
    different shapes and a consumer indexing them by key hit a KeyError on exactly the areas
    that went wrong.
    """
    area.update({"status": "incomplete", "closed": now(), "blockers": blockers,
                 "passes": passes, "findings": None, "resolved": None, "score": None,
                 "test_evidence": None, "understood": None, "findings_file": None})


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
        # `project close` alone cannot settle this: it is refused for exactly this state, a
        # few lines into `cmd_project_close`. Naming it sent the operator through a certain
        # refusal to reach the flag, or worse to `project init --force`, which rebuilds the
        # queue and discards every outcome already earned.
        die(f"area `{area_name(cur)}` is in_progress but its loop state is gone; "
            "`project close --force` settles it `incomplete` and the queue continues "
            "(`project init --force` would rebuild the queue and lose the outcomes already recorded)")
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
        settle_area(nxt, ["area matches no file — path renamed, deleted or mistyped"], passes=0)
        save_project(p)
        left = sum(1 for x in p["areas"] if x["status"] == "pending")
        print(f"area `{area_name(nxt)}` matches no file — recorded incomplete; "
              f"{left} area(s) still pending. Run `project next` again for the next one.",
              file=sys.stderr)
        sys.exit(NO_CHANGES)
    state = {
        "created": now(), "mode": "project", "base": None,
        # The queued paths, kept as the loop's identity even if `scope --` widens `scope`.
        "area": area_paths(nxt), "scope": area_paths(nxt),
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
    ffile = os.path.join(FINDINGS_DIR, area_slug(nxt) + ".md")
    print(f"findings file: {ffile}")
    if ffile != ffile.encode("utf-8", "replace").decode("utf-8", "replace"):
        # The path holds bytes no encoding decodes, so what was just printed is an escaped
        # rendering — copying it from the terminal creates a *different* file, and `project
        # close` would then refuse for a missing report that looks written.
        print("note: this path contains undecodable bytes and is shown escaped; write the "
              "file through the shell's own completion of the area name, not by copying "
              "the line above", file=sys.stderr)
    if p["report_only"]:
        print("report-only: record the pass, write the findings file, then `project close` — do not fix")


def findings_are_current(ffile):
    """True when a findings file exists for this review.

    `project next` moves any previous review's file to `<slug>.prev.md`, so anything at this
    path was written during this area's current loop. The path repeats on every review of an
    area, and without that move a leftover file satisfied the gate while this run's findings
    died with `state.json`. Do not reach for a timestamp instead: `now()` has one-second
    resolution and a whole area can open and close inside that second, so the comparison
    passes exactly when the runs are back to back.
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
        settle_area(cur, ["loop state lost mid-area"])
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
        settle_area(cur, ["the open loop belongs to another scope; this area was never reviewed"])
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
        op = open_pass(state)
        if op is not None:
            # Fix mode reaches this through `blockers()`; report-only never calls it, so the
            # same dangling pass closed the area `reviewed` and counted itself in `passes`.
            # An open pass is a review whose verdict was never transcribed — `pass-record`
            # states it, `pass-abort` discards it, and silence is neither.
            terminal.append(f"pass {op['n']} opened but neither recorded nor aborted")
        elif r is None:
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
        if not terminal and not findings_are_current(ffile):
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
        if r and not findings_are_current(ffile):
            if not a.force:
                die(f"cannot close area: {ffile} is missing — write this reviewer's whole output there; closing deletes the loop state, so "
                    "nothing else survives (--force closes it as incomplete instead, losing the review)")
            # Forcing past it cannot leave the area `accepted`: the record the row would rest
            # on is exactly what was never written, and `--force` never lets an area pass —
            # here as in report-only, it settles the area with the reason attached.
            reasons = reasons + [f"{ffile} was never written — the review did not survive the close"]
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
        # Each metric prints only if the area has one. They are not filled as a block: an
        # area closed before any pass — an empty or renamed one, settled `incomplete` by
        # `project next` — has `passes: 0` and nothing else, and printing the group on the
        # strength of the pass count alone rendered it as `findings None/None score None`.
        if x.get("passes") is not None:
            line += f"  passes {x['passes']}"
        if x.get("findings") is not None:
            resolved = x.get("resolved")
            line += f"  findings {metric(resolved, '?')}/{x['findings']}"
        if x.get("score") is not None:
            line += f"  score {x['score']}"
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

    s = sub.add_parser("init"); s.add_argument("--max-passes", type=positive_int, default=5); s.add_argument("--mode", choices=MODES, default="changes"); s.add_argument("--base", metavar="REF", help="commit to diff against (changes mode); use for already-committed work"); s.add_argument("--task-brief", metavar="TEXT", help="the task's requirements and acceptance criteria, for the reviewer — never rationale or suspected issues"); s.add_argument("--task-brief-file", metavar="PATH", type=user_path, help="same, read from a file"); s.add_argument("--scope-note", metavar="TEXT", help="hunk-level ownership the reviewer must be told, echoed every pass"); s.add_argument("--allow-empty", action="store_true", help="proceed on an empty scoped diff instead of reporting `no-changes`"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*", type=user_path); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("validate"); s.add_argument("--none", action="store_true", help="record that no executable check applies (needs --reason)"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("scope"); s.add_argument("paths", nargs="*", type=user_path); s.set_defaults(fn=cmd_scope)
    s = sub.add_parser("validate-drop"); s.add_argument("--force", action="store_true"); s.add_argument("--none", action="store_true", help="withdraw a `validate --none` record"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate_drop)
    s = sub.add_parser("pass-start"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_pass_start)
    s = sub.add_parser("brief"); s.add_argument("--task-brief", metavar="TEXT"); s.add_argument("--task-brief-file", metavar="PATH", type=user_path); s.add_argument("--scope-note", metavar="TEXT"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_brief)
    s = sub.add_parser("pass-abort"); s.add_argument("--reason"); s.set_defaults(fn=cmd_pass_abort)
    s = sub.add_parser("pass-record"); s.add_argument("--score", type=score_value); s.add_argument("--findings", type=nonneg_int, required=True); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--understood", action="store_true"); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_pass_record)
    s = sub.add_parser("amend"); s.add_argument("--understood", action="store_true"); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--score", type=score_value); s.add_argument("--findings", type=nonneg_int); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("resolve"); s.add_argument("--fixed", type=nonneg_int, default=0); s.add_argument("--withdrawn", type=nonneg_int, default=0); s.add_argument("--adjudicated-invalid", type=nonneg_int, default=0); s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("accept"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fingerprint"); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser("reset"); s.add_argument("--project", action="store_true", help="also discard the project ledger"); s.set_defaults(fn=cmd_reset)

    pr = sub.add_parser("project").add_subparsers(dest="pcmd", required=True)
    s = pr.add_parser("init"); s.add_argument("--max-passes", type=positive_int, default=5); s.add_argument("--report-only", action="store_true", help="collect findings per area without fixing"); s.add_argument("--allow-empty", action="store_true", help="queue areas that match no file"); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*", type=area_token); s.set_defaults(fn=cmd_project_init)
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
