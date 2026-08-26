#!/usr/bin/env python3
"""Loop-review state machine.

Keeps the loop honest: tracks scope, validation, passes and findings in
.loop-review/state.json inside the repo, and refuses to accept until the
exit condition holds. No dependencies beyond git and Python 3.8+.

Commands:
  init [--max-passes N] -- <paths...>      start a loop over task-owned paths
  validate -- <command...>                 run a validation command, record result
  pass-start                               open a full-review pass (checks gates)
  pass-record --score S --findings N [--understood] [--test-score T] [--note TEXT]
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

STATE_DIR = ".loop-review"
STATE_FILE = os.path.join(STATE_DIR, "state.json")


# ---------- helpers ----------

def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def repo_root():
    return git("rev-parse", "--show-toplevel").strip()


def die(msg, code=1):
    print(f"loop-review: {msg}", file=sys.stderr)
    sys.exit(code)


def load():
    if not os.path.exists(STATE_FILE):
        die("no active loop; run `init` first")
    with open(STATE_FILE) as f:
        return json.load(f)


def save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fingerprint(paths):
    """Hash of tracked diff (staged+unstaged) plus untracked file contents, scoped to paths."""
    h = hashlib.sha256()
    h.update(git("diff", "HEAD", "--", *paths, check=False).encode())
    # -z: NUL-separated and unquoted, so paths with spaces, newlines or non-ASCII
    # characters survive intact. Splitting plain `ls-files` output would corrupt them
    # and silently drop those files from the hash.
    out = git("ls-files", "-z", "--others", "--exclude-standard", "--", *paths, check=False)
    untracked = [p for p in out.split("\0") if p]
    for p in sorted(untracked):
        h.update(p.encode())
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError as e:
            # Fold the failure in: an unreadable file must not hash like an empty one.
            h.update(f"<unreadable:{e.errno}>".encode())
    return h.hexdigest()[:16]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def last_pass(state):
    return state["passes"][-1] if state["passes"] else None


def current_validation(state):
    """Validation runs for the current fingerprint, latest result per command.

    Re-running the same command on unchanged files supersedes its earlier result, so a
    flaky or environment failure can be cleared by re-running it — not only by editing.
    Distinct commands are tracked separately: a green re-run of the tests does not
    absolve a red lint.
    """
    latest = {}
    for v in state["validation"]:
        if v["fingerprint"] == state["fingerprint_current"]:
            latest[v["cmd"]] = v
    return list(latest.values())


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
    return reasons


# ---------- commands ----------

def cmd_init(a):
    if not a.paths:
        die("init needs at least one task-owned path after `--`")
    os.chdir(repo_root())
    if os.path.exists(STATE_FILE) and not a.force:
        die("loop already active; use `reset` or `--force`")
    state = {
        "created": now(),
        "scope": a.paths,
        "max_passes": a.max_passes,
        "passes": [],
        "validation": [],
        "fingerprint_current": fingerprint(a.paths),
    }
    save(state)
    print(f"loop initialised: {len(a.paths)} path(s), max {a.max_passes} passes, fingerprint {state['fingerprint_current']}")


def cmd_validate(a):
    os.chdir(repo_root())
    state = load()
    if not a.command:
        die("validate needs a command after `--`")
    cmd = " ".join(a.command) if len(a.command) == 1 else shlex.join(a.command)
    print(f"$ {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True)
    state["fingerprint_current"] = fingerprint(state["scope"])
    state["validation"].append({"cmd": cmd, "exit": r.returncode, "at": now(), "fingerprint": state["fingerprint_current"]})
    save(state)
    print(f"loop-review: validation {'GREEN' if r.returncode == 0 else 'RED'} (exit {r.returncode})")
    sys.exit(r.returncode)


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
        die("validation is red for the current state; fix it (or re-run the command if the failure was environmental) before requesting a review")
    state["passes"].append({"n": n, "opened": now(), "fingerprint": state["fingerprint_current"], "result": None})
    save(state)
    print(f"pass {n}/{state['max_passes']} opened on fingerprint {state['fingerprint_current']}")
    print("scope:")
    for p in state["scope"]:
        print(f"  - {p}")
    print("validation for this state:")
    for v in cur:
        print(f"  - {v['cmd']}  -> exit {v['exit']}")


def cmd_pass_record(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("result") is not None:
        die("no open pass to record")
    lp["result"] = {
        "score": a.score,
        "findings": a.findings,
        "resolved": 0,
        "understood": bool(a.understood),
        "test_score": a.test_score,
        "note": a.note,
        "recorded": now(),
    }
    save(state)
    print(f"pass {lp['n']} recorded: score {a.score}, {a.findings} finding(s), understood={bool(a.understood)}")
    if a.findings == 0 and a.score < 9.5:
        print("note: score below 9.5 with zero findings — treat the number as calibration noise; findings control the loop")


def cmd_resolve(a):
    os.chdir(repo_root())
    state = load()
    lp = last_pass(state)
    if lp is None or lp.get("result") is None:
        die("no recorded pass to resolve against")
    total = a.fixed + a.withdrawn + a.adjudicated_invalid
    r = lp["result"]
    r["resolved"] = min(r["findings"], r["resolved"] + total)
    r.setdefault("resolution_log", []).append({"fixed": a.fixed, "withdrawn": a.withdrawn, "adjudicated_invalid": a.adjudicated_invalid, "at": now()})
    state["fingerprint_current"] = fingerprint(state["scope"])
    save(state)
    print(f"pass {lp['n']}: {r['resolved']}/{r['findings']} resolved")
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
            print(f"  #{p['n']}  score {r['score']}  findings {r['resolved']}/{r['findings']} resolved  understood={r['understood']}  test={r['test_score']}")
        else:
            print(f"  #{p['n']}  (open)")
    cur = current_validation(state)
    print(f"validation on current state: {len(cur)} command(s), " + ("GREEN" if cur and all(v['exit'] == 0 for v in cur) else "RED/none"))
    print("blockers: " + ("none — ready to accept" if not state["blockers"] else "; ".join(state["blockers"])))


def cmd_fingerprint(a):
    os.chdir(repo_root())
    print(fingerprint(load()["scope"]))


def cmd_reset(a):
    os.chdir(repo_root())
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    print("state cleared")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(prog="loop_review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--max-passes", type=int, default=5); s.add_argument("--force", action="store_true"); s.add_argument("paths", nargs="*"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("validate"); s.add_argument("command", nargs="*"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("pass-start"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_pass_start)
    s = sub.add_parser("pass-record"); s.add_argument("--score", type=float, required=True); s.add_argument("--findings", type=int, required=True); s.add_argument("--understood", action="store_true"); s.add_argument("--test-score", type=float); s.add_argument("--note"); s.set_defaults(fn=cmd_pass_record)
    s = sub.add_parser("resolve"); s.add_argument("--fixed", type=int, default=0); s.add_argument("--withdrawn", type=int, default=0); s.add_argument("--adjudicated-invalid", type=int, default=0); s.set_defaults(fn=cmd_resolve)
    s = sub.add_parser("accept"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("fingerprint"); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser("reset"); s.set_defaults(fn=cmd_reset)

    # allow "--" separated trailing args for init/validate
    argv = sys.argv[1:]
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
