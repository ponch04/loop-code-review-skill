#!/usr/bin/env python3
"""Regression check for loop_review.py: assert the refusals, not the happy path.

Run: python3 scripts/selftest.py    (exit 0 = green)

This file exists because the happy path never caught anything. Three fixed defects — the
unframed fingerprint, the non-atomic state write and the missing argument validation — were
lost again by a file copy, and a smoke that only walks init->validate->pass-start->accept
stayed green through all three. The script's whole purpose is to say no, so what has to be
tested is every no.

Same constraints as the tool it checks: stdlib and git only, no framework, no CI, one file.
Each case builds a throwaway repository under a temp dir and asserts observable behaviour —
exit codes, fingerprints, ledger contents — never internals.
"""
import argparse
import ast
import contextlib
import importlib.util
import io as _io
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile

LOOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loop_review.py")
FAILURES = []
CASES = []


def module():
    """Import loop_review.py as a module, for contracts no CLI call can express."""
    spec = importlib.util.spec_from_file_location("loop_review", LOOP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def case(fn):
    CASES.append(fn)
    return fn


def run(*args, cwd, check=False):
    r = subprocess.run([sys.executable, LOOP, *args], cwd=cwd,
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args)} failed ({r.returncode}): {r.stderr.strip()}")
    return r


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


REPOS = []


def repo(files=None, commit=True):
    d = tempfile.mkdtemp(prefix="loop-selftest-")
    REPOS.append(d)
    sh("git init -q . && git config user.email t@t && git config user.name t", d)
    for name, body in (files or {"a.py": "v1\n"}).items():
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        with open(path, "w") as f:
            f.write(body)
    if commit:
        sh("git add -A && git commit -qm init", d)
    return d


def write(d, name, body):
    with open(os.path.join(d, name), "w") as f:
        f.write(body)


def findings(d, slug, text="A. review\nB. understood\nC. trusted\nD. 9.5\n"):
    """Write an area's findings file. `project close` requires it in both modes: closing
    deletes the loop state, so this file is the review's only surviving record.

    The stem carries an unconditional hash tail (two areas can flatten to the same name), so
    the caller names the readable part and this resolves the rest.
    """
    fdir = os.path.join(d, ".loop-review", "findings")
    os.makedirs(fdir, exist_ok=True)
    hit = [f for f in os.listdir(fdir)
           if f.startswith(slug + "-") and f.endswith(".md") and not f.endswith(".prev.md")]
    name = hit[0] if hit else None
    if name is None:                      # not yet created: derive it the way the script does
        import hashlib
        paths = slug.split("__")
        name = slug + "-" + hashlib.sha256("\0".join(paths).encode()).hexdigest()[:8] + ".md"
    with open(os.path.join(fdir, name), "w") as f:
        f.write(text)


def pass_through(d, score="9.5", findings="0", evidence="trusted", understood=True):
    """validate + pass-start + pass-record: the opening ritual of almost every case.

    Cases that are *about* one of these three call them directly; the rest only need a
    recorded pass to exist before they can test what they are actually about.
    """
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    args = ["pass-record", "--score", score, "--findings", findings,
            "--test-evidence", evidence]
    if understood:
        args.append("--understood")
    run(*args, cwd=d, check=True)


def ok(cond, label):
    if not cond:
        FAILURES.append(label)


def green(d, *cmds):
    """init-less helper: record the given validation commands, all expected green."""
    for c in cmds:
        run("validate", "--", c, cwd=d)


# --------------------------------------------------------------------------- scope gates

@case
def empty_changes_scope_is_refused():
    d = repo()
    r = run("init", "--", "a.py", cwd=d)
    ok(r.returncode == 2, "clean worktree: init must exit 2 (no-changes)")
    r = run("init", "--allow-empty", "--", "a.py", cwd=d)
    ok(r.returncode == 0, "--allow-empty must override the empty-scope refusal")


@case
def scoped_file_alone_is_not_empty():
    """The regression that made the empty-scope test fire on healthy scopes."""
    d = repo({"a.py": "v1\n", "b.py": "v1\n"})
    write(d, "a.py", "v1\nv2\n")
    r = run("init", "--", "a.py", cwd=d)
    ok(r.returncode == 0, "a scope that is the only change must not read as empty")
    d2 = repo({"a.py": "v1\n", "b.py": "v1\n"})
    write(d2, "b.py", "v1\nv2\n")
    r = run("init", "--", "a.py", cwd=d2)
    ok(r.returncode == 2, "an unchanged scope must be refused even when the repo is dirty")


@case
def empty_project_area_is_refused():
    """On the path the workflow uses (`project init`), not only via `init --mode project`."""
    d = repo({"pkg/a.py": "x\n"})
    r = run("project", "init", "--", "pkg", "does/not/exist", cwd=d)
    ok(r.returncode == 2, "project init must refuse an area matching no file")
    ok(not os.path.exists(os.path.join(d, ".loop-review", "project.json")),
       "a refused queue must not be written to the ledger")
    r = run("project", "init", "--allow-empty", "--", "pkg", "does/not/exist", cwd=d)
    ok(r.returncode == 0, "--allow-empty must still queue it")
    run("init", "--mode", "project", "--", "does/not/exist", cwd=d)
    ok(run("init", "--mode", "project", "--", "does/not/exist", cwd=d).returncode == 2,
       "init --mode project must refuse it too")


