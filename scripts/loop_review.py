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
  init [--max-passes N] [--mode changes|project] [--base REF] [--allow-empty] [--force]
       [--task-brief TEXT | --task-brief-file PATH] [--scope-note TEXT] -- <paths...>
                                           start a loop over paths (task diff or whole area)
  project init [--max-passes N] [--report-only] [--allow-empty] [--force] -- <area paths...>
                                           queue a whole-project review, one loop per area
  project next                             open the loop for the next pending area
  project close [--force]                  record the current area's outcome, free the loop
  project status [--json]                  ledger of areas and outcomes
  validate -- <command...>                 run a validation command, record result
  validate --none --reason TEXT            record that no executable check applies here
  scope -- <paths...>                      widen the open loop's scope (never narrows it)
  validate-drop [--force] [--reason TEXT] {-- <command...> | --none}
                                           retract a record for a command that never ran,
                                           or (--force) retire a check inherited from the last pass
  pass-start [--force]                     open a full-review pass (checks gates)
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
import errno
import hashlib
import json
import os
import shlex
import signal
import stat
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

    A blank argument is refused here, for every path flag at once. `os.path.abspath("")` is
    the cwd, so an empty shell variable (`init -- "$FILE"`, `scope -- "$EXTRA"`) became `.`:
    the scope silently grew to the whole repository — irreversibly, for `scope` — and
    `--task-brief-file ""` tried to read a directory.
    """
    if not v.strip():
        raise argparse.ArgumentTypeError(
            "empty path — an unset variable? A blank argument resolves to the current "
            "directory; name `.` explicitly if that is what you mean")
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
        # cwd or root may be reached through a symlink; compare resolved paths too. Resolve
        # the directories only: `realpath` of the whole path also follows the last component,
        # so `init -- /via/link/CLAUDE.md` scoped `AGENTS.md` — the link's target, which git
        # tracks separately and the dedupe then merged away — and a link pointing outside
        # the repository was refused as "outside" although the link itself lies inside.
        # But only when the last component is an entry *of* the repository — its resolved
        # parent lies inside the root. A link from outside into the repository (`/via/repo`
        # pointing at the root, or at a directory in it) is an entry point, not an entry,
        # and resolving it whole is what makes `init -- /via/repo` mean `.`.
        real_root = os.path.realpath(root)
        head, tail = os.path.split(os.path.abspath(path))
        inside = os.path.relpath(os.path.realpath(head), real_root).split(os.sep)[0] != os.pardir
        target = os.path.join(os.path.realpath(head), tail) if inside else os.path.realpath(path)
        rel = os.path.relpath(target, real_root)
    if rel.split(os.sep)[0] == os.pardir:
        die(f"path is outside the repository: {path}")
    rel = rel.replace(os.sep, "/")
    # The loop's own state is never under review: the fingerprint already excludes it, so a
    # path naming it scoped nothing — yet it was kept in the scope and printed into the
    # reviewer's prompt, where references/project-mode.md and the prompts say it never goes.
    # Every path the operator names passes through here, so one refusal covers init, scope
    # and project init; the warning it used to draw ("does not exist", before the first save
    # created the directory) was false as well.
    if rel == STATE_DIR or rel.startswith(STATE_DIR + "/"):
        die(f"`{path}` is the loop's own state ({STATE_DIR}/), never part of what is reviewed "
            "— leave it out of the paths")
    return rel


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
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        # A full disk or a blocked `.tmp` surfaced as a traceback, and the operator could not
        # tell whether the run had been recorded. Atomicity already answers it: the old file
        # is intact, the new state was not written. Say so; drop a half-written temp.
        if os.path.isfile(tmp) and not os.path.islink(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        die(f"could not write {path} ({e.strerror}) — nothing was recorded; the previous "
            "state is intact. Free the space or clear the obstruction, then repeat the command")


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


def literal(paths):
    """User paths as *literal* pathspecs, for every git call that takes them.

    After `--`, git still reads its arguments as pathspecs: a file named `:foo` was parsed
    as magic and `git` died, and one named `a[1].py` or `*.py` matched as a glob — files the
    operator never scoped entered the fingerprint and the review. `--literal-pathspecs` would
    also disarm the script's own `:(exclude)` items, so each user path carries `:(literal)`.
    """
    return [":(literal)" + os.fsdecode(p) for p in paths]


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
    return [*literal(paths), f":(exclude,glob){STATE_DIR}/**", f":(exclude){STATE_DIR}"]


def scope_files(paths, untracked_only=False):
    """`[(path, bytes | OSError, kind)]`: every file git lists under `paths`, read from disk.

    The one list behind the fingerprint, emptiness and the unseen-path warning. Each asked
    git on its own, and git's index is not the disk: a tracked area deleted with `rm -rf` and
    no `git rm` — the workflow never runs one — was still listed, so `scope_is_empty` called
    it non-empty, `project next` opened a loop over nothing, and the area closed `accepted`;
    a `chmod 000` file hashed as a marker and reached no gate at all. Reading each file once,
    here, makes "what is in scope" mean what a reviewer can actually open.
    Kept as bytes: a filename need not be decodable, and open() takes bytes paths.

    `kind` is the part of an entry's state that is not its bytes, read without following
    links: `b"link"` (the body is the link's target, as git stores it), `b"exec"`, or `b""`.
    Content alone missed both. A review finding "the script is not executable" was fixed with
    `chmod +x` and moved no hash, so `accept` called the fix absent and `pass-start` called the
    state unchanged — a dead end; a link was hashed as whatever it pointed at.
    """
    flags = ["--others"] if untracked_only else ["--cached", "--others"]
    out = git("ls-files", "-z", *flags, "--exclude-standard", "--", *literal(paths))
    files = []
    for p in sorted({p for p in out.split(b"\0") if p and outside_state_dir(p)}):
        try:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                files.append((p, os.readlink(p), b"link"))
                continue
            with open(p, "rb") as f:
                files.append((p, f.read(), b"exec" if st.st_mode & stat.S_IXUSR else b""))
        except OSError as e:
            files.append((p, e, b""))
    return files


def readable(files):
    return [p for p, body, _ in files if not isinstance(body, OSError)]


def unreadable(files):
    """Files present on disk that cannot be read. A tracked file deleted on disk is simply
    absent from the state under review, and a directory entry is a submodule's gitlink —
    neither is something a reviewer was meant to open."""
    return [p for p, body, _ in files if isinstance(body, OSError)
            and body.errno not in (errno.ENOENT, errno.EISDIR)]


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

    def files(listed):
        for p, body, kind in listed:
            # The kind rides in the path field after a NUL, which no path can contain: the
            # framing stays unambiguous, and a plain file hashes exactly as it always did, so
            # loops already in flight are not made stale by the format alone.
            field(p + b"\0" + kind if kind else p)
            # Fold a failure in: an unreadable file must not hash like an empty one.
            field(f"<unreadable:{body.errno}>".encode() if isinstance(body, OSError) else body)

    if mode == "project":
        files(scope_files(paths))
        return h.hexdigest()[:16]
    # check=True on both git calls: a failure here must be loud. A silently empty hash is
    # indistinguishable from "nothing changed", which is the one lie that breaks every gate.
    field(git("diff", base or diff_base(), "--", *scoped(paths)))
    # -z: NUL-separated and unquoted, so paths with spaces, newlines or non-ASCII
    # characters survive intact. Splitting plain `ls-files` output would corrupt them
    # and silently drop those files from the hash.
    files(scope_files(paths, untracked_only=True))
    return h.hexdigest()[:16]


def unseen_paths(paths, mode="changes", base=None):
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
        return [x for x in git(*args, "--", *literal([p])).split(b"\0") if x]

    out = []
    for p in paths:
        listed = scope_files([p])
        if readable(listed):
            continue                                   # the fingerprint can see it
        # In `changes` mode the fingerprint is also the scoped diff against the base, and a
        # deletion lives there and nowhere on disk — `rm`, `git rm`, `git mv`, or a commit
        # since `--base`; it may be the very change under review. So ask the diff, not the
        # index: the index still lists a file added and then removed, which no diff carries,
        # and has already dropped one `git rm` deleted, which the diff does. Never for an
        # unreadable path: that is reported below, and `git diff` cannot open it either.
        if (mode != "project" and not unreadable(listed)
                and git("diff", base or diff_base(), "--", *scoped([p])).strip()):
            continue
        # Why it is invisible decides what the operator should do about it, so the reasons
        # are told apart rather than guessed from `os.path.exists`. Calling an empty
        # directory "ignored by git" sent them to a `.gitignore` that never mentioned it,
        # when the real answer is that there is nothing in it yet.
        everything = entries(p, False)
        if not os.path.exists(p):
            why = ("does not exist on disk (git still lists it — deleted without `git rm`)"
                   if listed else "does not exist")
        elif unreadable(listed):
            why = "cannot be read — no reviewer can open what is in it"
        elif listed:
            why = "holds only files deleted on disk that git still lists"
        elif not everything:
            why = ("is an empty directory" if os.path.isdir(p)
                   else "holds nothing git can see")
        elif not [x for x in everything if outside_state_dir(x)]:
            why = "holds only the loop's own state, which every scope excludes"
        else:
            why = "exists, but everything in it is ignored by git"
        out.append((p, why))
    return out


def warn_unseen(paths, mode="changes", base=None):
    unseen = unseen_paths(paths, mode, base)
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
    falls inside the scope. `project`: empty when the paths hold no file that can be read
    from disk — a mistyped or renamed area, one deleted without `git rm`, or one nobody may
    open, which would otherwise be queued, reviewed by nobody and closed as accepted.
    """
    if mode == "project":
        return not readable(scope_files(paths))
    if git("diff", base or diff_base(), "--", *scoped(paths)).strip():
        return False
    return not readable(scope_files(paths, untracked_only=True))


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


def refuse_if_open(state, verb, what):
    """Refuse `verb` while a pass is open: it would change `what` under a review in flight.

    One guard for every command that edits what a review rests on. They learned it one at a
    time — `amend`, then `resolve` — and `brief` never did: a brief replaced while the
    reviewer was reading made `pass-record` file that verdict as a review against terms it
    was never given. The open pass is settled first, by recording or aborting it.
    """
    op = open_pass(state)
    if op is not None:
        die(f"pass {op['n']} is open — `pass-record` states its verdict, `pass-abort` "
            f"discards it. `{verb}` changes {what}, which that review is reading.")


def terms_of(state):
    """Hash of the review's terms: the brief and the scope note, the two things besides the
    code a reviewer is given. A pass stamps it at `pass-start`; the fingerprint covers only
    the code, so terms changed after a pass left that pass "current" — `accept` signed off on
    requirements no reviewer saw, and `pass-start` refused the new pass as unchanged."""
    blob = json.dumps([state.get("task_brief"), state.get("scope_note")]).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def terms_moved(state, lp):
    """True when `lp` was reviewed against other terms. Passes stamped before the field
    existed carry no record of what they were given, and are not second-guessed."""
    return "terms" in lp and lp["terms"] != terms_of(state)


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
    # A run can never hold this key now; a record that does and failed was one, from before.
    return v.get("cmd") == NO_CHECK and v.get("exit") == 0


def is_retirement(v):
    """True for a `validate-drop --force` retirement: an event in the log, not a run.

    It is appended, never stamped onto the records it supersedes, and it carries `retracted`
    so that no reader of runs can mistake it for one. Correcting a record — a typo, a missing
    tool, a mistaken declaration — marks that record retracted in place and is *not* a
    retirement: it says the run never happened, not that the check left the set.
    """
    return v.get("exit") is None and bool(v.get("retracted"))


def evidence_set(records):
    """The commands of `records` that a later pass must carry: real runs, never declarations."""
    return {c: v for c, v in records.items() if not is_declaration(v)}


def validation_view(state, fp=None):
    """The loop's validation state as the gates need it: `(records, retired)`.

    `records` maps each command to its latest usable run **at `fp`** (default: the current
    fingerprint). Re-running a command on unchanged files supersedes its earlier result, so
    a flaky or environment failure clears by re-running — not only by editing — and distinct
    commands stay separate: a green re-run of the tests does not absolve a red lint.

    `retired` holds the commands whose last retirement event has not been followed by a run.
    That is deliberately *not* scoped to a fingerprint: retiring a check is a decision about
    the check, made once by a human with `validate-drop --force`, and it holds until the
    command is run again. Keyed by fingerprint instead, the decision evaporated at the next
    edit and `pass-start` demanded the same `--force` over again — turning the escape hatch
    into a routine keystroke, which is exactly how an escape hatch stops meaning anything.
    """
    fp = state["fingerprint_current"] if fp is None else fp
    at_fp, retired = {}, set()
    for v in state["validation"]:
        # Append-only, so list order is chronological: a later run of a command supersedes
        # its retirement, and a retirement supersedes every earlier run of it — on any
        # fingerprint, or returning to a state where it once ran would bring it back.
        if is_retirement(v):
            at_fp.pop(v["cmd"], None)
            retired.add(v["cmd"])
            continue
        if v.get("retracted"):
            # A corrected record never happened: it neither retires nor un-retires anything.
            continue
        retired.discard(v["cmd"])
        if v["fingerprint"] == fp and is_evidence(v):
            at_fp[v["cmd"]] = v
    return at_fp, retired


def validation_at(state, fp):
    return validation_view(state, fp)[0]


def current_validation(state):
    return list(validation_at(state, state["fingerprint_current"]).values())


def pass_evidence(state, lp):
    """The commands pass `lp` rested on, as recorded when it opened (`rested_on`).

    One answer for every gate that asks it. Each used to compute it inline, and only "when
    the scope has moved since": standing on the reviewed fingerprint — right after
    `pass-record`, or after reverting a fix — the set came out empty, so a check re-run there
    to exit 126 read as a typo no pass leaned on, and a plain `validate-drop` retracted the
    green record the review was granted on along with it. It is a fact about the pass, so
    nothing done afterwards rewrites it: a typo typed later on the same state is not in it,
    and a check retired later is still in it — taking it out is `retired`'s job, read from
    the log's order. Passes recorded before the set was stored fall back to the log.
    """
    if lp is None:
        return set()
    if "rested_on" in lp:
        return set(lp["rested_on"])
    return set(evidence_set(validation_at(state, lp["fingerprint"])))


def pass_rested_on(state, cmd):
    """The latest pass — recorded or open, never aborted — whose evidence set holds `cmd`.

    The question `validate-drop` and the `pass-start` hint ask: did *any* review rest on this
    check? Asked of the last recorded pass alone, it missed a pass still open on this very
    state, and a check an earlier pass rested on that a later one was opened without
    (`pass-start --force`) — and either way a re-run to 126/127 turned the check into a typo
    a plain `validate-drop` could clear. Fingerprints play no part: membership is a fact
    about the pass (`pass_evidence`). A forced retirement still ends the matter until the
    command is run again; that is `retired`, asked separately.
    """
    for x in reversed(state["passes"]):
        if not x.get("aborted") and cmd in pass_evidence(state, x):
            return x
    return None


def missing_since_last_pass(state):
    """`{cmd: pass}` for checks a recorded pass rested on that have no record here.

    Evidence may not silently shrink: after a fix batch, re-running one quick check would
    otherwise be enough to open the next pass, and acceptance only ever looks at the
    commands present on the accepted fingerprint. So a later pass may not be granted on a
    weaker set than the passes before it (SKILL.md steps 2 and 5).

    Every recorded pass, not the last one. Read from the last alone, one pass opened
    without a check — before `pass-start --force` stopped skipping this gate, or in a state
    written then — dropped it for good: the next gate compared against the pass that lacked
    it, and `accept` signed off on the shrunken set with no retirement anywhere in the log.
    The only way out of the set is `validate-drop --force`, which `retired` reports.
    """
    present, retired = validation_view(state)
    out = {}
    for x in state["passes"]:
        if x.get("result") is None or x.get("aborted"):
            continue
        # A retired check is one a human took out of the set; without this the set could
        # only ever grow and `validate-drop` could not do what its message promises.
        for c in pass_evidence(state, x):
            if c not in present and c not in retired:
                out[c] = x
    return dict(sorted(out.items()))


def describe_missing(missing):
    return "; ".join(f"`{c}` (pass {x['n']})" for c, x in missing.items())


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


def validation_summary(cur):
    """The aggregate label for a set of validation records, in one place.

    A `validate --none` record carries `exit: 0` so that exit condition 1 is satisfied, but
    it is a declaration that nothing ran. The header asked only "are all exits zero?", so a
    loop whose entire validation was that declaration printed `1 command(s), GREEN` — the
    exact reading AGENTS.md forbids ("it is not a green `true`"), and the line an agent
    transcribes into the reviewer's prompt. The per-record rendering was fixed for this once;
    the header kept its own copy of the question and kept answering it wrong.
    """
    if not cur:
        return "RED/none"
    runs = [v for v in cur if not is_declaration(v)]
    if any(v["exit"] != 0 for v in runs):
        return "RED/none"
    if not runs:
        return "NOTHING RAN (absence declared)"
    return "GREEN"


def print_validation(cur, header="validation on current state", state=None, file=None):
    """One rendering of validation state for every text-mode command, diagnostics included.

    `references/reviewer-prompt.md` is filled from `status`, and its validation section
    needs each command with its exit code — an aggregate GREEN/RED line cannot be
    transcribed into the reviewer's prompt.

    `file` exists so refusals printed to stderr use this renderer instead of writing their
    own loop. One of them did, and printed a `validate --none` record as
    `<no executable check applies> -> exit 0` — a declaration that nothing was run, dressed
    as a command that ran and passed, in the very list an operator reads to decide what to
    retract.
    """
    out = sys.stdout if file is None else file
    print(f"{header}: {len(cur)} command(s), {validation_summary(cur)}", file=out)
    for v in cur:
        if is_declaration(v):
            print(f"  - no executable check applies"
                  + (f": {v['reason']}" if v.get("reason") else ""), file=out)
        elif v["exit"] != 0 and state is not None:
            print(f"  - {v['cmd']}  -> exit {v['exit']}  [{red_provenance(state, v['cmd'])}]", file=out)
        else:
            print(f"  - {v['cmd']}  -> exit {v['exit']}", file=out)


def review_integrity(state, phrasing="loop"):
    """What makes a review not a review, in either mode: the list both gates read.

    `blockers()` adds the acceptance-only checks on top; report-only `project close` uses
    this alone, because nothing there is meant to be fixed. They were two hand-written lists
    and they drifted twice, always in the same direction — a condition reached one side and
    not the other. The open-pass check arrived in report-only long after `blockers()` had it,
    and the `allow_empty` exemption never arrived at all, so an area queued deliberately
    empty closed `incomplete` however carefully its report was written.

    `phrasing` only selects wording: an area reviewer is told about "the area", a task loop
    about "the scope". The conditions are the same, and that is the point.
    """
    area = phrasing == "area"
    out = []
    # Checked here, not only at `init`. A scope can empty *after* the loop starts — the work
    # is committed, a change is reverted or stashed, an area's directory is deleted — and
    # from that moment the loop reviews nothing while passing every gate exactly as it would
    # on an empty `init`. In project mode the lie then outlives the loop in the ledger.
    if not state.get("allow_empty") and scope_is_empty(state["scope"], state.get("mode", "changes"),
                                                       state.get("base")):
        out.append("the area's files are gone; the review describes nothing" if area else
                   "the scope is empty now — nothing is under review "
                   "(reverted, stashed, committed, or the area's files are gone)")
    # A file in scope nobody may open was never reviewed, whatever the rest of the area
    # holds. The fingerprint folds it in as a marker so it cannot hash like an empty file;
    # this is where it stops being silent.
    locked = unreadable(scope_files(state["scope"], untracked_only=state.get("mode", "changes") != "project"))
    if locked:
        out.append("unreadable file(s) in scope, which no reviewer could open: "
                   + ", ".join(os.fsdecode(p) for p in locked))
    op = open_pass(state)
    if op:
        out.append(f"pass {op['n']} opened but neither recorded nor aborted" if area else
                   f"pass {op['n']} opened but not recorded (pass-record, or pass-abort if it is stale)")
        return out
    lp = last_recorded_pass(state)
    if lp is None:
        out.append("no recorded pass" if area else "no full-review pass recorded")
        return out
    if state["fingerprint_current"] != lp["fingerprint"]:
        out.append("the area moved since the pass; the review describes code that is no "
                   "longer there" if area else
                   "scoped changes moved since the last pass; review is stale")
    if terms_moved(state, lp):
        out.append(f"the brief or scope note changed since pass {lp['n']}, so that review "
                   "was given other terms; open a new pass")
    # Condition 1's floor, asked of both modes. Report-only does not need the checks green —
    # an area is reviewed as it stands — but it needs them to *exist*: `validate-drop --none`
    # or a retracted record could leave the reviewed state with no validation at all, and a
    # 126/127 is evidence about nothing, so the area closed `reviewed` on checks nobody ran.
    cur = current_validation(state)
    if not cur:
        out.append("no validation run against the current state")
    never_ran = [v for v in cur if v["exit"] in NEVER_RAN]
    if never_ran:
        out.append("validation red: " + ", ".join(f"`{v['cmd']}` never ran (exit {v['exit']})"
                                                  for v in never_ran))
    missing = missing_since_last_pass(state)
    if missing:
        out.append("check(s) a recorded pass rested on have no record on the current state: "
                   + describe_missing(missing))
    if not lp["result"]["understood"]:
        out.append("reviewer did not demonstrate a credible understanding"
                   + ("" if area else " of the change"))
    # A required part of the reviewer's output in both modes (SKILL.md step 3, section C of
    # the area prompt), so it lives here and not in `blockers()`: there, report-only `project
    # close` never asked it, and an area whose reviewer gave no verdict closed `reviewed`
    # with `test_evidence: null`. What the verdict *says* — `inadequate` — stays an
    # acceptance matter: in report-only it is a finding to report, not a missing review.
    if lp["result"].get("test_evidence") is None:
        out.append(f"test evidence not assessed for pass {lp['n']} "
                   "(record it with `amend --test-evidence ...`)")
    return out


def resolution(r):
    """A pass result's resolution state, derived from `resolution_log` — the one record.

    `resolved` was a counter kept beside the log and read on its own, and the two came apart:
    `amend --findings` raised the total without re-reading what the log had already claimed,
    and `blockers()` asked `any(fixed)` of the log, so one stray `resolve --fixed` blocked
    `accept` for good on the reviewed state even after every finding was withdrawn or
    adjudicated invalid — a review that asked for no fix at all. Everything here is read from
    the log, so whoever asks gets the same answer.

    `needs_fix` is how many findings still stand once the dismissed ones are taken out: zero
    means nothing has to change on disk, so standing on the reviewed fingerprint is honest.
    A result written before the log existed keeps its stored counter as `unattributed`
    (`cmd_resolve` seeds it into the log): it counts as resolved and, as it always did, it
    does not tie the review to a fix the script cannot know was made.
    """
    findings = r["findings"]
    log = r.get("resolution_log")
    if log is None:
        log = [{"unattributed": r.get("resolved") or 0}]
    fixed = sum(x.get("fixed", 0) for x in log)
    dismissed = sum(x.get("withdrawn", 0) + x.get("adjudicated_invalid", 0) for x in log)
    unattributed = sum(x.get("unattributed", 0) for x in log)
    claimed = fixed + dismissed + unattributed
    return {"fixed": fixed, "dismissed": dismissed, "claimed": claimed,
            "resolved": min(findings, claimed),
            "needs_fix": max(0, findings - dismissed - unattributed)}


def blockers(state):
    """Return list of reasons acceptance is not possible. Empty list == accepted."""
    reasons = review_integrity(state)
    # Stop where `review_integrity` stops. With a newer pass open, the last recorded one is
    # superseded: judging its findings, fixes and verdict reported blockers about a review
    # no gate will read again, and each came with a recovery (`amend`, `resolve`) that
    # `require_recorded_pass` refuses in exactly this state. The open pass is the blocker.
    lp = last_recorded_pass(state)
    if lp is None or open_pass(state) is not None:
        return reasons
    # Absent and never-ran validation are `review_integrity`'s; a check that ran and failed
    # is an acceptance matter — in report-only it is the area's state, to be reported.
    red = [v for v in current_validation(state) if v["exit"] != 0 and v["exit"] not in NEVER_RAN]
    if red:
        reasons.append(f"validation red: {red[-1]['cmd']}")
    res = resolution(lp["result"])
    unresolved = lp["result"]["findings"] - res["resolved"]
    if unresolved > 0:
        reasons.append(f"{unresolved} unresolved actionable finding(s) from pass {lp['n']}")
    else:
        # Findings that stand after withdrawals and adjudications were resolved by fixing;
        # if the scope is back on the fingerprint the review found defective, those fixes
        # are not on disk. None standing means nothing had to change, whatever `--fixed`
        # was also typed — a stray one must not lock `accept` for good.
        # Standing on the reviewed fingerprint *is* the proof that the fixes are absent —
        # a fix changes the code, so it moves the hash. Excusing the case where the fix was
        # recorded on that same fingerprint excused the only way to reach it deliberately:
        # `resolve --fixed` typed before touching a file left the log pointing at the
        # reviewed state, the exemption fired, and `accept` signed off the defect itself.
        if res["needs_fix"] and state["fingerprint_current"] == lp["fingerprint"]:
            reasons.append("the scope is on the state the review found defective — the "
                           "recorded fixes are not present (never made, reverted, stashed, "
                           "checked out, or landed outside the scope: widen it with `scope --`)")
    # "Not assessed" is `review_integrity`'s; only the verdict's content gates acceptance.
    if lp["result"].get("test_evidence") == TEST_EVIDENCE_BLOCKING:
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
    # Asked whether or not a loop state exists: an area whose state was lost — a crash, or
    # `reset` clearing a foreign loop — is still in progress in the ledger, and a fresh
    # `init` there was the one loop nothing stopped from being read as that area's.
    if os.path.exists(PROJECT_FILE):
        try:
            busy = next((x for x in read_json(PROJECT_FILE)["areas"] if x["status"] == "in_progress"), None)
        except (OSError, ValueError, KeyError):
            busy = None
        if busy:
            if os.path.exists(STATE_FILE):
                die(f"area `{area_name(busy)}` is mid-loop — `project close` records its outcome "
                    "first. Re-initialising here would close that area from a review of something "
                    "else; `--force` does not cover that.")
            die(f"area `{area_name(busy)}` is in progress and its loop state is gone — "
                "`project close --force` settles it `incomplete` and the queue continues with "
                "`project next`. A loop started here would be read as that area's; `--force` "
                "does not cover that.")
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
    # The pinned commit, never the text the operator typed: every later gate reads
    # `state["base"]`, and the check that refuses an empty loop must ask the same question.
    if scope_is_empty(scope, a.mode, base):
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
    warn_unseen(scope, a.mode, base)
    save(state)
    # The pinned id, as `status` prints it: the agent copies this into the reviewer's prompt,
    # and `HEAD~1` there names a different commit the moment anything is committed.
    print(f"loop initialised ({a.mode}{' vs ' + base if base else ''}): {len(scope)} path(s), max {a.max_passes} passes, fingerprint {state['fingerprint_current']}")
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
    refuse_if_open(state, "scope", "what is under review")
    new = [x for x in dict.fromkeys(added) if x not in state["scope"]]
    if not new:
        die("every path given is already in scope")
    warn_unseen(new, state.get("mode", "changes"), state.get("base"))
    before = fp_of(state)
    state["scope"] = state["scope"] + new
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"scope widened by {len(new)} path(s):")
    for x in new:
        print(f"  + {x}")
    if state["fingerprint_current"] != before:
        # "The last review is stale" is only true when there is one; before the first pass the
        # same move just means the first reviewer must see the wider scope.
        if last_recorded_pass(state) is not None:
            print("the reviewed state moved; the last review is now stale — re-validate and "
                  "open a new pass so a reviewer actually sees the added paths")
        else:
            print("the scope moved before any review was recorded — validate the new state; "
                  "the first pass will cover the added paths")


