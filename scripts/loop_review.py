#!/usr/bin/env python3
"""Loop-review state machine.

Keeps the loop honest: tracks scope, validation, passes and findings in
.loop-review/state.json inside the repo, and refuses to accept until the
exit condition holds. No dependencies beyond git and Python 3.8+.

Commands:
  init [--max-passes N] -- <paths...>      start a loop over task-owned paths
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

STATE_DIR = ".loop-review"
STATE_FILE = os.path.join(STATE_DIR, "state.json")


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
    """argparse type: the 1–10 progress signal. Range only — the score gates nothing."""
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number")
    if not 1.0 <= f <= 10.0:
        raise argparse.ArgumentTypeError(f"must be within the 1–10 anchors, got {f}")
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


def fingerprint(paths):
    """Hash of tracked diff (staged+unstaged) plus untracked file contents, scoped to paths."""
    h = hashlib.sha256()

    def field(b):
        """Feed one length-prefixed field, so the boundary between fields is unambiguous.

        Concatenating a path with its contents lets distinct states hash alike: file `a`
        holding "bc" and file `ab` holding "c" both produce b"abc". Two different states
        with one fingerprint is the gate-breaking lie — `pass-start` would call the change
        unchanged, and a stale review would still count as current.
        """
        h.update(str(len(b)).encode() + b"\0" + b)

    # check=True on both git calls: a failure here must be loud. A silently empty hash is
    # indistinguishable from "nothing changed", which is the one lie that breaks every gate.
    field(git("diff", diff_base(), "--", *paths))
    # -z: NUL-separated and unquoted, so paths with spaces, newlines or non-ASCII
    # characters survive intact. Splitting plain `ls-files` output would corrupt them
    # and silently drop those files from the hash.
    out = git("ls-files", "-z", "--others", "--exclude-standard", "--", *paths)
    # Kept as bytes: a filename need not be decodable, and open() takes bytes paths.
    untracked = [p for p in out.split(b"\0") if p]
    for p in sorted(untracked):
        field(p)
        try:
            with open(p, "rb") as f:
                field(f.read())
        except OSError as e:
            # Fold the failure in: an unreadable file must not hash like an empty one.
            field(f"<unreadable:{e.errno}>".encode())
    return h.hexdigest()[:16]


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
    state = {
        "created": now(),
        "scope": scope,
        "max_passes": a.max_passes,
        "passes": [],
        "validation": [],
        "fingerprint_current": fingerprint(scope),
    }
    save(state)
    print(f"loop initialised: {len(scope)} path(s), max {a.max_passes} passes, fingerprint {state['fingerprint_current']}")
    for p in scope:
        print(f"  - {p}")


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
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    print_validation(cur, "validation for this state")


def cmd_pass_record(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None:
        die("no open pass to record")
    if lp.get("result") is not None:
        die(f"pass {lp['n']} is already recorded; use `amend` to add output the reviewer supplied afterwards")
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
    if "findings" in given and given["findings"] < r["findings"]:
        die(f"amend cannot lower findings ({r['findings']} -> {given['findings']}); "
            "record a fix, withdrawal or adjudication with `resolve` instead")
    r.update(given)
    r.setdefault("amendments", []).append({**given, "at": now()})
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    # Counts are non-negative by argument type; a call that reports nothing is still a
    # no-op that would land in the log as a resolution step.
    total = a.fixed + a.withdrawn + a.adjudicated_invalid
    if total == 0:
        die("resolve needs at least one of --fixed / --withdrawn / --adjudicated-invalid")
    r = lp["result"]
    claimed = r["resolved"] + total
    r["resolved"] = min(r["findings"], claimed)
    r.setdefault("resolution_log", []).append({"fixed": a.fixed, "withdrawn": a.withdrawn, "adjudicated_invalid": a.adjudicated_invalid, "at": now()})
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    state["fingerprint_current"] = fingerprint(state["scope"])
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
    state["fingerprint_current"] = fingerprint(state["scope"])
    state["blockers"] = blockers(state)
    if a.json:
        print(json.dumps(state, indent=2))
        return
    print(f"scope ({len(state['scope'])}): " + ", ".join(state["scope"]))
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
        if os.path.isdir(STATE_DIR):
            print(f"note: {STATE_DIR}/ kept — it holds files this script did not create")
    print("state cleared")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(prog="loop_review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--max-passes", type=positive_int, default=5); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("validate"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("validate-drop"); s.add_argument("--force", action="store_true"); s.add_argument("--reason"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate_drop)
    s = sub.add_parser("pass-start"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_pass_start)
    s = sub.add_parser("pass-record"); s.add_argument("--score", type=score_value, required=True); s.add_argument("--findings", type=nonneg_int, required=True); s.add_argument("--test-evidence", choices=TEST_EVIDENCE, required=True); s.add_argument("--understood", action="store_true"); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_pass_record)
    s = sub.add_parser("amend"); s.add_argument("--understood", action="store_true"); s.add_argument("--test-evidence", choices=TEST_EVIDENCE); s.add_argument("--score", type=score_value); s.add_argument("--findings", type=nonneg_int); s.add_argument("--test-score", type=score_value); s.add_argument("--note"); s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("resolve"); s.add_argument("--fixed", type=nonneg_int, default=0); s.add_argument("--withdrawn", type=nonneg_int, default=0); s.add_argument("--adjudicated-invalid", type=nonneg_int, default=0); s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("accept"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fingerprint"); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser("reset"); s.set_defaults(fn=cmd_reset)

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