@case
def an_area_emptied_after_queueing_is_refused_at_open():
    d = repo({"pkg/a.py": "x\n", "gone/b.py": "y\n"})
    run("project", "init", "--", "pkg", "gone", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9", "0", "trusted")
    findings(d, "pkg")
    run("project", "close", cwd=d, check=True)
    sh("git rm -q -r gone && git commit -qm drop", d)
    r = run("project", "next", cwd=d)
    ok(r.returncode == 2, "an area that lost its files must not open a loop")
    st = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok(st["areas"][1]["status"] == "incomplete" and st["areas"][1]["blockers"],
       f"it must be recorded incomplete with its blocker, got {st['areas'][1]['status']}")


@case
def a_foreign_loop_never_traps_the_area_that_did_not_open_it():
    """`project close` and `reset` must not each defer to the other.

    When state.json belongs to some other loop, close cannot record an outcome from it and
    reset once refused to discard it "because an area is mid-loop" — so the queue could only
    be freed by deleting files by hand. Both halves are asserted by name: the refusal has to
    quote a form the parser accepts, `--force` has to settle the area, and the foreign loop
    has to survive it — it is another review's entire history.
    """
    d = repo({"a/x.py": "1\n", "b/y.py": "2\n"})
    run("project", "init", "--", "a", "b", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.load(open(sf))
    st["scope"] = ["b"]                                # the open loop is not this area's
    json.dump(st, open(sf, "w"), indent=2)

    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "not area" in r.stderr,
       f"close must refuse to sign another loop into this area, got rc={r.returncode}")
    ok("project close --force" in r.stderr and "reset --force" not in r.stderr,
       f"the refusal must name a route the CLI has: {r.stderr.strip()[:160]}")

    r = run("project", "close", "--force", cwd=d)
    ok(r.returncode == 0, f"--force must settle the area, got rc={r.returncode}: {r.stderr.strip()[:160]}")
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok(led["areas"][0]["status"] == "incomplete" and led["areas"][0]["blockers"],
       f"the unreviewed area must be recorded incomplete with its blocker: {led['areas'][0]}")
    ok(os.path.exists(sf), "the foreign loop must be left untouched — it is another review's history")

    r = run("reset", cwd=d)
    ok(r.returncode == 0 and not os.path.exists(sf),
       f"reset must free a loop no in-progress area owns, got rc={r.returncode}")
    r = run("project", "next", cwd=d)
    ok(r.returncode == 0 and "b" in r.stdout, f"the queue must continue: {r.stdout.strip()[:120]}")


@case
def reset_still_refuses_to_discard_the_loop_of_the_area_that_owns_it():
    """The other side of the same predicate: narrowing reset must not un-protect an outcome."""
    d = repo({"a/x.py": "1\n"})
    run("project", "init", "--", "a", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    r = run("reset", cwd=d)
    ok(r.returncode != 0 and "mid-loop" in r.stderr,
       f"reset must still refuse inside the area's own loop, got rc={r.returncode}")
    ok(os.path.exists(os.path.join(d, ".loop-review", "state.json")),
       "the area's own loop state must survive the refusal")


@case
def a_refused_area_does_not_block_the_areas_behind_it():
    """One renamed directory must not make every later area unreachable."""
    d = repo({"a/x.py": "1\n", "gone/y.py": "2\n", "c/z.py": "3\n"})
    run("project", "init", "--report-only", "--", "a", "gone", "c", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    findings(d, "a", "clean\n")
    run("project", "close", cwd=d, check=True)
    sh("git rm -q -r gone && git commit -qm drop", d)
    run("project", "next", cwd=d)                      # hits the vanished area
    r = run("project", "next", cwd=d)                  # must reach `c`, not the same one
    ok(r.returncode == 0 and "c" in r.stdout,
       f"the queue must advance past a refused area, got rc={r.returncode}: {r.stdout.strip()[:120]}")
    st = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok([x["status"] for x in st["areas"]] == ["reviewed", "incomplete", "in_progress"],
       f"ledger: {[x['status'] for x in st['areas']]}")


@case
def a_deeply_nested_area_is_fingerprinted_by_its_tracked_files():
    """SKILL.md's own example uses `apps/portal/src/lib`; shallow fixtures never reached it.

    A `:(exclude)` pathspec is truncated by the common prefix of the positive pathspecs, so
    for a deep area the exclusion matched everything and `ls-files --cached` returned
    nothing: the area hashed as empty, a fix never moved the fingerprint, and `accept`
    signed off a review of code that had since been rewritten.
    """
    d = repo({"apps/portal/src/lib/a.ts": "one\n",
              "apps/portal/src/lib-tests/a.test.ts": "t\n",
              "packages/domain/d.ts": "d\n"})
    r = run("project", "init", "--", "packages/domain",
            "apps/portal/src/lib", "+", "apps/portal/src/lib-tests", cwd=d)
    ok(r.returncode == 0, f"the documented multi-path example must queue, got: {r.stderr.strip()[:160]}")
    ffile = run("project", "next", cwd=d, check=True).stdout.split(
        "findings file: ")[1].splitlines()[0]              # packages/domain
    pass_through(d, "9.5", "0", "trusted")
    # This area's slug flattens `packages/domain`, which the helper cannot reconstruct from
    # the stem alone — take the path the script itself printed.
    with open(os.path.join(d, ffile), "w") as f:
        f.write("clean\n")
    run("project", "close", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    before = out.split("fingerprint ")[1].split(")")[0].strip()
    write(d, "apps/portal/src/lib/a.ts", "completely rewritten\n")
    after = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(before != after,
       f"rewriting a tracked file in a nested area must move the fingerprint ({before} == {after})")


@case
def project_next_creates_the_findings_directory():
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    ok(os.path.isdir(os.path.join(d, os.path.dirname(stem))),
       "the findings directory must exist before the agent is told to write into it")


@case
def a_stale_pass_can_be_aborted_without_inventing_a_result():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    ok(run("pass-start", cwd=d).returncode != 0, "a second pass must wait for the first")
    write(d, "a.py", "v3\n")                       # the scope moved mid-review
    run("pass-abort", "--reason", "scope moved mid-review", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0, "an aborted pass must never satisfy acceptance")
    run("validate", "--", "true", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0, "after aborting, a fresh pass must open")
    ok("aborted" in run("status", cwd=d, check=True).stdout,
       "status must show the aborted pass rather than hiding it")
    ok(run("pass-abort", cwd=d).returncode == 0, "an open pass is abortable")
    ok(run("pass-abort", cwd=d).returncode != 0, "there is nothing left to abort")


@case
def a_fix_mode_area_reviews_over_inherited_red_but_never_accepts_over_it():
    """Both project modes inherit the area as it stands, so a failing check is a property of
    it, not evidence about a change — but nothing is waved through: `accept` still refuses,
    so an area whose inherited failures were not fixed closes `incomplete`."""
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)     # fix mode, not report-only
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0,
       "fix mode must still let a reviewer read an area whose checks already fail")
    run("pass-record", "--score", "7", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0, "accept must still refuse over red validation")
    findings(d, "pkg")
    run("project", "close", cwd=d, check=True)
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "incomplete" and any("red" in b for b in led["blockers"]),
       f"the area must close incomplete naming the red check, got {led}")
    d2 = repo()
    write(d2, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d2, check=True)
    run("validate", "--", "false", cwd=d2)
    ok(run("pass-start", cwd=d2).returncode != 0,
       "task mode is unchanged: red validation still blocks the pass")


@case
def a_red_check_is_labelled_inherited_or_regressed():
    """Which one it is decides what the reviewer is told, and the loop's own history
    answers it: a check this loop broke is about the change, not the area."""
    d = repo({"pkg/a.py": "x\n", "pkg/flag": "ok\n"})
    check = "test \"$(cat pkg/flag)\" = ok"
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)                  # never green in this loop
    run("validate", "--", check, cwd=d)                    # green now
    out = run("pass-start", cwd=d, check=True).stdout
    ok("[inherited]" in out, f"a never-green check must read as inherited: {out}")
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "pkg/flag", "broken\n")                       # the loop's own batch breaks it
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)
    run("validate", "--", check, cwd=d)
    out = run("pass-start", cwd=d, check=True).stdout + run("status", cwd=d, check=True).stdout
    ok("[regressed]" in out, f"a check green earlier in this loop must read as regressed: {out}")
    ok("[inherited]" in out, "the never-green one must still read as inherited")