def check_none_or_command(a, verb, empty_hint, reason_needs_none=False):
    """The whole flag combination for `validate`/`validate-drop`, checked in one place.

    `--none` is about the declaration, a command after `--` is about a run: exactly one. And
    in `validate`, `--reason` belongs to the declaration alone — it is the sentence the
    reviewer is given in place of a check. Passed alongside a command it was read by nothing
    and dropped without a word, so an operator who explained why a check is unusual got a
    record that does not carry the explanation. Checking pairs one at a time is what left
    that gap: the previous pass closed `--none` + command and stopped there.

    `validate-drop` keeps `--reason` in both forms — there it annotates the retraction, not
    the declaration, and a retraction has a reason whichever record it names.
    """
    if a.none and a.command:
        die(f"{verb} takes --none or a command after `--`, not both: `--none` records that "
            "no executable check applies, a command names one to run")
    if not a.none and not a.command:
        die(empty_hint)
    # The command itself is input too. `validate -- ""` (or only blanks) ran an empty shell
    # line, exited 0 and was recorded GREEN — condition 1 met by running nothing. And the
    # declaration's internal key, typed as a command, was recorded under that key: read as a
    # declaration by every gate, and retracted along with the real one by `--none`.
    if a.command and not any(p.strip() for p in a.command):
        die(f"{verb}: the command after `--` is empty — name the check to run, or use "
            "`--none --reason ...` when none applies")
    if a.command and join_cmd(a.command) == NO_CHECK:
        die(f"{verb}: `{NO_CHECK}` is the declaration's record, not a command — use `--none`")
    if a.reason is not None and not a.reason.strip():
        die(f"{verb} --reason is blank: say why, in words")
    if reason_needs_none and a.reason and not a.none:
        die(f"{verb} --reason belongs to `--none`: it is what the reviewer is told in place "
            "of a check. A command's record carries its exit code, not a reason")