@case
def report_only_reviews_an_area_whose_checks_are_already_red():
    """A red repository is exactly the kind that gets a full review commissioned."""
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0,
       "report-only must review inherited code whose checks already fail")
    run("pass-record", "--score", "7", "--findings", "0", "--understood",
        "--test-evidence", "inadequate", cwd=d, check=True)
    ok(run("project", "close", cwd=d).returncode != 0,
       "a pass opened over red validation must reach the findings file even with no findings")
    findings(d, "pkg", "the area's own checks fail\n")
    ok(run("project", "close", cwd=d).returncode == 0, "with the red checks written down it closes")
    d2 = repo({"pkg/a.py": "x\n"})
    write(d2, "pkg/a.py", "y\n")
    run("init", "--", "pkg", cwd=d2, check=True)
    run("validate", "--", "false", cwd=d2)
    ok(run("pass-start", cwd=d2).returncode != 0,
       "outside report-only, red validation must still block the pass")


@case
def the_documented_task_workflow_runs_as_written():
    """Walk SKILL.md steps 0-6 verbatim. Every earlier dead end was found by doing this."""
    d = repo({"src/a.py": "v1\n"})
    write(d, "src/a.py", "v1\nv2\n")
    run("init", "--max-passes", "5", "--task-brief", "a.py must end with v2.",
        "--", "src/a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    out = run("pass-start", cwd=d, check=True).stdout
    ok("task brief" in out, "pass-start must echo the brief the reviewer is to be given")
    run("pass-record", "--score", "8.5", "--findings", "2", "--understood",
        "--test-evidence", "trusted", "--test-score", "7", cwd=d, check=True)
    run("resolve", "--fixed", "1", "--withdrawn", "1", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "the documented task flow must reach acceptance")
    ok(run("status", "--json", cwd=d, check=True).stdout.strip().startswith("{"),
       "status --json must be machine-readable for the final report")


@case
def the_documented_project_workflow_runs_as_written():
    """Walk SKILL.md P0-P2 verbatim, in fix mode, including resume after a lost loop."""
    d = repo({"pkg/a.py": "v1\n", "lib/b.py": "v1\n"})
    run("project", "init", "--max-passes", "5", "--", "lib", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    ok(run("accept", cwd=d).returncode == 0, "fix mode: a clean area must accept")
    findings(d, "lib")
    run("project", "close", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    ok(run("reset", cwd=d).returncode != 0,
       "reset inside an open area must refuse — `project close` is what frees it")
    os.remove(os.path.join(d, ".loop-review", "state.json"))   # the session dies mid-area
    ok(run("project", "next", cwd=d).returncode != 0, "a stranded area must block the queue")
    ok(run("project", "close", cwd=d).returncode != 0, "and a plain close must refuse")
    ok(run("project", "close", "--force", cwd=d).returncode == 0,
       "P2's documented recovery must actually free the queue")
    st = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok([x["status"] for x in st["areas"]] == ["accepted", "incomplete"],
       f"ledger must record both outcomes, got {[x['status'] for x in st['areas']]}")


@case
def state_dir_inside_scope_does_not_deadlock():
    """`init -- .` must stay openable: the loop's own writes are not reviewed changes."""
    d = repo()
    write(d, "a.py", "v1\nv2\n")
    run("init", "--", ".", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0, "a scope containing .loop-review must not deadlock pass-start")


# ---------------------------------------------------------------------------- fingerprint

@case
def fingerprint_frames_path_and_content():
    a = repo({"area/ab": "c"})
    b = repo({"area/a": "bc"})
    fa = run("init", "--mode", "project", "--", "area", cwd=a, check=True).stdout
    fb = run("init", "--mode", "project", "--", "area", cwd=b, check=True).stdout
    ha = fa.split("fingerprint ")[1].strip()
    hb = fb.split("fingerprint ")[1].strip()
    ok(ha != hb, f"distinct states must not share a fingerprint ({ha} == {hb})")


@case
def fingerprint_command_matches_the_gates():
    d = repo({"src/a.py": "v1\n"})
    run("init", "--mode", "project", "--", "src", cwd=d, check=True)
    state = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    printed = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(printed == state["fingerprint_current"],
       f"project mode: `fingerprint` printed {printed}, gates use {state['fingerprint_current']}")
    run("reset", cwd=d, check=True)
    write(d, "src/a.py", "v2\n")
    sh("git commit -qam work", d)
    run("init", "--base", "HEAD~1", "--", "src", cwd=d, check=True)
    state = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    printed = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(printed == state["fingerprint_current"],
       f"--base: `fingerprint` printed {printed}, gates use {state['fingerprint_current']}")


# ----------------------------------------------------------------------- validation gates

@case
def red_validation_blocks_a_pass():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)
    ok(run("pass-start", cwd=d).returncode != 0, "red validation must block pass-start")


@case
def a_command_that_never_ran_is_droppable_a_failure_is_not():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "definitely-not-a-command", cwd=d)
    ok(run("validate-drop", "--", "definitely-not-a-command", cwd=d).returncode == 0,
       "a command that never ran (127) must be droppable")
    run("validate", "--", "false", cwd=d)
    ok(run("validate-drop", "--", "false", cwd=d).returncode != 0,
       "a command that ran and failed must not be droppable without --force")


@case
def an_inherited_check_cannot_be_retired_by_letting_it_fail_to_start():
    """The evidence set may only shrink by a human decision — from every direction.

    `validate-drop` waives its refusal for exits 126/127, because a command that never ran
    is bookkeeping, not evidence. Read as "no record here", that exemption swallowed the
    inherited case too: run a check the last pass rested on after its tool is gone, take the
    127, drop the record without `--force`, and `pass-start` accepted the shrunken set. The
    tool disappearing is a fact about the environment, not permission to review on less.
    """
    d = repo({"src/a.py": "1\n"})
    tool = os.path.join(d, "suite.sh")
    _io.open(tool, "w").write("#!/bin/sh\nexit 0\n")
    os.chmod(tool, 0o755)
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)

    write(d, "src/a.py", "3\n")                       # the fix batch
    run("resolve", "--fixed", "1", cwd=d, check=True)
    os.remove(tool)                                   # the check can no longer start
    run("validate", "--", tool, cwd=d)                # -> exit 127
    run("validate", "--", "echo lint", cwd=d)

    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode != 0 and "--force" in r.stderr,
       f"retiring an inherited check must need --force even at exit 127: rc={r.returncode} {r.stderr.strip()[:160]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0, "and the pass must not open while that check is unaccounted for")

    r = run("validate-drop", "--force", "--reason", "tool retired", "--", tool, cwd=d)
    ok(r.returncode == 0, f"--force must still retire it: {r.stderr.strip()[:160]}")
    ok(run("pass-start", cwd=d).returncode == 0, "after the human decision the pass opens")


@case
def a_fresh_typo_no_pass_rested_on_still_clears_without_force():
    """The other side of the same rule: the never-ran exemption must survive the fix, or a
    mistyped command would strand the loop it never contributed evidence to."""
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    run("validate", "--", "pytset tests/", cwd=d)     # typo, no pass rested on it
    r = run("validate-drop", "--", "pytset tests/", cwd=d)
    ok(r.returncode == 0, f"a typo no pass leaned on must still clear freely: {r.stderr.strip()[:160]}")
    ok(run("pass-start", cwd=d).returncode == 0, "and the pass opens on the unchanged evidence set")


@case
def validation_that_moves_the_scope_is_not_evidence():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "echo drift >> a.py", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0, "a validation run that edited the scope must not open a pass")
    run("validate", "--", "true", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0, "a settled re-run must open the pass")


@case
def evidence_may_not_shrink():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    green(d, "true", "echo lint")
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "a.py", "v3\n")
    run("validate", "--", "true", cwd=d)
    ok(run("pass-start", cwd=d).returncode != 0,
       "a later pass must not rest on fewer checks than the one before it")
    run("validate", "--", "echo lint", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0, "restoring the check must open the pass")


@case
def an_unchanged_scope_is_not_a_new_pass():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    pass_through(d, "9", "0", "trusted")
    ok(run("pass-start", cwd=d).returncode != 0,
       "a second full pass on identical code must be refused")


# ------------------------------------------------------------------------ acceptance gates

@case
def inadequate_test_evidence_blocks_acceptance():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "inadequate", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0, "inadequate test evidence must block accept")
    run("amend", "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "amending the verdict must unblock accept")


@case
def unresolved_findings_block_acceptance():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0, "unresolved findings must block accept")
    run("resolve", "--fixed", "2", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "resolving them must unblock accept")


@case
def amend_may_not_lower_findings():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "3", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("amend", "--findings", "1", cwd=d).returncode != 0,
       "amend must not lower the reported finding count")


# ------------------------------------------------------------------------- argument types

@case
def argument_values_are_validated():
    d = repo()
    write(d, "a.py", "v2\n")
    ok(run("init", "--max-passes", "0", "--", "a.py", cwd=d).returncode != 0,
       "--max-passes 0 would create a loop that can never open a pass")
    ok(run("init", "--max-passes", "-3", "--", "a.py", cwd=d).returncode != 0,
       "--max-passes must reject a negative count")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    ok(run("pass-record", "--score", "4000", "--findings", "0", "--understood",
           "--test-evidence", "trusted", cwd=d).returncode != 0,
       "--score must stay inside the 1-10 anchors")


# ------------------------------------------------------------------------------- the brief

@case
def task_brief_file_is_read_from_the_callers_directory():
    d = repo({"a.py": "v1\n", "sub/keep.txt": "x\n"})
    write(d, "a.py", "v2\n")
    write(d, "brief.md", "ROOT-BRIEF\n")
    with open(os.path.join(d, "sub", "brief.md"), "w") as f:
        f.write("SUB-BRIEF\n")
    subprocess.run([sys.executable, LOOP, "init", "--task-brief-file", "brief.md",
                    "--", "../a.py"], cwd=os.path.join(d, "sub"),
                   capture_output=True, text=True)
    state = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    ok(state.get("task_brief") == "SUB-BRIEF",
       f"--task-brief-file must resolve next to the caller, got {state.get('task_brief')!r}")


# ------------------------------------------------------------------------------ durability

@case
def state_is_written_atomically():
    """A write that dies partway must leave the previous state readable. Provoked directly:
    no CLI sequence can interrupt a write on purpose."""
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    lr = module()
    here = os.getcwd()
    os.chdir(d)
    try:
        with open(lr.STATE_FILE) as f:
            good = json.load(f)
        try:
            lr.save(dict(good, poison=object()))
        except TypeError:
            pass
        try:
            with open(lr.STATE_FILE) as f:
                after = json.load(f)
        except ValueError:
            after = None
        ok(after == good, "an interrupted write must leave the previous state intact")
    finally:
        os.chdir(here)
    run("reset", cwd=d, check=True)
    left = os.path.isdir(os.path.join(d, ".loop-review")) and \
        [f for f in os.listdir(os.path.join(d, ".loop-review")) if f.endswith(".tmp")]
    ok(not left, f"reset must clear the temp file a failed write left behind, found {left}")


# --------------------------------------------------------------------------- project mode

@case
def report_only_close_requires_the_findings_file():
    d = repo({"pkg/a.py": "v1\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("project", "close", cwd=d).returncode != 0,
       "report-only: findings must reach the findings file before the area closes")
    findings(d, "pkg", "two findings\n")
    ok(run("project", "close", cwd=d).returncode == 0, "with the file written it must close")


@case
def forcing_a_close_past_a_missing_findings_file_never_yields_accepted():
    """`--force` never lets an area pass — fix mode was the hole.

    Report-only folded the missing file into the area's blockers; fix mode only skipped the
    refusal, so a clean loop closed `accepted` with `findings_file: null` after `state.json`
    was deleted. The ledger then claimed a review that no longer existed anywhere: the row
    said the area passed, and the reviewer's output — the whole deliverable for an area with
    no findings — was gone.
    """
    d = repo({"pkg/a.py": "v1\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)     # fix mode
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    ok(run("project", "close", cwd=d).returncode != 0,
       "the missing findings file must be refused while it can still be written")
    r = run("project", "close", "--force", cwd=d)
    ok(r.returncode == 0, f"--force must settle the area, got rc={r.returncode}")
    area = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "incomplete",
       f"a forced close with no surviving review must not read as accepted, got {area['status']}")
    ok(any("findings" in b for b in area["blockers"]),
       f"and the reason must say so: {area['blockers']}")


@case
def a_clean_area_with_its_findings_file_still_closes_accepted():
    """The other side: the gate must not turn every fix-mode area incomplete."""
    d = repo({"pkg/a.py": "v1\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9.5", "0", "trusted")
    _io.open(os.path.join(d, stem), "w").write("A. no actionable findings\nB. understood\n")
    ok(run("project", "close", cwd=d).returncode == 0, "a clean area with its file must close")
    area = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "accepted" and area["findings_file"],
       f"and it must be recorded accepted with its file: {area['status']}, {area['findings_file']}")


@case
def multi_path_areas_are_one_area_with_a_bounded_slug():
    files = {f"pkg/f{i}.py": "x\n" for i in range(11)}
    d = repo(files)
    args = []
    for i, name in enumerate(sorted(files)):
        if i:
            args.append("+")
        args.append(name)
    r = run("project", "init", "--report-only", "--", *args, cwd=d, check=True)
    ok(r.stdout.count("\n  - ") == 1, "`+` must glue every path into a single area")
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    ok(len(os.path.basename(stem)) <= 100,
       f"findings filename must stay writable, got {len(os.path.basename(stem))} bytes")
    os.makedirs(os.path.join(d, os.path.dirname(stem)), exist_ok=True)
    with open(os.path.join(d, stem), "w") as f:
        f.write("x\n")
    ok(os.path.exists(os.path.join(d, stem)), "the findings file must be creatable")


@case
def a_dangling_glue_operator_is_refused_at_either_end():
    """`+` joins two paths, so an end of the list is not a place it can do its job.

    A leading `+` was refused from the start; a trailing one was dropped in silence, and
    dropping it changes the queue rather than just the wording — `a b +` queued two separate
    areas where the operator asked for a join, and nothing in the output said a token had
    been ignored. The refusal has to be symmetric, and it must not cost the valid forms.
    """
    d = repo({"a/x.py": "1\n", "b/y.py": "2\n"})
    r = run("project", "init", "--", "a", "b", "+", cwd=d)
    ok(r.returncode != 0 and "+" in r.stderr,
       f"a trailing `+` must be refused, not dropped: rc={r.returncode} {r.stderr.strip()[:120]}")
    ok(not os.path.exists(os.path.join(d, ".loop-review", "project.json")),
       "and a refused queue must not be written to the ledger")
    r = run("project", "init", "--", "+", "a", cwd=d)
    ok(r.returncode != 0, "a leading `+` must still be refused")

    r = run("project", "init", "--", "a", "+", "b", cwd=d, check=True)
    ok(r.stdout.count("\n  - ") == 1, "the glued form must still queue one area")
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok(led["areas"][0]["paths"] == ["a", "b"], f"joined into one area: {led['areas'][0]['paths']}")
    r = run("project", "init", "--force", "--", "a", "b", cwd=d, check=True)
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))
    ok([x["paths"] for x in led["areas"]] == [["a"], ["b"]],
       f"and without `+` they stay separate: {[x['paths'] for x in led['areas']]}")