def cmd_validate(a):
    os.chdir(repo_root())
    state = load()
    check_none_or_command(a, "validate",
                          "validate needs a command after `--`, or `--none --reason ...` "
                          "when none applies", reason_needs_none=True)
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
                                    "reason": a.reason.strip()})
        save(state)
        print(f"recorded: no executable check applies — {a.reason.strip()}")
        print("tell the reviewer exactly this in the validation section of its prompt; "
              "do not present it as a check that passed")
        return
    cmd = join_cmd(a.command)
    before = fp_of(state)
    print(f"$ {cmd}", flush=True)
    try:
        r = subprocess.run(cmd, shell=True)
    except KeyboardInterrupt:
        # Ctrl-C reaches the check and this script alike. A run that did not finish is
        # evidence about nothing, so nothing is recorded — but that used to be a traceback,
        # leaving the operator to guess. Say it, and say whether the files moved meanwhile:
        # every gate recomputes the fingerprint, so a moved scope simply has no validation
        # until the check is run again on what is there now.
        moved = fp_of(state) != before
        die("validation interrupted — nothing recorded"
            + ("; the scoped files changed while it ran, so re-run it once they settle"
               if moved else "; re-run it to record a result"), code=130)
    after = fp_of(state)
    # A formatter, a snapshot-updating test run or a codegen step edits the scope while it
    # validates it. Recording the result against `after` would attach green evidence to a
    # state nothing ran on, and `pass-start` would then open the pass on it — manufacturing
    # the one thing the fingerprint exists to forbid. Neither end is evidence, so the record
    # is kept, marked, and ignored by the gate until the command is re-run on a settled
    # state. Recording it against `before` would be worse: that state no longer exists.
    moved = before != after
    state["fingerprint_current"] = after
    # A process killed by signal N comes back as -N, and `sys.exit(-9)` left this script
    # with status 247 while the record and the message said -9: three numbers for one run.
    # The shell's own convention is 128+N, which is what `$?` shows and what a caller of
    # "exits with its status" expects; the signal is kept beside it so the cause is named.
    sig = -r.returncode if r.returncode < 0 else None
    status = 128 + sig if sig else r.returncode
    rec = {"cmd": cmd, "exit": status, "at": now(), "fingerprint": after}
    if sig:
        rec["signal"] = sig
    if moved:
        rec["moved_scope"] = True
        rec["fingerprint_before"] = before
    state["validation"].append(rec)
    save(state)
    if moved:
        print(f"loop-review: the command changed the scope ({before} -> {after}); this run is not "
              "evidence for either state — re-run it now that the files have settled",
              file=sys.stderr)
    killed = ""
    if sig:
        try:
            killed = f", killed by {signal.Signals(sig).name}"
        except ValueError:
            killed = f", killed by signal {sig}"
    print(f"loop-review: validation {'GREEN' if status == 0 else 'RED'} (exit {status}{killed})")
    sys.exit(status)


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
    refuse_if_open(state, "validate-drop", "the validation that review was handed")
    check_none_or_command(a, "validate-drop",
                          "validate-drop needs a command after `--`, or --none to withdraw "
                          "a `validate --none` record")
    # `--none` names the same record `validate --none` wrote. Without it the only way to
    # withdraw a mistaken declaration was to type the script's internal marker verbatim.
    cmd = NO_CHECK if a.none else join_cmd(a.command)
    state["fingerprint_current"] = fp_of(state)
    # Records still in effect: a retirement supersedes every run of the command before it.
    hits = []
    for v in state["validation"]:
        if v["cmd"] != cmd:
            continue
        if is_retirement(v):
            hits = []
        elif v["fingerprint"] == state["fingerprint_current"] and not v.get("retracted"):
            hits.append(v)
    lp = pass_rested_on(state, cmd)
    # Membership in a pass's evidence set, decided on its own — *not* on whether the
    # command also has a record here. Reading it as "no record here" left the rule open from
    # the other side: running an inherited check so it fails to start (126/127) put it in the
    # never-ran branch, where a plain `validate-drop` retired it and `pass-start` accepted the
    # shrunken set. A check the review was granted on stays evidence even once the tool that
    # ran it is gone; what changed is the environment, and that is the human's call to weigh.
    inherited = lp is not None
    _, retired = validation_view(state)
    if cmd in retired and not hits:
        # Already out of the set and still out — the retirement holds until the command is
        # run again. Saying "no validation record" here sent the operator to re-force a
        # decision that was never undone.
        die(f"`{cmd}` is already retired; it stays out of the set until it is run again")
    if not hits and not inherited:
        # Named as the operator typed it: the declaration's record key is internal.
        what = "`validate --none` declaration" if a.none else f"validation record for `{cmd}`"
        print(f"loop-review: no {what} at the current state", file=sys.stderr)
        cur = current_validation(state)
        if cur:
            print_validation(cur, "recorded on the current state", state, file=sys.stderr)
        sys.exit(1)
    if inherited:
        # A check a pass rested on — green, or in project mode failing: `pass-start` opens a
        # project pass over red and stamps it `opened_over_red`. Either way retiring it is a
        # human call, never the agent's. A green one costs the review evidence it was granted
        # on; a failing one is worse to lose quietly, because it takes a known failure out of
        # what every later pass and `accept` must face. The message says which it is: reading
        # the set as "everything here passed" was true only in task mode.
        if not a.force:
            kind, why = record_state(hits)
            failing = cmd in (lp.get("opened_over_red") or [])
            tail = ("retiring it is still a human call (--force), and it costs the review "
                    "that evidence" if kind == "green" else
                    "re-run it, or retire it with --force (that weakens the evidence set)")
            if failing:
                tail += (f" — it was failing when pass {lp['n']} opened, so retiring it also "
                         "hides that failure from every later gate")
            die(f"`{cmd}` carried pass {lp['n']} and {why}; {tail}")
        # The retirement is its own entry and touches no record. Marking the runs in place
        # was invisible to `validation_view`, which reads events by list position (a later
        # run on another fingerprint stayed the latest event), and it rewrote the history
        # `pass_evidence` had been read from. The runs stay; the event supersedes them.
        state["validation"].append({
            "cmd": cmd, "exit": None, "at": now(), "fingerprint": state["fingerprint_current"],
            "retracted": {"at": now(), "reason": a.reason or (
                f"retired: inherited from pass {lp['n']}" + ("" if hits else ", not re-run"))},
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
    if not a.force:
        # Only what never ran is bookkeeping. The verdict above reads the latest record; the
        # earlier ones on this state may be results — a green run before the tool vanished —
        # and retracting every hit took them along under a waiver granted for a 127.
        hits = [v for v in hits if is_declaration(v) or v["exit"] in NEVER_RAN]
    for v in hits:
        v["retracted"] = {"at": now(), "reason": a.reason or ("forced" if a.force else "never ran")}
    save(state)
    print(f"retracted {len(hits)} record(s) for `{cmd}`")
    print_validation(current_validation(state), state=state)


def pass_start_blockers(state, force=False):
    """Why `pass-start` would refuse right now, as `(key, payload)` in gate order.

    The gate is not the only thing that answers this question: `pass-abort` tells the
    operator what the next `pass-start` will do, and it used to answer from one condition of
    the several — it compared fingerprints and promised the pass would be granted, while a
    pass recorded earlier on that same fingerprint made the gate refuse. A hint that names
    the way out has to be computed by the thing that decides it.
    """
    out = []
    op = open_pass(state)
    if op:
        return [("open", op)]
    lp = last_recorded_pass(state)
    if len(state["passes"]) + 1 > state["max_passes"] and not force:
        out.append(("limit", state["max_passes"]))
    # Unchanged means the code *and* the terms: a new brief is a new question about the
    # same code, and refusing it as "unchanged" left the old review standing for both.
    if (lp and lp["fingerprint"] == state["fingerprint_current"]
            and not terms_moved(state, lp) and not force):
        out.append(("unchanged", lp))
    if not state.get("allow_empty") and scope_is_empty(state["scope"],
                                                       state.get("mode", "changes"),
                                                       state.get("base")):
        out.append(("empty", None))
    cur = current_validation(state)
    red = [v for v in cur if v["exit"] != 0]
    if not cur:
        out.append(("no-validation", None))
    never_ran = [v for v in red if v["exit"] in NEVER_RAN]
    if never_ran:
        out.append(("never-ran", never_ran))
    if red and not never_ran and state.get("mode", "changes") != "project":
        out.append(("red", red))
    # Not waived by `force`. That flag answers the pass limit and an unchanged scope — a
    # human asking for one more look — and neither question is "may the evidence shrink".
    # Waiving this here opened a pass on a weaker set with no retirement in the log; the one
    # door out of the set is `validate-drop --force`, which records the decision.
    missing = missing_since_last_pass(state)
    if missing:
        out.append(("missing", missing))
    return out


def pass_start_refusal(state, key, payload):
    """`(message, exit code)` for one `pass_start_blockers` reason, worded once.

    `pass-start` dies with it; `pass-abort` quotes it as what the next `pass-start` will say.
    The abort note had its own explanation — "the scope has not moved, and the abort is not
    what stands in the way" — printed for every reason, though at the pass limit the abort
    *is* what used the last pass, and an edit unblocks neither a limit, a red check nor a
    missing one. A hint about the gate is the gate's own sentence or it drifts.
    """
    def refusal(msg, code=1):
        return msg, code
    if key == "open":
        return refusal(f"pass {payload['n']} is still open; record it, or `pass-abort --reason ...` if the "
            "scope moved while the reviewer was reading and its review is of a state that "
            "no longer exists")
    if key == "limit":
        return refusal(f"pass limit {payload} reached — outcome is INCOMPLETE unless the user asked for persistence (then use --force)")
    if key == "unchanged":
        return refusal("scoped changes are unchanged since the last pass; clarify with the same reviewer instead of opening a new full review (--force to override)")
    if key == "empty":
        return refusal("the scope is empty now — nothing is under review. It emptied after `init`: the "
            "work was committed (re-`init` with `--base`), reverted or stashed, or the area's "
            "files are gone. A pass over nothing is not a review.", code=NO_CHANGES)
    if key == "no-validation":
        return refusal("no validation recorded for the current state; run `validate` first")
    if key == "never-ran":
        # A 126/127 record means the command never executed. Project mode tolerates a
        # *failing* check because an inherited area is reviewed as it stands — but a typo
        # or a missing tool is not a property of the area, and letting it through would
        # stamp it `[inherited]` and tell a fresh reviewer that the area's own checks
        # fail. Refuse in every mode. Two different exits, and naming the wrong one is
        # how the gate got walked around: a check the last pass rested on is still
        # evidence once its tool disappears, so retiring it needs `--force`.
        carried = [v["cmd"] for v in payload if pass_rested_on(state, v["cmd"])]
        return refusal("these command(s) never ran (missing tool or typo), so they are evidence about "
            "nothing: " + "; ".join(f"`{v['cmd']}` (exit {v['exit']})" for v in payload)
            + " — fix the command and re-run it, or retract the record with `validate-drop`."
            + (" Note that " + "; ".join(f"`{c}`" for c in carried)
               + " carried a recorded pass, so retiring it takes `validate-drop --force`, "
                 "a human decision." if carried else ""))
    if key == "red":
        return refusal("validation is red for the current state; fix it, or re-run the command if the failure was environmental "
            "(if it never ran at all — typo, missing tool — retract it with `validate-drop`)")
    if key == "missing":
        return refusal(f"recorded passes rested on {len(payload)} check(s) not re-run since the change: "
            + describe_missing(payload)
            + " — re-run them. `pass-start --force` does not waive this. Retiring one of these weakens the evidence this review rests on, "
              "even if the tool that ran it is gone, so it needs `validate-drop --force` — "
              "a human decision. (Plain `validate-drop` only clears a command that never ran "
              "here and no pass rested on.)")
    raise ValueError(f"no wording for pass-start blocker {key!r}")


def cmd_pass_start(a):
    os.chdir(repo_root())
    state = load()
    state["fingerprint_current"] = fp_of(state)
    lp = last_recorded_pass(state)
    n = len(state["passes"]) + 1
    for key, payload in pass_start_blockers(state, a.force):
        msg, code = pass_start_refusal(state, key, payload)
        die(msg, code=code)
    cur = current_validation(state)
    red = [v for v in cur if v["exit"] != 0]
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
        print_validation(red, "failing checks", state, file=sys.stderr)
        print("  in report-only write them into the findings file; in fix mode they still "
              "block `accept`, so fix them or close the area incomplete with them as the "
              "blocker", file=sys.stderr)
    # What the pass rested on, fixed now: later runs on this fingerprint and later
    # retirements must not rewrite it (`pass_evidence`).
    entry = {"n": n, "opened": now(), "fingerprint": state["fingerprint_current"],
             "terms": terms_of(state), "result": None,
             "rested_on": sorted(evidence_set(validation_at(state, state["fingerprint_current"])))}
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
    refuse_if_open(state, "brief", "the terms the reviewer was given")
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
    lp = last_recorded_pass(state)
    if lp is not None and terms_moved(state, lp):
        print(f"note: pass {lp['n']} was reviewed against other terms and is now stale — "
              "open a new pass so a reviewer sees these", file=sys.stderr)


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
    # Never below zero: passes opened past the limit with `--force` leave nothing, not "-1".
    left = max(0, state["max_passes"] - len(state["passes"]))
    print(f"pass {lp['n']} aborted ({lp['abort_reason']}); it counts as used. "
          f"{left} of {state['max_passes']} left")
    # Answered by the gate itself, not by re-deriving one of its conditions. This note used
    # to compare fingerprints and promise the next pass would be granted — true only when no
    # *recorded* pass sits on this fingerprint. After a pass opened with `--force` on an
    # unchanged scope and then aborted, the gate refused the pass the note had promised.
    blocking = pass_start_blockers(state)
    if not blocking:
        print("note: the next `pass-start` is granted — an aborted pass never counted as "
              "recorded, so it does not stand in the way of the next one", file=sys.stderr)
    else:
        key, payload = blocking[0]
        msg, _ = pass_start_refusal(state, key, payload)
        print(f"note: the next `pass-start` will refuse — {msg}", file=sys.stderr)


def cmd_pass_record(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("aborted"):
        die("no open pass to record; `pass-start` opens one")
    if lp.get("result") is not None:
        die(f"pass {lp['n']} is already recorded; use `amend` to add output the reviewer supplied afterwards")
    # SKILL.md step 3: a review of a state the files have moved past is not recorded. The
    # script never asked, so `pass-record` filed a verdict about code that no longer exists
    # as the pass of the state it found — and left `fingerprint_current` stale behind it.
    # `scope --` cannot move it any more (it refuses while a pass is open); an edit, a
    # formatter or a checkout still can.
    state["fingerprint_current"] = fp_of(state)
    if state["fingerprint_current"] != lp["fingerprint"]:
        save(state)
        die(f"the scoped files moved while pass {lp['n']} was being reviewed "
            f"({lp['fingerprint']} -> {state['fingerprint_current']}); that review is of a state "
            "that no longer exists — `pass-abort --reason \"scope moved mid-review\"` discards it, "
            "and a fresh reviewer reviews what is there now")
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
    print(pass_summary(lp["n"], lp["result"]).replace(f"pass {lp['n']}:", f"pass {lp['n']} recorded:", 1))
    calibration_note(lp["result"])


def require_recorded_pass(state, action, missing):
    """The last recorded pass, refused while a newer pass is open.

    `amend` and `resolve` both mutate that pass, and with a newer one open the pass they
    reach is the one it supersedes: the edit lands on a record no gate consults any more,
    while the output or the fix batch in the operator's hands belongs to the open review.
    `amend` learned to refuse; `resolve` did not, and silently moved `resolved` and the
    resolution log onto the dead record — then advised opening a new pass, which was already
    open. One guard, so the next command that edits a recorded pass cannot forget.
    """
    refuse_if_open(state, action, "the last recorded pass, which that one supersedes")
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
    # A raised total is reconciled with what the log already claimed — the clamp in
    # `resolve` sends the operator here for exactly that.
    r["resolved"] = resolution(r)["resolved"]
    r.setdefault("amendments", []).append({**given, "at": now()})
    state["fingerprint_current"] = fp_of(state)
    save(state)
    print(f"pass {lp['n']} amended: " + ", ".join(f"{k}={v}" for k, v in given.items()))
    print(pass_summary(lp["n"], r))
    if "score" in given or "findings" in given:
        calibration_note(r)
    if state["fingerprint_current"] != lp["fingerprint"]:
        print("note: scoped changes moved since this pass — the review is stale; validate and open a new pass")


def report_only(state):
    """True for the loop of a `--report-only` area, where the review ends at step 3.

    `project next` stamped the mode into `state.json` and nothing read it, so the loop's own
    commands behaved as fix mode: `status` judged the area by `blockers()` and announced
    "ready to accept", and `accept` and `resolve` ran — the steps references/project-mode.md
    says not to take there, `accept` because it "only invites a contradictory report".
    """
    return state.get("mode") == "project" and bool(state.get("report_only"))


REPORT_ONLY_EXIT = ("this is a --report-only area: the review ends once the pass is recorded "
                    "and its findings file written — `project close` records it, `reviewed` "
                    "or `incomplete` with its blockers; `amend` completes the reviewer's output")


def cmd_resolve(a):
    os.chdir(repo_root())
    state = load()
    if report_only(state):
        # Nothing is fixed in report-only, so nothing is resolved: findings are the report.
        die(f"`resolve` records a fix or a dismissal, and {REPORT_ONLY_EXIT}")
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
    if "resolution_log" not in r and r.get("resolved"):
        r["resolution_log"] = [{"unattributed": r["resolved"], "at": now()}]
    state["fingerprint_current"] = fp_of(state)
    # Stamped with the state the fixes were made on. `resolved` was the one counter bound to
    # nothing, so reverting a fix batch (`git checkout --`, `stash`) put the scope back on the
    # reviewed fingerprint with the findings still counted resolved — and `accept` signed off
    # code the reviewer had called defective.
    r.setdefault("resolution_log", []).append(
        {"fixed": a.fixed, "withdrawn": a.withdrawn, "adjudicated_invalid": a.adjudicated_invalid,
         "at": now(), "fingerprint": state["fingerprint_current"]})
    res = resolution(r)
    r["resolved"] = res["resolved"]   # kept for readers of state.json; the log decides
    claimed = res["claimed"]
    save(state)
    print(f"pass {lp['n']}: {r['resolved']}/{r['findings']} resolved")
    if claimed > r["findings"]:
        print(f"note: {claimed} resolutions claimed against {r['findings']} finding(s) — clamped; "
              "if the reviewer named more findings than were recorded, `amend --findings` first")
    if state["fingerprint_current"] != lp["fingerprint"]:
        print("scoped changes moved: validate, then open a new pass with a fresh reviewer")
    elif a.fixed and res["needs_fix"]:
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
    if report_only(state):
        die(f"`accept` is the fix-mode gate, and {REPORT_ONLY_EXIT}")
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
    # The questions the area's own close will ask: in report-only that is review_integrity,
    # never blockers() — inherited red, open findings and an `inadequate` verdict are the
    # report there, not obstacles to an `accept` that is not the gate.
    state["blockers"] = review_integrity(state, "area") if report_only(state) else blockers(state)
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
            # A field the CLI accepts and the state keeps must reach the text output too:
            # `status --json` is for machines, and this is the rendering an agent reads and
            # transcribes. `--note` was recordable and invisible, which makes it a place to
            # put something that then silently does not exist.
            for line in (r.get("note") or "").splitlines():
                print(f"      note: {line}")
        elif p.get("aborted"):
            print(f"  #{p['n']}  (aborted: {p.get('abort_reason', 'unspecified')})")
        else:
            print(f"  #{p['n']}  (open)")
    print_validation(current_validation(state), state=state)
    ready = ("none — ready for `project close` (with this reviewer's output in its findings file)"
             if report_only(state) else "none — ready to accept")
    print("blockers: " + (ready if not state["blockers"] else "; ".join(state["blockers"])))


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


def is_odd_artifact(path):
    """A real directory where this script keeps a file: never removed, always named."""
    return os.path.isdir(path) and not os.path.islink(path)


def remove_artifact(path):
    """Remove one of this script's files; True if something was removed.

    `os.path.exists` follows links and calls a directory present, so `reset` crashed with a
    traceback on the two shapes it can actually meet: a dangling symlink (exists() False, the
    link stays) and a directory at the name (os.remove raises IsADirectoryError). A link is
    unlinked, never followed; a directory is not this script's to delete and is reported.
    """
    if not os.path.lexists(path):
        return False
    if is_odd_artifact(path):
        return False                                     # named in the closing note
    try:
        os.remove(path)
    except OSError as e:
        print(f"note: could not remove {path} ({e.strerror})", file=sys.stderr)
        return False
    return True


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
        if remove_artifact(PROJECT_FILE):
            print("project ledger cleared")
        elif not is_odd_artifact(PROJECT_FILE) and not os.path.lexists(PROJECT_FILE):
            print("no project ledger to clear")
    # A `.tmp` can survive a crash between write and replace; it is debris, never the file
    # itself, so both are cleared whatever `--project` says. Only `project.json` is a record
    # worth keeping, and it is removed above when the operator asked for it.
    for f in (f for f in own_artifacts() if f.endswith(".tmp") or f == STATE_FILE):
        remove_artifact(f)
    # Dropping the loop state of an in-progress area strands the queue: `project next` and
    # a plain `project close` both refuse afterwards, and only `project close --force` can
    # settle it. Say so here, while the operator is still looking, instead of after two
    # failed commands.
    # A ledger only when it is one: a directory at the name is no ledger, and after
    # `--project` failed to remove the file, advising `reset --project` sends the operator
    # back to the command that just could not do it. Both are named in the closing note.
    if os.path.isfile(PROJECT_FILE) and not a.project:
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
        # Whatever keeps it, say so: SKILL.md has the agent name `.loop-review/` in the
        # report, and a silent survivor — the findings reports kept on purpose read as "ours",
        # so nothing was printed — is one it does not know to mention.
        if os.path.isdir(STATE_DIR):
            left = sorted(os.listdir(STATE_DIR))
            ours = {os.path.basename(f) for f in own_artifacts()}
            why = []
            if os.path.basename(FINDINGS_DIR) in left:
                why.append("findings/ holds the per-area review reports, kept on purpose")
            if os.path.isfile(PROJECT_FILE) and not a.project:
                why.append("project.json is the ledger (`reset --project` discards it)")
            elif os.path.isfile(PROJECT_FILE):
                why.append("project.json could not be removed (see the note above)")
            if any(x not in ours for x in left):
                why.append("it holds files this script did not create")
            # Every name `remove_artifact` left standing because it is a directory, not only
            # `state.json`: the one it skipped in the case that reached review was `.tmp`.
            odd = [f for f in own_artifacts() if f != FINDINGS_DIR and is_odd_artifact(f)]
            if odd:
                why.append(", ".join(odd) + (" is a directory" if len(odd) == 1 else
                                             " are directories")
                           + " where this script keeps a file — not its to delete, remove by hand")
            print(f"note: {STATE_DIR}/ kept — " + "; ".join(why or ["it is not empty"])
                  + ". Mention it in the report; delete it once nothing in it is needed")
    print("state cleared")


# ---------- project mode ----------

def load_project():
    if not os.path.exists(PROJECT_FILE):
        die("no project review; run `project init` first")
    try:
        return read_json(PROJECT_FILE)
    except ValueError as e:
        die(f"{PROJECT_FILE} is not valid JSON ({e})")


def open_loop_way_out(p):
    """What frees an open `state.json` before the queue can move, or None if there is none.

    `project init` and `project next` both refused on the file's mere existence and sent
    the operator to `project close` — which is refused itself unless an area is in progress
    and the loop is that area's: a loop left by `init` between areas, or a foreign one that
    `project close --force` deliberately leaves behind, met `no area in progress` on the very
    next command. The way out depends on whose loop it is, so it is decided here, once.
    `p` is the ledger, or None when there is none.
    """
    if not os.path.exists(STATE_FILE):
        return None
    try:
        loop = read_json(STATE_FILE)
    except (OSError, ValueError):
        loop = {}
    cur = next((x for x in (p or {}).get("areas", []) if x["status"] == "in_progress"), None)
    if cur is not None and loop_is_area(loop, cur):
        return (f"area `{area_name(cur)}` is mid-loop — `project close` records its outcome "
                "and frees the loop first")
    if cur is not None:
        return (f"the open loop (state.json) is not area `{area_name(cur)}`'s — `project close "
                "--force` settles that area `incomplete` and leaves the loop alone; `reset` "
                "then frees it")
    return (f"a loop outside the project queue is open (state.json, scope "
            f"{loop.get('scope')}) — `reset` frees it once it is finished or the user agrees "
            "it is dead; no area owns it, so `project close` has nothing to record")


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
    area's row without so much as a `--force`. No stamp, no match: the containment fallback
    kept for loops older than the stamp also matched any `init --mode project` whose scope
    lists the area's paths among others — a loop `project next` never opened. An unstamped loop is foreign; `project close --force`
    settles the area `incomplete`, which never lets an unreviewed area pass.
    """
    if state.get("mode") != "project":
        return False
    opened_for = state.get("area")
    return opened_for is not None and list(opened_for) == list(area_paths(area))


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
    return stem_within(stem, 80) + "-" + tail


def stem_within(stem, budget):
    """`stem` cut to at most `budget` *bytes* on disk, never inside a character.

    The limit is the filesystem's, and it counts bytes: 80 characters of CJK or emoji are up
    to 320, so the old character slice produced a name `open()` refused with ENAMETOOLONG
    once `-<hash>.prev.md` was added. Cut the encoded form (fsencode, so a non-UTF-8 name's
    raw bytes count as themselves), back off any UTF-8 continuation bytes so a multi-byte
    character is dropped whole rather than split, and decode it the same way. A name of
    ASCII is cut exactly where it always was, so existing findings files keep their names.
    """
    raw = os.fsencode(stem)
    if len(raw) <= budget:
        return stem
    cut = budget
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return os.fsdecode(raw[:cut])


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


# Every metric a closed ledger row carries, in one place. The row was spelled out by hand in
# `project close` and in `settle_area`, and both dropped `test_score` and `note`: the area
# prompt asks for a test quality score, SKILL.md's report quotes it from `project status
# --json`, and `project close` deleted the only copy with `state.json`.
LEDGER_METRICS = ("passes", "findings", "resolved", "total_findings", "total_resolved", "score",
                  "test_score", "test_evidence", "understood", "note", "findings_file")


def pass_summary(n, r):
    """The one-line state of a pass result, as `pass-record` and `amend` both print it."""
    return (f"pass {n}: score {metric(r.get('score'))}, test score "
            f"{metric(r.get('test_score'))}, {r.get('resolved', 0)}/{r['findings']} resolved, "
            f"understood={bool(r.get('understood'))}, test evidence "
            f"{metric(r.get('test_evidence'), 'not assessed')}"
            + (f", note: {r['note']}" if r.get("note") else ""))


def calibration_note(r):
    """Asked of the result as it stands, so `amend --score` meets it as `pass-record` did."""
    if r["findings"] == 0 and r.get("score") is not None and r["score"] < 9.5:
        print("note: score below 9.5 with zero findings — treat the number as calibration "
              "noise; findings control the loop")


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
                 **{k: None for k in LEDGER_METRICS}, "passes": passes})


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
    # `--force` rebuilds the ledger; an open loop is still whatever it was under the old one.
    try:
        prior = read_json(PROJECT_FILE) if os.path.exists(PROJECT_FILE) else None
    except (OSError, ValueError):
        prior = None
    blocked = open_loop_way_out(prior)
    if blocked:
        die(blocked)
    # The same question with the loop state already gone: `open_loop_way_out` only sees a
    # `state.json` that exists, and with it lost `--force` silently replaced a ledger whose
    # finished areas are the review's whole output — the one thing evals 14 and 15 say the
    # agent must not do. The documented recovery keeps them; starting over stays possible,
    # but as two explicit commands, not as a side effect of re-queueing.
    stuck = next((x for x in (prior or {}).get("areas", []) if x.get("status") == "in_progress"), None)
    if stuck is not None:
        die(f"area `{area_name(stuck)}` is in progress and its loop state is gone — "
            "`project close --force` settles it `incomplete`, keeps every outcome already "
            "recorded, and the queue continues with `project next`. To discard the review "
            "instead: `reset --project`, then `project init`.")
    done = [x for x in (prior or {}).get("areas", []) if x.get("status") not in ("pending", "in_progress")]
    # Every area is checked here, before any of them is reviewed: a mistyped or renamed
    # path matches no file, and an area nobody can read still passes every gate and closes
    # as `accepted`. Checking at queue time reports all the bad paths at once, while the
    # operator is still choosing them, instead of one per `project next`.
    # Per path, not per area: `scope_is_empty` asks whether the area as a whole has files, so
    # a glued area kept a mistyped path alive behind a healthy neighbour — `pkg + non_exitent`
    # queued, opened and reached the reviewer as one of the paths it was told to read whole.
    # Task mode has warned about this since `init`; project mode never did.
    warn_unseen([q for x in areas for q in x], "project")
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
    if done:
        # `--force` is the human's "start over"; say what it cost, once it has happened.
        print(f"note: --force discarded {len(done)} recorded area outcome(s) from the previous "
              f"ledger; their findings files stay in {FINDINGS_DIR}/", file=sys.stderr)
    print(f"project review queued: {len(areas)} area(s), {a.max_passes} passes each, "
          + ("report-only (no fixes)" if a.report_only else "fix mode"))
    for x in areas:
        print(f"  - {' + '.join(x)}")


def set_aside_findings(ffile):
    """Move an earlier review's findings file out of the live path, overwriting nothing.

    A previous review's file is moved aside rather than compared against. Comparison was
    wrong in both directions: a digest match refused a legitimately identical repeat review
    for ever, and an mtime fallback accepted a stale file that had merely been touched.
    Setting it aside makes the gate a plain existence check again, and keeps the old text.

    The rename went to `<slug>.prev.md` whatever was there, so a third round of an area —
    or a round re-queued after a crash left a half-written file — replaced the report kept
    from the first. Every report the script set aside stays: the first at `.prev.md`, later
    ones at the next free `<slug>.N.prev.md`. Returns where the file went, or None.
    """
    if not os.path.exists(ffile):
        return None
    dest, n = ffile[:-3] + ".prev.md", 2
    while os.path.lexists(dest):
        dest, n = f"{ffile[:-3]}.{n}.prev.md", n + 1
    os.replace(ffile, dest)
    return dest


def cmd_project_next(a):
    os.chdir(repo_root())
    p = load_project()
    blocked = open_loop_way_out(p)
    if blocked:
        die(blocked)
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
        settle_area(nxt, ["area matches no file — path renamed, deleted (on disk or in git), "
                          "mistyped, or unreadable"], passes=0)
        save_project(p)
        left = sum(1 for x in p["areas"] if x["status"] == "pending")
        print(f"area `{area_name(nxt)}` matches no file — recorded incomplete; "
              f"{left} area(s) still pending. Run `project next` again for the next one.",
              file=sys.stderr)
        sys.exit(NO_CHANGES)
    # Only now, with a loop about to open: an area settled empty above was not reviewed this
    # round, so its last report stays where it was rather than being renamed for a review
    # that never happened.
    set_aside_findings(os.path.join(FINDINGS_DIR, area_slug(nxt) + ".md"))
    state = {
        "created": now(), "mode": "project", "base": None,
        # The queued paths, kept as the loop's identity even if `scope --` widens `scope`.
        "area": area_paths(nxt), "scope": area_paths(nxt),
        "report_only": p["report_only"], "allow_empty": bool(p.get("allow_empty")),
        "max_passes": p["max_passes"], "passes": [], "validation": [],
    }
    state["fingerprint_current"] = fp_of(state)
    save(state)
    # Again here, and not only at queue time: areas are queued once and opened one by one,
    # so a path can be renamed or deleted in between — and this is the moment the agent
    # writes the area's paths into the reviewer's prompt.
    warn_unseen(area_paths(nxt), "project")
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


def findings_problem(ffile):
    """Why the findings file cannot stand in for the review, or None when it can.

    `project next` moves any previous review's file to `<slug>.prev.md`, so anything at this
    path was written during this area's current loop. The path repeats on every review of an
    area, and without that move a leftover file satisfied the gate while this run's findings
    died with `state.json`. Do not reach for a timestamp instead: `now()` has one-second
    resolution and a whole area can open and close inside that second, so the comparison
    passes exactly when the runs are back to back.

    Existence was the whole check, and `project close` deletes `state.json` right after it:
    a `touch`, an interrupted `> file` redirect or a directory at that path passed, and the
    area closed `reviewed` or `accepted` with no review text anywhere. The brief has been
    held to "non-empty" since `--task-brief-file`; the one record that outlives the loop is
    held to the same.
    """
    if not os.path.lexists(ffile):
        return "is missing"
    if not os.path.isfile(ffile):
        return "is not a regular file"
    try:
        with open(ffile, "rb") as f:
            body = f.read()
    except OSError as e:
        return f"cannot be read ({e.strerror})"
    if not body.strip():
        return "is empty"
    return None


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
    # One rule for both modes: the findings file is required exactly when a recorded review
    # the reviewer *understood* exists, because closing deletes the only other copy of it.
    # Its findings are real even if the area has since moved, its files are gone, or a later
    # pass is open — it closes `incomplete` then, but its text must survive. A pass without
    # understanding was never a review, and an area with no recorded pass has nothing to
    # keep; refusing those would only stall the queue. The two modes asked this differently:
    # report-only skipped the file whenever any blocker stood, so a real review over an area
    # that had since moved was deleted with `findings_file: null`, and fix mode demanded a
    # file for a review that had shown no understanding at all.
    missing = findings_problem(ffile) if r and r.get("understood") else None
    if missing and not a.force:
        die(f"cannot close area: {ffile} {missing} — write this reviewer's whole output "
            "there; closing deletes the loop state, so nothing else survives (--force closes "
            "it as incomplete instead, losing the review)")
    lost = [f"{ffile} {missing} — the review did not survive the close"] if missing else []
    if p["report_only"]:
        # Two kinds of problem, and conflating them is what stalled the queue. A missing
        # findings file is fixable right now — refuse, so the review is written down before
        # the state that holds it is deleted. An area that never produced a usable pass is
        # not fixable by refusing: it is recorded `incomplete` with its blockers and the
        # queue moves on, exactly as fix mode settles a failed area without a hatch.
        # The same conditions `blockers()` reads, phrased for an area. Report-only does not
        # require the loop's own acceptance — nothing here is meant to be fixed — but a
        # review with no pass, no understanding, or one describing code that has since moved
        # is not a review in either mode, and keeping the list in one place is what stops the
        # two from drifting apart again.
        # Required whatever the finding count: for a clean area the file is the *entire*
        # record — the understanding summary, the test-evidence basis, the score rationale.
        problems = review_integrity(state, "area") + lost
        cur["status"] = "reviewed" if not problems else "incomplete"
        cur["blockers"] = problems
    else:
        # Forcing past a missing file cannot leave the area `accepted`: the record the row
        # would rest on is exactly what was never written, and `--force` never lets an area
        # pass — in either mode it settles the area with the reason attached.
        reasons = blockers(state) + lost
        cur["status"] = "accepted" if not reasons else "incomplete"
        cur["blockers"] = reasons
    cur["closed"] = now()
    # The loop's scope, not the queued paths: `scope --` may have widened the area when a fix
    # landed next door, and the ledger is the deliverable — it must say what was reviewed.
    if state["scope"] != area_paths(cur):
        cur["reviewed_paths"] = state["scope"]
    cur["passes"] = len(state["passes"])
    # Two questions, two fields — the single summed pair answered neither. `findings` /
    # `resolved` are the last recorded pass's: what acceptance was judged on, so an area
    # `accepted` after a clean second pass reads 0/0, not "2 findings, 0 resolved". The
    # totals sum every pass, which is the area's history: a finding a later reviewer named
    # again counts once per pass that named it (findings are counts, not records).
    recorded = [x["result"] for x in state["passes"] if x.get("result")]
    cur["findings"] = r["findings"] if r else None
    cur["resolved"] = r["resolved"] if r else None
    cur["total_findings"] = sum(x["findings"] for x in recorded) if recorded else None
    cur["total_resolved"] = sum(x["resolved"] for x in recorded) if recorded else None
    # The last recorded pass's verdicts and metrics would otherwise die with state.json: in
    # report-only mode `blockers()` is never consulted, so the ledger is the only place they
    # can be reported. Read off the shared list, so a field cannot be left behind again.
    for k in ("score", "test_score", "test_evidence", "understood", "note"):
        cur[k] = r.get(k) if r else None
    cur["findings_file"] = ffile if findings_problem(ffile) is None else None
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
        # A row that recorded a review — closed with a findings count — owes the report both
        # fields SKILL.md names per area, verdict and findings-file path, even when absent.
        # Printing the verdict only when set hid exactly the rows where it was missing, and
        # the path was never printed. A row with no review has neither, and says nothing.
        reviewed = x["status"] not in ("pending", "in_progress") and x.get("findings") is not None
        if x.get("test_evidence") or reviewed:
            line += f"  test-evidence {metric(x.get('test_evidence'), 'not assessed')}"
        # Each metric prints only if the area has one. They are not filled as a block: an
        # area closed before any pass — an empty or renamed one, settled `incomplete` by
        # `project next` — has `passes: 0` and nothing else, and printing the group on the
        # strength of the pass count alone rendered it as `findings None/None score None`.
        if x.get("passes") is not None:
            line += f"  passes {x['passes']}"
        if x.get("findings") is not None:
            resolved = x.get("resolved")
            line += f"  findings {metric(resolved, '?')}/{x['findings']}"
            # The history, only where it says something the last pass does not.
            if (x.get("total_findings") is not None
                    and (x["total_findings"], x.get("total_resolved")) != (x["findings"], resolved)):
                line += (f" (over all passes "
                         f"{metric(x.get('total_resolved'), '?')}/{x['total_findings']})")
        if x.get("score") is not None:
            line += f"  score {x['score']}"
        if x.get("test_score") is not None:
            line += f"  test-score {x['test_score']}"
        if x.get("findings_file") or reviewed:
            line += f"  findings-file {metric(x.get('findings_file'), 'not written')}"
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