@case
def the_script_carries_no_unreachable_helper():
    """A helper nobody calls is a claim about the workflow that nothing enforces.

    `findings_digest` outlived the rule it implemented: the digest comparison was replaced by
    the `.prev.md` rotation, and the function stayed behind for releases — read as live
    machinery by anyone auditing how the findings gate works, while the real gate was a plain
    existence check somewhere else. Names referenced only from `selftest.py` count as live;
    everything else must be reachable from within the script.
    """
    src = _io.open(os.path.join(ROOT, "scripts", "loop_review.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    defined = {n.name: n.lineno for n in tree.body if isinstance(n, ast.FunctionDef)}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # The entry point is called by the interpreter, and selftest drives the CLI through these.
    used |= {"main", "build_parser"}
    used |= set(re.findall(r"module\(\)\.([A-Za-z_]\w*)",
                           _io.open(os.path.join(ROOT, "scripts", "selftest.py"),
                                    encoding="utf-8").read()))
    dead = sorted((name, line) for name, line in defined.items() if name not in used)
    ok(not dead, "unreachable helper(s): " + "; ".join(f"{n} at line {l}" for n, l in dead))


# ----------------------------------------------------------------- prose vs the real CLI

# README.md and AGENTS.md name commands and flags as densely as SKILL.md does, and AGENTS.md
# itself calls the CLI this skill's public API — leaving them out put the drift where it was
# least visible.
DOCS = ("SKILL.md", "README.md", "AGENTS.md", "references/reviewer-prompt.md",
        "references/reviewer-prompt-area.md", "references/review-dimensions.md",
        "references/project-mode.md")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBCOMMANDS = ("init", "validate-drop", "validate", "pass-start", "pass-abort", "pass-record",
               "amend", "resolve", "accept", "status", "fingerprint", "scope", "brief",
               "project init", "project next", "project close", "project status", "reset")


def command_hits(line):
    """Invocations named in one line of prose or one message string, normalised for parsing.

    Placeholders are made concrete (`<paths...>` -> a word, `a|b` -> `a`, `[--flag v]` ->
    `--flag v`) so the line can be handed to the real parser: what is being checked is that
    the subcommand and its flags exist, not that the example values are meaningful.
    """
    hits = [m.group(1) for m in
            re.finditer(r"`?(?:python3\s+\S*loop_review\.py|LR)\s+([^`\n]+)", line)]
    # Bare inline mentions — `validate-drop --force`, `project close --force` — name
    # the CLI just as much as a fenced `LR` line, and the CLI is this skill's public
    # API. They were unchecked while every fenced line was parsed.
    hits += [m.group(1) for m in re.finditer(r"`((?:%s)(?:\s+[^`\n]*)?)`"
                                             % "|".join(SUBCOMMANDS), line)]
    out = []
    for body in hits:
        body = body.split("#")[0]
        if body.strip() in ("--help",):               # not a subcommand invocation
            continue
        body = body.rstrip("`.,;:")
        body = re.sub(r"<[^>]*>", "ARG", body)
        body = re.sub(r"\[([^\]]*)\]", r"\1", body)
        body = re.sub(r"(\w)\|[\w|-]+", r"\1", body)
        out.append(body.strip())
    return out


def script_message_lines():
    """Every invocation the script *tells the operator to run*, from its own die()/print().

    A message naming a command that does not exist is the same dead end as prose naming one,
    and it is worse hidden: `cmd_project_close` advised `reset --force` for months, a flag
    `reset` never had, while the only other route out of that state was refused as well.
    Messages are prose too — they are read by the agent and are part of the CLI's contract.
    """
    src = _io.open(os.path.join(ROOT, "scripts", "loop_review.py"), encoding="utf-8").read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("die", "print")):
            continue
        for arg in ast.walk(node):
            # f-strings contribute their literal parts; an interpolated value is not a
            # command name, so dropping it cannot hide one.
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                for body in command_hits(arg.value):
                    out.append((f"scripts/loop_review.py:{node.lineno}", arg.value.strip(), body))
    return out


def doc_command_lines():
    """Every loop_review invocation printed in the normative prose, normalised for parsing.

    Placeholders are made concrete (`<paths...>` -> a word, `a|b` -> `a`, `[--flag v]` ->
    `--flag v`) so the line can be handed to the real parser: what is being checked is that
    the subcommand and its flags exist, not that the example values are meaningful.
    """
    out = []
    for rel in DOCS:
        text = _io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        text = text.replace("\\\n", " ")                     # fenced line continuations
        for raw in text.splitlines():
            line = raw.strip().lstrip("#").strip()
            for body in command_hits(line):
                out.append((rel, raw.strip(), body))
    return out


def assert_invocations_parse(lines):
    """Hand every named invocation to the real parser; collect what it will not accept."""
    parser = module().build_parser()
    for rel, raw, body in lines:
        try:
            tokens = shlex.split(body)
        except ValueError:
            FAILURES.append(f"{rel}: unparseable command line: {raw}")
            continue
        if not tokens:
            continue
        # Prose legitimately names a flag without its value ("record it with `--scope-note`"),
        # so a value-taking flag at the end is retried with a placeholder — a word, then a
        # number, because a typed flag like `--findings` rejects the word and would otherwise
        # read as a broken command. An unknown flag or subcommand fails every attempt, which
        # is what this case is for.
        # A bare subcommand mention ("`pass-record` requires only --findings") names the CLI
        # without invoking it; demanding its mandatory flags would flag correct prose. Check
        # that the name exists instead.
        if len(tokens) == 1 or (len(tokens) == 2 and tokens[0] == "project"):
            if " ".join(tokens) not in SUBCOMMANDS:
                FAILURES.append(f"{rel}: `{raw}` names no such subcommand")
            continue
        for attempt in (tokens, tokens + ["ARG"], tokens + ["1"]):
            buf = _io.StringIO()
            try:
                with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                    parser.parse_args(attempt)
                break
            except (SystemExit, argparse.ArgumentError):
                last = buf.getvalue().strip().splitlines()[-1:] or [""]
        else:
            FAILURES.append(f"{rel}: `{raw}` is not accepted by the CLI ({last})")


@case
def every_command_in_the_prose_exists_in_the_cli():
    """The prose is executed by a model; a flag it names that the CLI lacks is a dead end,
    and exercising the script cannot catch it."""
    lines = doc_command_lines()
    ok(len(lines) >= 12, f"expected the prose to show the command surface, found {len(lines)}")
    assert_invocations_parse(lines)


@case
def every_command_the_script_recommends_exists_in_the_cli():
    """A refusal that names the way out is only useful if that way exists.

    `project close` sent the operator to `reset --force`, which `reset` has never accepted,
    while the plain `reset` it also implied was refused mid-area — the two commands together
    left no route out of the state at all. Checking the prose could not see it: the dead end
    was quoted in a die() string, not in a document.
    """
    lines = script_message_lines()
    ok(len(lines) >= 10, f"expected the messages to name the CLI, found {len(lines)}")
    assert_invocations_parse(lines)


@case
def the_prose_never_invokes_the_script_by_a_repo_relative_path():
    """SKILL.md itself forbids it: the reviewed repository need not have a scripts/ at all,
    and if it does, that copy is a different one.

    Runtime prose only. AGENTS.md's build instructions are run *in this repository*, where
    `scripts/loop_review.py` is exactly the right path.
    """
    for rel in (d for d in DOCS if d not in ("AGENTS.md", "README.md")):
        text = _io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"python3\s+scripts/loop_review\.py", line):
                FAILURES.append(f"{rel}:{n} invokes the script by a repo-relative path: {line.strip()}")


@case
def a_pass_records_what_the_reviewer_gave_and_amend_fills_the_rest():
    """Record what the reviewer gave, `amend` the rest — the alternative was inventing a
    verdict, which is what the loop exists to stop."""
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--findings", "0", "--understood", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0, "an unassessed verdict must still block acceptance")
    run("amend", "--test-evidence", "trusted", "--score", "9.5", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "amending the missing values must unblock it")


@case
def a_clean_report_only_area_still_leaves_its_review_behind():
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    ok(run("project", "close", cwd=d).returncode != 0,
       "a clean area's review must be written down too — closing deletes the loop state")
    findings(d, "pkg", "A. No actionable findings\nB. ...\nC. trusted\nD. 9.5\n")
    ok(run("project", "close", cwd=d).returncode == 0, "with the review written it closes")
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "reviewed", f"expected reviewed, got {led['status']}")
    ok(led.get("test_evidence") == "trusted",
       "the ledger must carry the verdict that gates exit condition 4")


@case
def an_unreviewable_area_does_not_stall_the_queue():
    """The reviewer never produced a usable pass. That is an outcome, not a hatch case."""
    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg", "lib", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    r = run("project", "close", cwd=d)
    ok(r.returncode == 0, "an area with no recorded pass must close without --force")
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "incomplete" and led["blockers"],
       f"it must be recorded incomplete with its blocker, got {led['status']}")
    ok(run("project", "next", cwd=d).returncode == 0, "the queue must continue to the next area")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--findings", "0", cwd=d, check=True)      # no --understood
    ok(run("project", "close", cwd=d).returncode == 0,
       "an area whose reviewer showed no understanding must close as incomplete, not stall")


@case
def hunk_level_scope_notes_live_in_the_state():
    d = repo({"a.py": "v1\n"})
    write(d, "a.py", "v2\n")
    run("init", "--scope-note", "only the retry hunk in a.py; the logging change is not ours",
        "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    started = run("pass-start", cwd=d, check=True).stdout
    ok("only the retry hunk" in started, "pass-start must echo the scope note every pass")
    ok("only the retry hunk" in run("status", cwd=d, check=True).stdout,
       "status must echo it too")
    ok(run("brief", "--scope-note", "something else", cwd=d).returncode != 0,
       "silently replacing a recorded scope note must be refused")
    d2 = repo({"a.py": "v1\n"})
    write(d2, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d2, check=True)
    run("brief", "--task-brief", "late criteria", cwd=d2, check=True)
    ok("late criteria" in run("status", cwd=d2, check=True).stdout,
       "a brief that arrives after init must be recordable without rebuilding the loop")
    ok(run("brief", "--task-brief", "different", cwd=d2).returncode != 0,
       "replacing a recorded brief needs --force")


@case
def a_relative_base_is_pinned_so_a_later_commit_cannot_narrow_the_scope():
    """`HEAD~1` is relative to HEAD, and the workflow may commit at the user's request."""
    d = repo({"src/a.py": "one\n"})
    write(d, "src/a.py", "one\ntwo\n")
    sh("git add -A && git commit -qm first", d)
    write(d, "src/a.py", "one\ntwo\nthree\n")
    sh("git add -A && git commit -qm second", d)
    run("init", "--base", "HEAD~2", "--", "src", cwd=d, check=True)
    st = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    ok(st["base"] and len(st["base"]) >= 40,
       f"the base must be pinned to a commit id, got {st['base']!r}")
    before = st["fingerprint_current"]
    write(d, "src/a.py", "one\ntwo\nthree\nfour\n")
    sh("git add -A && git commit -qm third", d)
    after = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(after != before, "a new commit must widen the diff, not silently reset the base")
    ok(b"four" in subprocess.run(["git", "diff", st["base"], "--", "src"], cwd=d,
                                 capture_output=True).stdout
       and b"two" in subprocess.run(["git", "diff", st["base"], "--", "src"], cwd=d,
                                    capture_output=True).stdout,
       "the pinned base must still cover the whole task, not only the newest commit")


@case
def an_unusable_recorded_review_is_not_what_the_workflow_asks_for():
    """The prose now says: judge usability BEFORE recording; abort is the exit."""
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-abort", "--reason", "reviewer unusable after one clarification", cwd=d, check=True)
    ok(run("pass-start", cwd=d).returncode == 0,
       "a fresh reviewer after an aborted pass must not need --force, on the same fingerprint")
    run("pass-record", "--findings", "0", "--understood", "--score", "9.5",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("pass-abort", cwd=d).returncode != 0,
       "once recorded, the judgement is spent — abort must refuse")


@case
def a_fix_outside_the_scope_can_be_brought_under_the_gates():
    """A fix landing next door moves no fingerprint, so no gate sees it until the scope is
    widened — and widening must keep the pass history."""
    d = repo({"src/client.ts": "a\n", "src/helper.ts": "b\n"})
    write(d, "src/client.ts", "a2\n")
    run("init", "--", "src/client.ts", cwd=d, check=True)
    pass_through(d, "8", "1", "trusted")
    write(d, "src/helper.ts", "b2\n")                  # the fix lands outside the scope
    run("resolve", "--fixed", "1", cwd=d, check=True)
    before = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(run("accept", cwd=d).returncode == 0,
       "precondition: an out-of-scope fix is invisible until the scope is widened")
    run("scope", "--", "src/helper.ts", cwd=d, check=True)
    after = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(after != before, "widening the scope must move the reviewed state")
    ok(run("accept", cwd=d).returncode != 0,
       "the earlier review must now be stale, forcing a pass that sees the added path")
    st = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    ok(st["scope"] == ["src/client.ts", "src/helper.ts"], f"scope: {st['scope']}")
    ok(len(st["passes"]) == 1, "widening must keep the pass history, unlike init --force")
    ok(run("scope", "--", "src/client.ts", cwd=d).returncode != 0,
       "a path already in scope is not a widening")


@case
def an_area_with_no_runnable_check_is_recorded_honestly():
    """Prose and config have nothing to run, and `validate -- true` would reach the reviewer
    as a check the author claims to have run."""
    d = repo({"docs/guide.md": "text\n"})
    write(d, "docs/guide.md", "text\nmore\n")
    run("init", "--", "docs", cwd=d, check=True)
    ok(run("pass-start", cwd=d).returncode != 0, "no validation at all must still block a pass")
    ok(run("validate", "--none", cwd=d).returncode != 0, "--none must demand a reason")
    run("validate", "--none", "--reason", "markdown only: no test, lint or build applies",
        cwd=d, check=True)
    ok(run("pass-start", cwd=d).returncode == 0, "an honest 'nothing to run' must open the pass")
    out = run("status", cwd=d, check=True).stdout
    ok("no executable check applies: markdown only" in out,
       "status must carry the reason, since that is what reaches the reviewer's prompt")
    ok("-> exit 0" not in out.split("validation")[-1],
       "the absence must not render as a command that exited zero")


@case
def a_project_area_is_freed_by_close_not_by_reset():
    """A task loop ends with `reset`; an area ends with `project close`. Confusing them
    discarded an area that had passed every gate."""
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    ok(run("accept", cwd=d).returncode == 0, "precondition: the area passes every gate")
    ok(run("reset", cwd=d).returncode != 0, "reset must refuse rather than discard the outcome")
    ok(run("project", "close", cwd=d).returncode != 0,
       "a clean area's review must be written down in fix mode too")
    findings(d, "pkg")
    run("project", "close", cwd=d, check=True)
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "accepted" and led["score"] == 9.5 and led["passes"] == 1,
       f"the outcome must survive: {led}")


@case
def a_previous_review_is_set_aside_not_reused():
    """The findings path repeats on every review of an area.

    A leftover file used to satisfy the close gate while this run's findings died with
    `state.json`. Comparing digests then refused a legitimately identical repeat review for
    ever, and an mtime fallback accepted a merely touched file — so `project next` now moves
    the old review to `<slug>.prev.md` and the gate is a plain existence check again.
    """
    d = repo({"pkg/a.py": "x\n"})
    fdir = os.path.join(d, ".loop-review", "findings")

    def review(write_text):
        run("project", "init", "--report-only", "--force", "--", "pkg", cwd=d, check=True)
        run("project", "next", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "7", "--findings", "3", "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        if write_text is not None:
            findings(d, "pkg", write_text)
        return run("project", "close", cwd=d)

    ok(review("first review\n").returncode == 0, "the first round closes with its own file")
    ok(review(None).returncode != 0,
       "a second review must not be closed by the previous run's findings file")
    findings(d, "pkg", "second review\n")
    ok(run("project", "close", cwd=d).returncode == 0, "writing this run's output closes it")
    live = [f for f in os.listdir(fdir) if f.endswith(".md") and not f.endswith(".prev.md")]
    prev = [f for f in os.listdir(fdir) if f.endswith(".prev.md")]
    ok(len(live) == 1 and open(os.path.join(fdir, live[0])).read().strip() == "second review",
       "the surviving deliverable must be the current review")
    ok(len(prev) == 1 and open(os.path.join(fdir, prev[0])).read().strip() == "first review",
       "and the previous review must be kept, not destroyed")


@case
def status_prints_what_the_reviewer_prompt_needs():
    d = repo({"a.py": "v1\n"})
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    out = run("status", cwd=d, check=True).stdout
    ok(os.path.realpath(d) in out or os.path.basename(d) in out,
       "status must print the repository path the reviewer prompt opens with")


@case
def the_pass_limit_is_a_hard_stop():
    """The refusal the whole script exists for."""
    d = repo()
    write(d, "a.py", "v1\nv2\n")
    run("init", "--max-passes", "2", "--", "a.py", cwd=d, check=True)
    for i in range(2):
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "8", "--findings", "1", "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        run("resolve", "--fixed", "1", cwd=d, check=True)
        write(d, "a.py", f"v{i + 3}\n")
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "limit" in (r.stdout + r.stderr).lower(),
       f"a third pass must be refused at --max-passes 2: rc={r.returncode}")
    ok(run("pass-start", "--force", cwd=d).returncode == 0,
       "--force is the documented override, and must still work")


@case
def resolved_never_exceeds_the_findings_reported():
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("resolve", "--fixed", "5", cwd=d, check=True)
    st = json.load(open(os.path.join(d, ".loop-review", "state.json")))
    ok(st["passes"][-1]["result"]["resolved"] == 2,
       f"resolved must clamp to findings, got {st['passes'][-1]['result']}")


@case
def a_scope_that_empties_after_init_stops_the_loop():
    """A scope that empties mid-loop — reverted, stashed, committed, deleted — must stop the
    loop, not pass every gate over nothing."""
    d = repo()
    write(d, "a.py", "v1\nv2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    sh("git checkout -- a.py", d)                              # the change is reverted
    r = run("accept", cwd=d)
    # Assert the reason, not just the exit code: a stale fingerprint also fails `accept`
    # here, so a bare `returncode != 0` stays green even with the emptiness check deleted.
    ok(r.returncode != 0 and "scope is empty" in r.stdout + r.stderr,
       f"accept must name the empty scope as a blocker: {r.stdout.strip()}")
    run("validate", "--", "true", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 2,
       "and a pass over nothing must not open")
    d2 = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d2, check=True)
    run("project", "next", cwd=d2, check=True)
    run("validate", "--", "true", cwd=d2)
    run("pass-start", cwd=d2, check=True)
    run("pass-record", "--score", "10", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d2, check=True)
    sh("git rm -q -r pkg && git commit -qm drop", d2)           # the area is deleted
    r = run("accept", cwd=d2)
    ok(r.returncode != 0 and "scope is empty" in r.stdout + r.stderr,
       f"a vanished area must be blocked by name, not incidentally: {r.stdout.strip()}")
    findings(d2, "pkg")
    run("project", "close", cwd=d2, check=True)
    led = json.load(open(os.path.join(d2, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "incomplete",
       f"and it must never reach the ledger as accepted, got {led['status']}")


@case
def a_scope_path_git_cannot_see_is_reported():
    d = repo({"src/a.py": "1\n", ".gitignore": "cfg/\n"})
    os.makedirs(os.path.join(d, "cfg"))
    write(d, "cfg/local.yaml", "k: v\n")
    write(d, "src/a.py", "2\n")
    r = run("init", "--", "src/a.py", "cfg/local.yaml", "src/typoo.py", cwd=d, check=True)
    err = r.stderr
    ok("cfg/local.yaml" in err and "ignored" in err,
       f"an ignored scope path must be reported: {err}")
    ok("src/typoo.py" in err and "does not exist" in err,
       f"a mistyped scope path must be reported: {err}")


@case
def acceptance_refuses_by_name_for_each_of_the_four_conditions():
    """Each blocker proved on its own: a non-zero exit is also produced by the others."""
    def loop(d):
        run("init", "--", "a.py", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)

    def says(d, phrase, label):
        r = run("accept", cwd=d)
        ok(r.returncode != 0 and phrase in r.stdout + r.stderr,
           f"{label}: expected `{phrase}`, got {r.stdout.strip()!r}")

    d = repo(); write(d, "a.py", "v2\n"); loop(d)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "a.py", "rewritten after the review\n")
    run("validate", "--", "true", cwd=d)
    says(d, "stale", "condition 2 — the pass must describe the current state")

    d = repo(); write(d, "a.py", "v2\n"); loop(d)
    run("pass-record", "--score", "9", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    says(d, "unresolved", "condition 2 — unresolved findings")

    d = repo(); write(d, "a.py", "v2\n"); loop(d)
    run("pass-record", "--score", "9", "--findings", "0",
        "--test-evidence", "trusted", cwd=d, check=True)      # no --understood
    says(d, "understanding", "condition 3 — credible understanding")

    d = repo(); write(d, "a.py", "v2\n"); loop(d)
    run("pass-record", "--score", "9", "--findings", "0", "--understood", cwd=d, check=True)
    says(d, "test evidence", "condition 4 — the verdict must exist")

    d = repo(); write(d, "a.py", "v2\n"); loop(d)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "a.py", "v3\n")                                  # moves the scope, drops validation
    says(d, "no validation", "condition 1 — validation on the current state")


@case
def a_project_area_cannot_be_closed_from_someone_elses_loop():
    """A foreign loop must not be recorded as this area's outcome."""
    d = repo({"pkg/a.py": "1\n", "lib/b.py": "2\n"})
    run("project", "init", "--", "pkg", "lib", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)                  # area `pkg`
    write(d, "lib/b.py", "3\n")
    r = run("init", "--force", "--", "lib/b.py", cwd=d)
    ok(r.returncode != 0, "re-initialising inside a live area must be refused")
    os.remove(os.path.join(d, ".loop-review", "state.json"))   # the loop is lost
    run("init", "--", "lib/b.py", cwd=d, check=True)           # a foreign changes-mode loop
    pass_through(d, "10", "0", "trusted")
    ok(run("accept", cwd=d).returncode == 0, "that foreign loop is itself acceptable")
    findings(d, "pkg")            # so the close cannot fail merely for a missing file
    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "is not area" in r.stdout + r.stderr,
       f"it must be refused as a foreign loop, by name: {(r.stdout + r.stderr).strip()}")


@case
def a_report_only_area_that_moved_after_its_pass_is_not_reviewed():
    """Report-only never reaches blockers(), so it must ask staleness itself."""
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "8", "1", "trusted")
    findings(d, "pkg", "three findings\n")
    write(d, "pkg/a.py", "completely rewritten after the review\n")
    run("project", "close", cwd=d, check=True)
    led = json.load(open(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "incomplete" and any("moved" in b for b in led["blockers"]),
       f"an area rewritten after its pass must not close `reviewed`: {led}")


@case
def an_identical_repeat_review_can_still_be_closed():
    """A clean area legitimately produces the same text twice."""
    d = repo({"pkg/a.py": "x\n"})
    text = "A. No actionable findings\n"
    for expect_ok in (True, True):
        run("project", "init", "--report-only", "--force", "--", "pkg", cwd=d, check=True)
        run("project", "next", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        findings(d, "pkg", text)                               # byte-identical each round
        ok(run("project", "close", cwd=d).returncode == 0,
           "an identical but freshly written review must close")


@case
def reverting_a_fix_batch_un_resolves_its_findings():
    """Fix, `resolve`, revert: the scope is back on the fingerprint the review called
    defective with `unresolved` at zero, so the resolution must not survive."""
    d = repo({"src/a.py": "buggy\n"})
    write(d, "src/a.py", "buggy\nchanged\n")
    run("init", "--", "src/a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "6", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    reviewed = run("fingerprint", cwd=d, check=True).stdout.strip()
    write(d, "src/a.py", "fixed\n")                        # the fix batch
    run("resolve", "--fixed", "2", cwd=d, check=True)
    write(d, "src/a.py", "buggy\nchanged\n")               # ...and it is reverted
    ok(run("fingerprint", cwd=d, check=True).stdout.strip() == reviewed,
       "precondition: the scope is back on the reviewed state")
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "not present" in r.stdout + r.stderr,
       f"accept must name the vanished fixes, not sign off: {(r.stdout + r.stderr).strip()}")


@case
def two_areas_never_share_a_findings_file():
    """`["src", "lib"]` and `["src/lib"]` both flatten to `src__lib`."""
    d = repo({"src/x.py": "1\n", "lib/y.py": "2\n", "src/lib/z.py": "3\n"})
    run("project", "init", "--report-only", "--", "src", "+", "lib", "src/lib", cwd=d, check=True)
    first = run("project", "next", cwd=d, check=True).stdout.split(
        "findings file: ")[1].splitlines()[0]
    pass_through(d, "8", "1", "trusted")
    with open(os.path.join(d, first), "w") as f:
        f.write("area one\n")
    run("project", "close", cwd=d, check=True)
    second = run("project", "next", cwd=d, check=True).stdout.split(
        "findings file: ")[1].splitlines()[0]
    ok(first != second, f"two areas must not share {first}")
    ok(open(os.path.join(d, first)).read().strip() == "area one",
       "the first area's review must survive the second area opening")


@case
def skill_md_stays_inside_the_budget_the_passport_states():
    """The budget is parsed out of AGENTS.md, so the number and the file cannot drift."""
    passport = _io.open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8").read()
    m = re.search(r"budget it in words[^)]*?\(~(\d+)\)", passport)
    ok(m is not None, "AGENTS.md must state the SKILL.md word budget in a parseable form")
    if not m:
        return
    budget = int(m.group(1))
    words = len(_io.open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8").read().split())
    ok(words <= budget, f"SKILL.md is {words} words against the stated budget of {budget}")


def main():
    for fn in CASES:
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001
            FAILURES.append(f"{fn.__name__} raised {e!r}")
    # Every case builds a throwaway repository; without this the suite left ~47 of them in
    # TMPDIR per run, and "the check that matters" punished the maintainer for running it.
    for d in REPOS:
        shutil.rmtree(d, ignore_errors=True)
    if FAILURES:
        print(f"selftest: {len(FAILURES)} failure(s) in {len(CASES)} case(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"selftest: {len(CASES)} case(s) green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
