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
import hashlib
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


def read_text(path):
    """Read a text file as UTF-8, never as whatever the machine's locale happens to be.

    The suite is where the surrogate and non-UTF-8 paths are exercised, so its own I/O has to
    be the one thing that does not depend on the locale: under `LC_ALL=C` the fixtures read
    `state.json` and the ledger through an ASCII codec and fail on content the product
    handles correctly, which reads as a defect in the product.
    """
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_state(d, name="state.json"):
    """The loop state or the project ledger of fixture repo `d`, parsed."""
    return json.loads(read_text(os.path.join(d, ".loop-review", name)))


def write_state(d, obj, name="state.json"):
    write_text(os.path.join(d, ".loop-review", name), json.dumps(obj, indent=2))


def repo(files=None, commit=True):
    d = tempfile.mkdtemp(prefix="loop-selftest-")
    REPOS.append(d)
    sh("git init -q . && git config user.email t@t && git config user.name t", d)
    for name, body in (files or {"a.py": "v1\n"}).items():
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        write_text(path, body)
    if commit:
        sh("git add -A && git commit -qm init", d)
    return d


def write(d, name, body):
    write_text(os.path.join(d, name), body)


def findings(d, slug, text="A. review\nB. understood\nC. trusted\nD. 9.5\n"):
    """Write an area's findings file. `project close` requires it in both modes: closing
    deletes the loop state, so this file is the review's only surviving record.

    The stem carries an unconditional hash tail (two areas can flatten to the same name), so
    the caller names the readable part and this resolves the rest.
    """
    fdir = os.path.join(d, ".loop-review", "findings")
    os.makedirs(fdir, exist_ok=True)
    # Ask the script for the name instead of re-deriving it. The private copy hashed
    # `slug.split("__")`, which is the flattened stem taken apart again: for a nested area
    # like `apps/portal/src/lib` that is four paths where the script hashes one, so the file
    # landed under a name `project close` does not look for — and the case would have failed
    # as "findings file is missing", pointing at the product for a defect in the fixture.
    # It also knew nothing of the stem's length cap or its leading-dot stripping.
    ledger = os.path.join(d, ".loop-review", "project.json")
    area = None
    if os.path.exists(ledger):
        areas = json.loads(read_text(ledger))["areas"]
        area = next((x for x in areas if x["status"] == "in_progress"), None)
    if area is not None:
        name = module().area_slug(area) + ".md"
        named = name.rsplit("-", 1)[0]
        if named != slug:
            raise AssertionError(f"findings(): area in progress is `{named}`, not `{slug}`")
    else:
        hit = [f for f in os.listdir(fdir)
               if f.startswith(slug + "-") and f.endswith(".md") and not f.endswith(".prev.md")]
        if not hit:
            raise AssertionError(f"findings(): no area in progress and no file for `{slug}`")
        name = hit[0]
    write_text(os.path.join(fdir, name), text)


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
    st = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok(st["areas"][1]["status"] == "incomplete" and st["areas"][1]["blockers"],
       f"it must be recorded incomplete with its blocker, got {st['areas'][1]['status']}")


@case
def an_areas_row_is_closed_only_from_the_loop_that_area_opened():
    """Identity is the area the loop was opened for, not the paths it happens to cover.

    Containment cannot answer this. `scope --` legitimately widens a loop past its queued
    paths — that is why the ledger records `reviewed_paths` — so "the loop covers this area"
    also matched a *different* area's loop that merely contained them: a review of `pkg/a +
    pkg/b` was signed into the row of `pkg/a` alone, `reviewed`, with no `--force` asked for.
    """
    d = repo({"pkg/a/f.py": "x\n", "pkg/b/g.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg/a", "pkg/b", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.loads(read_text(sf))
    st["area"] = ["pkg/a", "pkg/b"]                    # a loop opened for a different area
    st["scope"] = ["pkg/a", "pkg/b"]
    write_text(sf, json.dumps(st, indent=2))
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "not area" in r.stderr,
       f"a wider loop from elsewhere must not close this row: rc={r.returncode} {r.stderr.strip()[:160]}")

    # The other side, which is why containment was there in the first place.
    d = repo({"pkg/a/f.py": "x\n", "pkg/b/g.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg/a", "pkg/b", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    run("scope", "--", "pkg/b", cwd=d, check=True)     # this area's own loop, widened
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "reviewed" and area.get("reviewed_paths") == ["pkg/a", "pkg/b"],
       f"a widened loop is still this area's: {area['status']}, {area.get('reviewed_paths')}")


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
    st = json.loads(read_text(sf))
    # A loop opened for another area. `area` is the identity — forging `scope` alone would
    # only look like this area's own loop after a legitimate `scope --` widening.
    st["area"] = st["scope"] = ["b"]
    write_text(sf, json.dumps(st, indent=2))

    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "not area" in r.stderr,
       f"close must refuse to sign another loop into this area, got rc={r.returncode}")
    ok("project close --force" in r.stderr and "reset --force" not in r.stderr,
       f"the refusal must name a route the CLI has: {r.stderr.strip()[:160]}")

    r = run("project", "close", "--force", cwd=d)
    ok(r.returncode == 0, f"--force must settle the area, got rc={r.returncode}: {r.stderr.strip()[:160]}")
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
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
def init_refuses_over_an_area_whose_loop_state_is_gone():
    """The mid-area guard on `init` was asked only when `state.json` existed.

    Once the area's loop state was gone — a crash, or `reset` clearing a foreign loop, which
    it is allowed to do — `init --mode project -- a b` went through without `--force`, and
    its wider scope was then read by `project close` as this area's own loop: the row closed
    `reviewed` from a review nobody opened for it. Refuse on the ledger alone, and do not
    let an unstamped loop pass for the area's even if one gets there by other means.
    """
    d = repo({"a/x.py": "1\n", "b/y.py": "2\n"})
    run("project", "init", "--report-only", "--", "a", "b", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    sf = os.path.join(d, ".loop-review", "state.json")
    os.remove(sf)                                      # the area's loop state is lost

    r = run("init", "--mode", "project", "--", "a", "b", cwd=d)
    ok(r.returncode != 0 and "in progress and its loop state is gone" in r.stderr
       and "project close --force" in r.stderr,
       f"init must refuse over an in-progress area with no loop: rc={r.returncode} "
       f"{r.stderr.strip()[:200]}")
    ok(not os.path.exists(sf), "the refusal must not leave a loop behind")

    # A loop `init` wrote anyway (an older script, a hand edit) carries no area stamp.
    proj = read_state(d, "project.json")
    write_state(d, {"areas": []}, "project.json")
    run("init", "--mode", "project", "--", "a", "b", cwd=d, check=True)   # lists `a`, and more
    write_state(d, proj, "project.json")
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "not area" in r.stderr,
       f"an unstamped wider loop must not close the area's row: rc={r.returncode} "
       f"{r.stderr.strip()[:160]}")
    ok(read_state(d, "project.json")["areas"][0]["status"] == "in_progress",
       "the row must stay open after the refusal")
    r = run("reset", cwd=d)
    ok(r.returncode == 0 and not os.path.exists(sf),
       f"and reset must treat it as foreign, not as the area's own: rc={r.returncode} "
       f"{r.stderr.strip()[:160]}")


@case
def a_tracked_file_deleted_on_disk_is_warned_about_only_where_it_is_invisible():
    """`git ls-files --cached` lists a file `rm` deleted without `git rm`.

    In project mode the fingerprint hashes the disk, so that path is invisible and must be
    warned about — it was not: the index said "seen". In changes mode the scoped diff carries
    the deletion, which may be the very change under review, so the warning that it
    "contributes nothing to the fingerprint" would be false — and it was printed there.
    """
    d = repo({"pkg/a.py": "x\n", "gone.py": "y\n"})
    os.remove(os.path.join(d, "gone.py"))
    r = run("project", "init", "--allow-empty", "--", "pkg", "gone.py", cwd=d)
    ok("`gone.py` does not exist on disk" in r.stderr,
       f"project mode must warn that the deleted path is invisible: {r.stderr.strip()[:200]}")

    d = repo({"pkg/a.py": "x\n", "gone.py": "y\n"})
    os.remove(os.path.join(d, "gone.py"))                     # the task deletes a file
    r = run("init", "--", "gone.py", cwd=d, check=True)
    ok("gone.py" not in r.stderr,
       f"changes mode: the deletion is in the diff, so no warning: {r.stderr.strip()[:200]}")
    before = read_state(d)["fingerprint_current"]
    write(d, "gone.py", "y\n")                                # restoring it undoes the change
    ok(run("fingerprint", cwd=d, check=True).stdout.strip() != before,
       "and the deletion is in fact what the fingerprint hashes")

    # Every other way a deletion reaches the diff while the index no longer lists the path.
    # Asking the index (`--cached`) warned on all of them, and called "seen" a file added to
    # the index and then removed, which no diff carries.
    def changes_init(setup, *init_args):
        d = repo({"pkg/a.py": "x\n", "gone.py": "y\n"})
        setup(d)
        return run("init", *init_args, "--", "gone.py", cwd=d).stderr, d

    for label, setup, args in (
            ("git rm", lambda d: sh("git rm -q gone.py", d), ()),
            ("git mv", lambda d: sh("git mv gone.py moved.py", d), ()),
            ("committed since --base",
             lambda d: sh("git rm -q gone.py && git commit -qm drop", d), ("--base", "HEAD~1")),
            ("a rename committed since --base",
             lambda d: sh("git mv gone.py moved.py && git commit -qm mv", d), ("--base", "HEAD~1"))):
        err, _ = changes_init(setup, *args)
        ok("gone.py" not in err, f"{label}: the deletion is in the diff, so no warning: {err.strip()[:200]}")

    d = repo({"pkg/a.py": "x\n", "gone.py": "y\n"})              # `scope --` asks the same
    write(d, "pkg/a.py", "x2\n")
    run("init", "--", "pkg", cwd=d, check=True)
    sh("git rm -q gone.py", d)
    r = run("scope", "--", "gone.py", cwd=d, check=True)
    ok("warning" not in r.stderr,
       f"scope --: a deleted path in the diff is not unseen: {r.stderr.strip()[:200]}")

    d = repo({"pkg/a.py": "x\n"})                                  # added, then removed
    write(d, "pkg/a.py", "x2\n")
    write(d, "ghost.py", "g\n")
    sh("git add ghost.py", d)
    os.remove(os.path.join(d, "ghost.py"))
    r = run("init", "--", "pkg", "ghost.py", cwd=d, check=True)
    ok("`ghost.py` does not exist on disk" in r.stderr,
       f"a path only the index lists is in no diff and must be warned about: {r.stderr.strip()[:200]}")

    if os.geteuid() != 0:                                           # root reads anything
        d = repo({"pkg/a.py": "x\n"})
        write(d, "pkg/a.py", "x2\n")
        lock = os.path.join(d, "lock.py")
        write_text(lock, "s\n")
        os.chmod(lock, 0)
        try:
            r = run("init", "--", "pkg", "lock.py", cwd=d)
            ok("`lock.py` cannot be read" in r.stderr,
               f"an unreadable file in changes mode is still warned about: {r.stderr.strip()[:200]}")
        finally:
            os.chmod(lock, 0o644)

        # A *tracked* unreadable file: `init` stops at `git diff` before it would warn, so
        # the answer is asked of `unseen_paths` itself. The index lists it, and reading
        # "listed" as "seen" hid it; an unreadable path is reported before any such check.
        d = repo({"pkg/a.py": "x\n", "lock.py": "s\n"})
        lock = os.path.join(d, "lock.py")
        os.chmod(lock, 0)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            got = dict(module().unseen_paths(["lock.py"], "changes"))
        finally:
            os.chdir(cwd)
            os.chmod(lock, 0o644)
        ok("cannot be read" in got.get("lock.py", ""),
           f"a tracked unreadable file is not seen because git lists it: {got}")


@case
def an_area_is_what_can_be_read_from_disk_not_what_git_lists():
    """Emptiness asked git's index, and the index is not the disk.

    The workflow never runs `git rm`, so an area deleted with `rm -rf` stayed listed: it
    queued, opened a loop over nothing and closed `accepted`. A `chmod 000` file was hashed
    as a marker and reached no gate, so an area closed `reviewed` with a file nobody opened.
    """
    def rm_area(d):
        shutil.rmtree(os.path.join(d, "pkg"))                      # no `git rm`

    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    rm_area(d)
    r = run("project", "init", "--", "pkg", "lib", cwd=d)
    ok(r.returncode == 2 and "match no file" in r.stderr and "pkg" in r.stderr,
       f"an area deleted on disk must be refused at queue time: rc={r.returncode} "
       f"{r.stderr.strip()[:200]}")

    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    run("project", "init", "--", "pkg", "lib", cwd=d, check=True)
    rm_area(d)                                                     # deleted after queueing
    r = run("project", "next", cwd=d)
    row = read_state(d, "project.json")["areas"][0]
    ok(r.returncode == 2 and row["status"] == "incomplete",
       f"project next must settle it, not open a loop: rc={r.returncode} {row['status']}")

    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    findings(d, "pkg", "A. clean\n")
    rm_area(d)                                                     # deleted after the pass
    run("project", "close", cwd=d, check=True)
    row = read_state(d, "project.json")["areas"][0]
    ok(row["status"] == "incomplete" and any("files are gone" in b for b in row["blockers"]),
       f"an area gone from disk after its pass describes nothing: {row}")

    if os.geteuid() == 0:                                          # root reads anything
        return
    d = repo({"pkg/a.py": "x\n", "pkg/secret.py": "s\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    locked = os.path.join(d, "pkg", "secret.py")
    os.chmod(locked, 0)
    try:
        pass_through(d, "9.5", "0", "trusted")
        findings(d, "pkg", "A. clean\n")
        run("project", "close", cwd=d, check=True)
        row = read_state(d, "project.json")["areas"][0]
        ok(row["status"] == "incomplete"
           and any("unreadable" in b and "secret.py" in b for b in row["blockers"]),
           f"a file no reviewer could open must keep the area from closing reviewed: {row}")
    finally:
        os.chmod(locked, 0o644)


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
    st = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
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
    write_text(os.path.join(d, ffile), "clean\n")
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
def a_review_of_a_state_the_files_moved_past_is_not_recorded():
    """SKILL.md step 3 says so, and `pass-record` never checked.

    An edit while the reviewer read — the user, a formatter, a checkout — left the open pass
    bound to a state that no longer exists, and `pass-record` filed its verdict anyway, with
    `fingerprint_current` stale behind it. The refusal names `pass-abort`, and that must be
    a way out in exactly this state.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    out = run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    opened = read_state(d)["passes"][-1]["fingerprint"]
    write(d, "src/a.py", "3\n")                                 # moved mid-review
    r = run("pass-record", "--score", "9", "--findings", "0", "--understood",
            "--test-evidence", "trusted", cwd=d)
    st = read_state(d)
    ok(r.returncode != 0 and "moved while pass 1 was being reviewed" in r.stderr
       and "pass-abort" in r.stderr,
       f"a review of a vanished state must be refused toward pass-abort: rc={r.returncode} "
       f"{r.stderr.strip()[:200]}")
    ok(st["passes"][-1]["result"] is None and st["fingerprint_current"] != opened,
       "nothing recorded, and the current fingerprint brought up to date")
    ok(run("pass-abort", "--reason", "scope moved mid-review", cwd=d).returncode == 0,
       "the named way out works in that state")
    run("validate", "--", "true", cwd=d)
    ok(run("pass-start", cwd=d).returncode == 0, "and a fresh pass opens on what is there now")

    d = repo({"src/a.py": "1\n", "lib/b.py": "1\n"})          # scope -- before any review
    write(d, "src/a.py", "2\n")
    write(d, "lib/b.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    out = run("scope", "--", "lib", cwd=d, check=True).stdout
    ok("stale" not in out and "first pass" in out,
       f"with no review yet, nothing is stale: {out.strip()[:200]}")


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
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
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
def a_nested_area_closes_on_the_findings_file_the_script_names():
    """The deep-path case the fixtures never reached, product and helper together.

    `findings()` used to rebuild the filename from the flattened stem, splitting a nested
    path back into four; the name it wrote was not the one `project close` looks for. No case
    caught it because every fixture area was a single flat directory — the shape that makes
    the two derivations agree by accident.
    """
    d = repo({"apps/portal/src/lib/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "apps/portal/src/lib", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    printed = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9.5", "0", "trusted")
    findings(d, "apps__portal__src__lib", "A. no actionable findings\n")
    written = [f for f in os.listdir(os.path.join(d, ".loop-review", "findings"))
               if f.endswith(".md")]
    ok(written == [os.path.basename(printed)],
       f"the helper must write the file the script named: {written} vs {printed}")
    r = run("project", "close", cwd=d)
    ok(r.returncode == 0, f"and the close must find it: {r.stderr.strip()[:160]}")
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "reviewed" and area["findings_file"],
       f"the ledger must record the surviving report: {area['status']}, {area['findings_file']}")


@case
def the_ledger_reports_the_last_pass_and_the_history_apart():
    """Summed over passes, the ledger's findings contradicted the outcome it sat beside.

    Pass 1 named two findings, one was resolved, the fix batch moved the area, and pass 2
    came back clean: `accept` judged pass 2, the area closed `accepted` — and the row read
    `findings 1/2`, an accepted area with an open finding. The last pass and the history
    are two answers; each gets its own field.
    """
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "2", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("resolve", "--fixed", "1", cwd=d, check=True)
    write(d, "pkg/a.py", "y\n")                                 # the fix batch
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    findings(d, "pkg", "A. clean on the second pass\n")
    run("project", "close", cwd=d, check=True)
    row = read_state(d, "project.json")["areas"][0]
    ok(row["status"] == "accepted" and (row["findings"], row["resolved"]) == (0, 0),
       f"findings/resolved are the pass acceptance judged: {row}")
    ok((row.get("total_findings"), row.get("total_resolved")) == (2, 1),
       f"and the history is kept beside them: {row}")
    line = next(l for l in run("project", "status", cwd=d, check=True).stdout.splitlines()
                if l.startswith("  ["))
    ok("findings 0/0" in line and "over all passes 1/2" in line,
       f"the text ledger shows both, labelled: {line}")


@case
def the_ledger_keeps_every_metric_the_pass_recorded():
    """`project close` deletes state.json, so a metric the row does not copy is gone.

    The area prompt asks for a test quality score and SKILL.md's report quotes it from
    `project status --json`, but the row was built by hand in two places and neither copied
    `test_score` or `note`. The summary `pass-record` and `amend` print left them out too,
    and `amend --score` skipped the calibration note `pass-record` gives.
    """
    fields = set(getattr(module(), "LEDGER_METRICS", ()))
    recorded = {"score", "test_score", "test_evidence", "understood", "note"}
    ok(recorded <= fields,
       f"every verdict and metric a pass records must be a ledger field: "
       f"{sorted(recorded - fields)}")

    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg", "lib", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    out = run("pass-record", "--findings", "0", "--understood", "--test-evidence", "trusted",
              "--test-score", "7", "--note", "tests mock the network", cwd=d, check=True).stdout
    ok("test score 7" in out and "tests mock the network" in out,
       f"pass-record must echo what it recorded: {out.strip()[:200]}")
    out = run("amend", "--score", "9", cwd=d, check=True).stdout
    ok("test score 7" in out and "calibration noise" in out,
       f"amend must print the same summary and the same note as pass-record: {out.strip()[:300]}")
    findings(d, "pkg", "A. clean\nC. trusted, 7\nD. 9\n")
    run("project", "close", cwd=d, check=True)
    row = read_state(d, "project.json")["areas"][0]
    ok(row.get("test_score") == 7 and row.get("note") == "tests mock the network",
       f"the ledger must keep the test score and the note: {row}")
    ok("test-score 7" in run("project", "status", cwd=d, check=True).stdout,
       "and the text ledger must show the test score")

    run("project", "next", cwd=d, check=True)                      # `lib`: its state is lost
    os.remove(os.path.join(d, ".loop-review", "state.json"))
    run("project", "close", "--force", cwd=d, check=True)
    rows = read_state(d, "project.json")["areas"]
    ok(set(rows[0]) == set(rows[1]) and recorded <= set(rows[1]),
       f"a settled row carries the same fields: {sorted(set(rows[0]) ^ set(rows[1]))}")


@case
def widening_the_scope_always_asks_for_a_new_review():
    """The fingerprint saw a widening only when the added path carried changes.

    `scope -- b.py` for a tracked file with no diff left the last pass current: `accept`
    signed it off for a path its reviewer was never told about, and `pass-start` refused
    the new pass as unchanged. A path under an already scoped directory was taken as a
    widening too. And the ledger wrote `reviewed_paths` only for widened areas.
    """
    d = repo({"a.py": "1\n", "b.py": "1\n", "pkg/sub/c.py": "1\n"})
    write(d, "a.py", "2\n")
    run("init", "--", "a.py", "pkg", cwd=d, check=True)
    pass_through(d)
    ok(run("accept", cwd=d).returncode == 0, "the fixture accepts before the widening")
    r = run("scope", "--", "pkg/sub/c.py", cwd=d)
    ok(r.returncode != 0 and "already in scope" in r.stderr and read_state(d)["scope"] == ["a.py", "pkg"],
       f"a path under a scoped directory is already covered: {r.stderr.strip()[:160]}")
    before = read_state(d)["fingerprint_current"]
    run("scope", "--", "b.py", cwd=d, check=True)                  # no diff: fingerprint stays
    ok(read_state(d)["fingerprint_current"] == before, "the fixture's widening moves no hash")
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "scope widened since pass 1" in r.stdout,
       f"the pass was told about other paths: {r.stdout.strip()[:200]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0, f"and a new pass over the wider scope opens: {r.stderr.strip()[:200]}")

    d = repo({"a/x.py": "1\n", "gone/y.py": "2\n"})
    run("project", "init", "--report-only", "--", "a", "gone", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d)
    findings(d, "a", "A. clean\n")
    run("project", "close", cwd=d, check=True)
    sh("git rm -q -r gone && git commit -qm drop", d)
    run("project", "next", cwd=d)                                  # settles `gone`
    rows = read_state(d, "project.json")["areas"]
    ok(rows[0].get("reviewed_paths") == ["a"] and "reviewed_paths" in rows[1]
       and rows[1]["reviewed_paths"] is None,
       f"every closed row carries reviewed_paths: {[r.get('reviewed_paths', 'MISSING') for r in rows]}")


@case
def every_closed_ledger_row_has_the_same_shape():
    """`project.json` is the deliverable of a project review, so its rows are a schema.

    Three paths settle an area that produced no usable review, and each wrote the row out by
    hand. One drifted: the lost-state branch left `test_evidence` and `understood` unset, so
    a consumer indexing rows by key hit a KeyError on exactly the areas that had gone wrong,
    and the ledger reported two different shapes for one outcome.
    """
    d = repo({"ok/f.py": "x\n", "gone/f.py": "x\n", "lost/f.py": "x\n", "foreign/f.py": "x\n"})
    run("project", "init", "--report-only", "--", "ok", "gone", "lost", "foreign", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout          # 1. a clean review
    stem = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)

    sh("git rm -q -r gone && git commit -qm drop", d)               # 2. files gone
    run("project", "next", cwd=d)

    run("project", "next", cwd=d, check=True)                       # 3. loop state lost
    os.remove(os.path.join(d, ".loop-review", "state.json"))
    run("project", "close", "--force", cwd=d, check=True)

    run("project", "next", cwd=d, check=True)                       # 4. a foreign loop
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.loads(read_text(sf))
    st["area"] = st["scope"] = ["ok"]                  # a loop belonging to another area
    write_text(sf, json.dumps(st, indent=2))
    run("project", "close", "--force", cwd=d, check=True)

    rows = [a for a in json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"]
            if a["status"] not in ("pending", "in_progress")]
    ok(len(rows) == 4, f"the fixture must exercise all four settle paths, got {len(rows)}")
    shapes = {tuple(sorted(a)) for a in rows}
    ok(len(shapes) == 1,
       "closed rows differ in shape: " + "; ".join(str(set(a) ^ set(rows[0])) for a in rows
                                                   if set(a) != set(rows[0])))
    for key in ("test_evidence", "understood", "findings_file", "passes", "score"):
        ok(all(key in a for a in rows), f"every closed row must carry `{key}`")


@case
def the_ledger_never_prints_a_metric_the_area_does_not_have():
    """`project status` is the deliverable of a project review — it has to read as a report.

    The metrics were printed as one block, gated on the pass count alone, so an area settled
    before it ever opened a pass — an empty or renamed one, which `project next` closes with
    `passes: 0` — rendered as `passes 0  findings None/None  score None`, three fields that do
    not exist dressed as results.
    """
    d = repo({"a/x.py": "1\n", "gone/y.py": "2\n"})
    run("project", "init", "--report-only", "--", "a", "gone", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)
    sh("git rm -q -r gone && git commit -qm drop", d)
    run("project", "next", cwd=d)                          # settles `gone` incomplete
    out = run("project", "status", cwd=d, check=True).stdout
    ok("None" not in out, f"no metric may print as None: {out}")
    gone = next(l for l in out.splitlines() if "gone" in l)
    ok("findings" not in gone and "score" not in gone,
       f"an area with no review must not show findings or a score: {gone}")
    rows = [l for l in out.splitlines() if l.startswith("  [")]
    reviewed = next(l for l in rows if l.split("]", 1)[1].strip().startswith("a"))
    ok("passes 1" in reviewed and "findings 0/0" in reviewed and "score 9.5" in reviewed,
       f"and a reviewed area must still show all of them: {reviewed}")


@case
def project_mode_refuses_a_brief_and_a_scope_note_by_name():
    """Both are changes-mode only, and both commands must say so about the flag given.

    `brief` refused `--scope-note` with a message about `--task-brief`, so an operator could
    not tell which was disallowed — while `init --mode project --scope-note` accepted the
    same note, recorded it, and had `status` tell the agent to "repeat it verbatim in the
    reviewer's hunk fields". `reviewer-prompt-area.md` has no such field: an area is in scope
    whole. One command was refusing what the other invited across the isolation boundary.
    """
    d = repo({"pkg/a.py": "x\n"})
    r = run("init", "--mode", "project", "--scope-note", "only the parser hunks", "--", "pkg", cwd=d)
    ok(r.returncode != 0 and "--scope-note" in r.stderr,
       f"init must refuse a scope note for an inherited area: rc={r.returncode} {r.stderr.strip()[:160]}")
    run("init", "--mode", "project", "--", "pkg", cwd=d, check=True)
    r = run("brief", "--scope-note", "only the parser hunks", cwd=d)
    ok(r.returncode != 0 and "--scope-note" in r.stderr,
       f"brief must name the flag it refuses: {r.stderr.strip()[:160]}")
    r = run("brief", "--task-brief", "ship it", cwd=d)
    ok(r.returncode != 0 and "--task-brief" in r.stderr,
       f"and still refuse a brief: {r.stderr.strip()[:160]}")
    # A refusal names what was typed. `--task-brief-file` was folded into `--task-brief` at
    # both sites, so an operator who supplied a file was answered about a flag they had not
    # used — and could not tell whether their own was allowed or had simply been ignored.
    write_text(os.path.join(d, "brief.md"), "requirements\n")
    r = run("brief", "--task-brief-file", "brief.md", cwd=d)
    ok(r.returncode != 0 and "--task-brief-file" in r.stderr,
       f"brief must name the file flag it was given: {r.stderr.strip()[:160]}")
    r = run("init", "--force", "--mode", "project", "--task-brief-file", "brief.md",
            "--", "pkg", cwd=d)
    ok(r.returncode != 0 and "--task-brief-file" in r.stderr,
       f"init must name it too: {r.stderr.strip()[:160]}")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    ok(st.get("scope_note") is None, f"nothing may have been recorded: {st.get('scope_note')!r}")

    run("reset", cwd=d, check=True)                       # changes mode keeps both
    write(d, "pkg/a.py", "y\n")
    run("init", "--", "pkg", cwd=d, check=True)
    out = run("brief", "--scope-note", "only the parser hunks", cwd=d, check=True).stdout
    ok("only the parser hunks" in out, f"changes mode must still take a scope note: {out[:160]}")


@case
def a_run_that_moved_the_scope_cannot_make_a_later_failure_read_as_regressed():
    """A voided run is voided for every reader, provenance included.

    `validate` marks a command that edited the files while checking them: it is evidence for
    neither the state before nor the state after, and every gate skips it. `red_provenance`
    did not, so one green-but-moving run was enough to stamp a later failure `[regressed]` —
    telling a fresh reviewer that the change under review broke a check, on the strength of a
    result the loop had already declared void.
    """
    d = repo({"pkg/a.py": "x\n"})
    # Green on the first run and editing the scope as it goes; red on every run after.
    check = "if [ -f pkg/marker ]; then exit 1; fi; touch pkg/marker; exit 0"
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    r = run("validate", "--", check, cwd=d)
    ok("changed the scope" in r.stderr, f"the fixture must produce a moved-scope run: {r.stderr[:160]}")
    run("validate", "--", check, cwd=d)                    # now red, scope settled
    out = run("pass-start", cwd=d, check=True).stdout + run("status", cwd=d, check=True).stdout
    # Not `[regressed]` — the voided green proves nothing. (Nor `[inherited]`: the check's
    # own marker moved the area, so its red run is not on the area as opened.)
    ok("[regressed]" not in out and "-> exit 1  [" in out,
       f"a moved-scope run must not count as this check having been green: {out}")


@case
def a_red_check_is_labelled_only_where_the_label_is_true():
    """`[inherited]` claims the area as opened already failed the check.

    It was printed in task mode, where the reviewer prompt asks for bare lines and there is
    no inherited area; on a 126/127, which is evidence about nothing; and on a check first
    run after the area had been edited, which nothing showed had ever failed before.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "false", cwd=d)
    out = run("status", cwd=d, check=True).stdout
    ok("-> exit 1\n" in out and "[inherited]" not in out and "[regressed]" not in out,
       f"task mode prints bare lines: {out.strip()[-200:]}")

    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "nosuchtool --x", cwd=d)               # 127 on the area as opened
    run("validate", "--", "false", cwd=d)                        # red on the area as opened
    out = run("status", cwd=d, check=True).stdout
    ok("nosuchtool --x  -> exit 127  [never ran]" in out and "false  -> exit 1  [inherited]" in out,
       f"a check that never started is not inherited; one red as opened is: {out.strip()[-240:]}")
    write(d, "pkg/a.py", "y\n")                                 # the area changes
    run("validate", "--", "test -z yes", cwd=d)                  # first run only now, red
    out = run("status", cwd=d, check=True).stdout
    ok("test -z yes  -> exit 1  [first run after the area changed]" in out,
       f"a check first run after the change is not called inherited: {out.strip()[-240:]}")

    d = repo({"pkg/a.py": "x\n"})                                 # opened by init, not project next
    run("init", "--mode", "project", "--", "pkg", cwd=d, check=True)
    write(d, "pkg/a.py", "y\n")
    run("validate", "--", "false", cwd=d)
    out = run("status", cwd=d, check=True).stdout
    ok("false  -> exit 1  [first run after the area changed]" in out,
       f"`init --mode project` stamps the opened area too: {out.strip()[-240:]}")


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
    write(d, "src/a.py", "v1\nv2 fixed\n")               # the fix batch, then step 5
    run("resolve", "--fixed", "1", "--withdrawn", "1", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode != 0,
       "a fix batch makes the review stale — acceptance waits for the pass that saw it")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
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
    st = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok([x["status"] for x in st["areas"]] == ["accepted", "incomplete"],
       f"ledger must record both outcomes, got {[x['status'] for x in st['areas']]}")


@case
def the_loops_own_state_is_never_a_scope_path():
    """`.loop-review` named as a path was kept in the scope and printed into the reviewer's
    prompt — which the prompts and project-mode.md forbid — while the fingerprint excluded
    it, so it scoped nothing; and on a first run it drew a false "does not exist" warning.
    `.` still covers a repository whose state lives inside it (the next case).
    """
    d = repo({"a.py": "1\n", "pkg/b.py": "2\n"})
    write(d, "a.py", "2\n")
    sd = os.path.join(d, ".loop-review")
    for args in (("init", "--", "a.py", ".loop-review"),
                 ("init", "--", "a.py", ".loop-review/findings"),
                 ("project", "init", "--", "pkg", "+", ".loop-review")):
        r = run(*args, cwd=d)
        ok(r.returncode != 0 and "the loop's own state" in r.stderr and "does not exist" not in r.stderr,
           f"`{' '.join(args)}` must be refused by name: rc={r.returncode} {r.stderr.strip()[:200]}")
        ok(not os.path.exists(os.path.join(sd, "state.json"))
           and not os.path.exists(os.path.join(sd, "project.json")),
           f"`{' '.join(args)}`: and nothing may be written")
    run("init", "--", "a.py", cwd=d, check=True)
    r = run("scope", "--", ".loop-review", cwd=d)
    ok(r.returncode != 0 and "the loop's own state" in r.stderr and read_state(d)["scope"] == ["a.py"],
       f"`scope -- .loop-review` must not widen the loop: {read_state(d)['scope']} {r.stderr.strip()[:160]}")


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
def a_scope_path_is_a_name_not_a_pattern():
    """After `--`, git reads arguments as pathspecs, and a file name can look like one.

    `:foo` was parsed as pathspec magic, so `git` died and `init` with it; `a[1].py` was a
    glob that also matched `a1.py`, so a file nobody scoped moved the fingerprint — and was
    in the reviewed state. Both modes, because both hash through the same pathspecs.
    """
    files = {":foo": "1\n", "a[1].py": "1\n", "a1.py": "1\n"}
    d = repo(files)
    write(d, ":foo", "2\n")
    write(d, "a[1].py", "2\n")
    write(d, "a1.py", "2\n")
    r = run("init", "--", ":foo", cwd=d)                    # alone: nothing else to hide it
    ok(r.returncode == 0, f"`:foo` alone is a changed file, not an empty scope: "
                          f"rc={r.returncode} {r.stderr.strip()[-160:]}")
    if r.returncode == 0:
        before = read_state(d)["fingerprint_current"]
        write(d, ":foo", "3\n")
        ok(run("fingerprint", cwd=d, check=True).stdout.strip() != before,
           "and editing it moves the fingerprint")
        write(d, ":foo", "2\n")
    r = run("init", "--force", "--", ":foo", "a[1].py", cwd=d)
    ok(r.returncode == 0 and read_state(d)["scope"] == [":foo", "a[1].py"],
       f"a path starting with `:` is a file name: rc={r.returncode} {r.stderr.strip()[-160:]}")
    if r.returncode == 0:
        before = read_state(d)["fingerprint_current"]
        write(d, "a1.py", "3\n")                                   # outside the scope
        ok(run("fingerprint", cwd=d, check=True).stdout.strip() == before,
           "a file a glob would match, but nobody scoped, must not move the fingerprint")

    d = repo(files)
    run("project", "init", "--", "a[1].py", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    before = read_state(d)["fingerprint_current"]
    write(d, "a1.py", "2\n")
    ok(run("fingerprint", cwd=d, check=True).stdout.strip() == before,
       "project mode: an area named like a glob is that one file")


@case
def a_link_in_scope_is_the_link_not_its_target():
    """A tracked symlink — this repository's own `CLAUDE.md -> AGENTS.md` — is an entry of
    its own, both when it is named and when it is hashed.

    A path given through a symlinked directory fell back to `realpath`, which also followed
    the last component: `init -- /via/link/CLAUDE.md` scoped `AGENTS.md`, the dedupe merged
    the two, and the link was out of scope; a link pointing outside the repository was
    refused as "outside". And the hash read through links, so an area holding a link moved
    when the *target* — outside the area — was edited, while swapping the link for a copy of
    the same bytes moved nothing.
    """
    d = repo({"AGENTS.md": "passport\n", "area/x.py": "x\n"})
    sh("ln -s AGENTS.md CLAUDE.md && ln -s ../AGENTS.md area/LINK.md && ln -s /etc/hostname OUT.md"
       " && git add -A && git commit -qm links", d)
    write(d, "AGENTS.md", "passport v2\n")
    via = tempfile.mkdtemp(prefix="loop-selftest-via-")
    REPOS.append(via)
    hop = os.path.join(via, "repo")
    os.symlink(d, hop)
    r = run("init", "--", os.path.join(hop, "CLAUDE.md"), os.path.join(hop, "AGENTS.md"),
            os.path.join(hop, "OUT.md"), cwd=d)
    ok(r.returncode == 0 and read_state(d)["scope"] == ["CLAUDE.md", "AGENTS.md", "OUT.md"],
       f"a path through a symlinked directory names the link, not its target: rc={r.returncode} "
       f"{read_state(d).get('scope') if r.returncode == 0 else r.stderr.strip()[:160]}")
    # The link *into* the repository is an entry point, not an entry: `/via/repo` is the
    # root, and `/via/repo/area` reached the same way is `area`.
    r = run("init", "--force", "--mode", "project", "--", hop, os.path.join(hop, "area"), cwd=d)
    ok(r.returncode == 0 and read_state(d)["scope"] == [".", "area"],
       f"a link to the repository root resolves to `.`: rc={r.returncode} "
       f"{read_state(d).get('scope') if r.returncode == 0 else r.stderr.strip()[:160]}")

    m = module()
    cwd = os.getcwd()
    os.chdir(d)
    try:
        before = m.fingerprint(["area"], "project")
        write_text("AGENTS.md", "passport v3\n")                 # the target, outside the area
        ok(m.fingerprint(["area"], "project") == before,
           "editing a link's target outside the area must not move the area's hash")
        before = m.fingerprint(["area"], "project")
        os.remove("area/LINK.md")
        write_text("area/LINK.md", "passport v3\n")               # a copy of the same bytes
        ok(m.fingerprint(["area"], "project") != before,
           "replacing the link with a copy must move it")
        os.remove("area/LINK.md")
        os.symlink("../AGENTS.md", "area/LINK.md")
        os.symlink("../AGENTS.md", "area/NEW.md")                  # untracked, changes mode
        before = m.fingerprint(["area"], "changes")
        write_text("AGENTS.md", "passport v4\n")
        ok(m.fingerprint(["area"], "changes") == before,
           "an untracked link hashes as itself in changes mode too")
    finally:
        os.chdir(cwd)


@case
def a_mode_or_link_change_moves_the_fingerprint():
    """The fingerprint hashed bytes only, and a file's state is more than its bytes.

    A finding "this script is not executable" was fixed with `chmod +x`: no hash moved, so
    `accept` said the recorded fix was absent and `pass-start` said nothing had changed — no
    way forward. A symlink was hashed as its target's content, so retargeting it moved nothing.
    """
    def fixed_by_chmod(d):
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "8", "--findings", "1", "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        os.chmod(os.path.join(d, "tool.sh"), 0o755)              # the fix
        run("resolve", "--fixed", "1", cwd=d, check=True)
        r = run("accept", cwd=d)
        ok("recorded fixes are not present" not in r.stdout,
           f"a chmod fix must be on the reviewed state's record: {r.stdout.strip()[:200]}")
        run("validate", "--", "true", cwd=d)
        r = run("pass-start", cwd=d)
        ok(r.returncode == 0, f"and the next pass must open: {r.stderr.strip()[:200]}")

    d = repo({"tool.sh": "#!/bin/sh\n", "pkg/a.py": "x\n"})          # project mode, tracked
    run("project", "init", "--", "tool.sh", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    fixed_by_chmod(d)

    d = repo({"a.py": "1\n"})                                      # changes mode, untracked
    write(d, "tool.sh", "#!/bin/sh\n")
    run("init", "--", "tool.sh", cwd=d, check=True)
    fixed_by_chmod(d)

    m = module()
    d = repo({"a.py": "1\n", "b.py": "1\n"})                        # same bytes, two targets
    os.symlink("a.py", os.path.join(d, "link"))
    cwd = os.getcwd()
    os.chdir(d)
    try:
        before = m.fingerprint(["link"], "project")
        os.remove("link")
        os.symlink("b.py", "link")
        ok(m.fingerprint(["link"], "project") != before, "retargeting a link must move the hash")

        # A plain file hashes exactly as it did before kinds existed, so a loop in flight
        # is not made stale by the format change alone.
        h = hashlib.sha256()
        for b in (b"a.py", b"1\n"):
            h.update(str(len(b)).encode() + b"\0" + b)
        ok(m.fingerprint(["a.py"], "project") == h.hexdigest()[:16],
           "a plain file's hash must be unchanged by the new field")
    finally:
        os.chdir(cwd)


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
    state = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    printed = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(printed == state["fingerprint_current"],
       f"project mode: `fingerprint` printed {printed}, gates use {state['fingerprint_current']}")
    run("reset", cwd=d, check=True)
    write(d, "src/a.py", "v2\n")
    sh("git commit -qam work", d)
    run("init", "--base", "HEAD~1", "--", "src", cwd=d, check=True)
    state = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
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
def the_abort_note_agrees_with_what_pass_start_will_actually_do():
    """The hint is computed by the gate, not by re-deriving one of its conditions.

    `pass-abort` compared fingerprints and promised the next `pass-start` would be granted.
    True only when no *recorded* pass sits on that fingerprint: open a pass with `--force` on
    an unchanged scope, abort it, and the gate refuses the pass the note had just promised —
    leaving the operator to conclude the abort had cost them something it had not.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    err = run("pass-abort", "--reason", "unusable reviewer", cwd=d, check=True).stderr
    ok("is granted" in err, f"with nothing recorded the note must promise the pass: {err.strip()[:200]}")
    ok(run("pass-start", cwd=d).returncode == 0, "and the gate must grant it")

    run("pass-record", "--score", "8", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("pass-start", "--force", cwd=d, check=True)     # forced onto the same fingerprint
    err = run("pass-abort", "--reason", "unusable again", cwd=d, check=True).stderr
    ok("will refuse" in err and "unchanged since the last pass" in err,
       f"with a recorded pass on this fingerprint the note must say so: {err.strip()[:200]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "unchanged" in r.stderr,
       f"and the gate must refuse for that reason: rc={r.returncode} {r.stderr.strip()[:160]}")


@case
def the_abort_note_quotes_the_gate_and_never_counts_below_zero():
    """The note explained every refusal as "the scope has not moved, and the abort is not
    what stands in the way" — at the pass limit, the abort is exactly what used the last
    pass, and no edit unblocks it. And passes forced past the limit left "-1 of N".
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--max-passes", "1", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    r = run("pass-abort", "--reason", "unusable reviewer", cwd=d, check=True)
    ok("0 of 1 left" in r.stdout and "pass limit 1 reached" in r.stderr
       and "has not moved" not in r.stderr and "not what stands in the way" not in r.stderr,
       f"at the limit the note names the limit, not the scope: {(r.stdout + r.stderr).strip()[:300]}")
    gate = run("pass-start", cwd=d).stderr
    ok("pass limit 1 reached" in gate, "and that is what the gate says")

    run("pass-start", "--force", cwd=d, check=True)               # past the limit, by a human
    run("pass-abort", "--reason", "unusable again", cwd=d, check=True)
    run("pass-start", "--force", cwd=d, check=True)
    r = run("pass-abort", "--reason", "and again", cwd=d, check=True)
    left = re.search(r"(-?\d+) of 1 left", r.stdout)
    ok(left is not None and int(left.group(1)) == 0,
       f"the remaining count never goes below zero: {r.stdout.strip()[:160]}")


@case
def accept_never_calls_the_limit_spent_while_the_last_pass_is_open():
    """`pass-start` appends the pass when it opens, so with the last allowed pass open the
    count already equals the limit. `accept` read that as "report as INCOMPLETE" — and, with
    nothing recorded, "every pass was aborted" — under the blocker saying the open pass can
    still be recorded.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--max-passes", "1", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    out = run("accept", cwd=d).stdout
    ok("opened but not recorded" in out and "limit reached" not in out and "aborted" not in out,
       f"an open last pass is still a pass to record: {out.strip()[:240]}")
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    out = run("accept", cwd=d).stdout
    ok("pass limit reached (1)" in out and "aborted" not in out,
       f"once it is recorded and blocked, the limit is spent — and nothing was aborted: {out.strip()[:240]}")


@case
def accept_reports_the_pass_limit_even_when_every_pass_was_aborted():
    """The limit counts passes opened, so the hint must not depend on one being recorded.

    An area whose reviewers were all invalidated mid-flight — the files moved while they read
    — burns its passes through `pass-abort`, and an aborted pass counts as used. That is the
    state where the operator most needs to hear that the loop has run out, and it was the one
    state where `accept` said nothing: the hint required a recorded pass, and there was none.
    The only warning came from the next `pass-start` refusing.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")                       # the loop needs a scope to review
    run("init", "--max-passes", "2", "--", "src", cwd=d, check=True)
    for i in (3, 4):
        write(d, "src/a.py", f"{i}\n")
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-abort", "--reason", "the area moved mid-review", cwd=d, check=True)
    r = run("accept", cwd=d)
    ok(r.returncode != 0, "acceptance must still refuse")
    ok("pass limit reached (2)" in r.stdout,
       f"and must say the loop has run out of passes: {r.stdout.strip()[:200]}")
    ok("aborted" in r.stdout,
       f"and why there is nothing recorded to show for them: {r.stdout.strip()[:200]}")

    d = repo({"src/a.py": "1\n"})                     # the limit is not mentioned early
    write(d, "src/a.py", "2\n")
    run("init", "--max-passes", "5", "--", "src", cwd=d, check=True)
    ok("pass limit" not in run("accept", cwd=d).stdout,
       "a loop with passes left must not be told it is out of them")


@case
def no_command_edits_a_recorded_pass_while_a_newer_one_is_open():
    """`amend` and `resolve` both mutate the last recorded pass, so both must refuse.

    `amend` was taught to; `resolve` was not, and moved `resolved` and the resolution log
    onto a record no gate consults any more — then advised opening a new pass, which was
    already open. Each is asserted by the blocker it prints, not by a non-zero exit: they
    have several refusals apiece, and any of the others would satisfy a code-only check.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    pass_through(d, "7", "2", "trusted")
    write(d, "src/a.py", "3\n")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)                   # pass 2 open

    for cmd in (("resolve", "--fixed", "2"), ("amend", "--score", "9.5")):
        r = run(*cmd, cwd=d)
        ok(r.returncode != 0 and "pass 2 is open" in r.stderr,
           f"`{cmd[0]}` must refuse while a newer pass is open: rc={r.returncode} {r.stderr.strip()[:160]}")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    r1 = st["passes"][0]["result"]
    ok(r1["resolved"] == 0 and "resolution_log" not in r1,
       f"and the superseded pass must be untouched: {r1}")

    run("pass-abort", "--reason", "fixture", cwd=d, check=True)
    ok(run("resolve", "--fixed", "2", cwd=d).returncode == 0,
       "with no pass open, the documented order still works")


@case
def amend_refuses_while_a_newer_pass_is_open():
    """`amend` edits the last *recorded* pass, so an open one makes its target the wrong pass.

    The output an operator holds while a pass is open belongs to that pass; `amend` wrote it
    into the review the open pass supersedes and said "pass 1 amended" for a pass 2 that was
    empty. Nothing gates on a superseded record, so the edit changed only what `status`
    reports — a finding count raised on a review no longer under consideration.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    pass_through(d, "7", "1", "trusted")
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)                   # pass 2 open

    r = run("amend", "--score", "9.5", "--findings", "3", cwd=d)
    ok(r.returncode != 0 and "pass 2 is open" in r.stderr,
       f"amend must refuse while a newer pass is open: rc={r.returncode} {r.stderr.strip()[:160]}")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    ok(st["passes"][0]["result"]["findings"] == 1,
       f"and must not have touched the superseded pass: {st['passes'][0]['result']}")

    run("pass-record", "--findings", "0", "--understood", "--test-evidence", "trusted",
        cwd=d, check=True)
    out = run("amend", "--score", "9.5", cwd=d, check=True).stdout
    ok("pass 2 amended" in out, f"once recorded, amend targets that pass: {out.strip()[:160]}")


@case
def every_recorded_field_of_a_pass_reaches_the_text_status():
    """`status --json` is for machines; the text rendering is what an agent reads.

    A field the CLI accepts and `state.json` keeps, but the text output drops, is a place to
    put something that then silently does not exist — `--note` was recordable and invisible
    for as long as it existed. The exemptions are named here rather than assumed, so the next
    field added to a pass result has to be either rendered or listed with a reason.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "trusted", "--test-score", "7",
        "--note", "reviewer replaced after one clarification", cwd=d, check=True)
    run("resolve", "--fixed", "1", cwd=d, check=True)
    out = run("status", cwd=d, check=True).stdout
    result = read_state(d)["passes"][0]["result"]
    # `resolved` and `findings` are rendered together as "0/1 resolved"; the rest are
    # bookkeeping the text view deliberately summarises rather than dumps.
    shown_together = {"resolved", "findings"}
    bookkeeping = {"recorded", "resolution_log", "amendments"}
    missing = [k for k, v in result.items()
               if k not in shown_together and k not in bookkeeping
               and v is not None and str(v) not in out and k.replace("_", "-") not in out]
    ok(not missing, f"recorded field(s) invisible in text status: {missing} — output was:\n{out}")
    ok("reviewer replaced after one clarification" in out,
       f"the note itself must be readable, not just its key: {out}")


@case
def a_metric_the_reviewer_withheld_never_prints_as_none():
    """`--score` is optional on purpose, so every line that shows one must say so in words.

    The agent may not invent a value the reviewer withheld, which is exactly why `accept` and
    `status` printed `last score None` and `test-score=None`: a literal that reads as a value
    and blames the record for a number the reviewer simply never gave. `pass-record` already
    said "not given" — one site knowing the right wording is what a shared formatter is for.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    out = run("pass-record", "--findings", "0", "--understood", "--test-evidence", "trusted",
              cwd=d, check=True).stdout
    ok("not given" in out, f"pass-record must name the missing score: {out.strip()[:160]}")
    for cmd in (("status",), ("accept",)):
        r = run(*cmd, cwd=d, check=True)
        ok("None" not in r.stdout, f"{cmd[0]} must not print a metric as None: {r.stdout.strip()[:200]}")
        ok("not given" in r.stdout, f"{cmd[0]} must say the score was withheld: {r.stdout.strip()[:200]}")
    out = run("amend", "--note", "no number from the reviewer", cwd=d, check=True).stdout
    ok("None" not in out, f"amend must not print a metric as None: {out.strip()[:160]}")

    run("amend", "--score", "9.5", "--test-score", "8", cwd=d, check=True)
    out = run("status", cwd=d, check=True).stdout
    ok("score 9.5" in out and "test-score=8" in out and "not given" not in out,
       f"a recorded score must still print as itself: {out[:200]}")


@case
def a_report_only_loop_never_offers_the_fix_mode_steps():
    """`project next` wrote `report_only` into the loop state and nothing read it.

    So inside a report-only area `status` judged by the acceptance gate — open findings and
    an `inadequate` verdict, which are the report there, listed as blockers, and a clean area
    announced "ready to accept" — while `accept` and `resolve`, steps the area never takes,
    ran. Each must point at `project close` instead, and that must work.
    """
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "6", "--findings", "2", "--understood",
        "--test-evidence", "inadequate", cwd=d, check=True)
    out = run("status", cwd=d, check=True).stdout
    ok("ready for `project close`" in out and "unresolved" not in out and "inadequate" not in
       out.split("blockers:")[1], f"status must judge the area as report-only: {out.strip()[-240:]}")
    for cmd in (("accept",), ("resolve", "--fixed", "1")):
        r = run(*cmd, cwd=d)
        ok(r.returncode != 0 and "--report-only area" in r.stderr and "project close" in r.stderr,
           f"`{cmd[0]}` must refuse toward project close: rc={r.returncode} {r.stderr.strip()[:200]}")
    findings(d, "pkg", "A. two findings\nC. inadequate\n")
    run("project", "close", cwd=d, check=True)
    row = read_state(d, "project.json")["areas"][0]
    ok(row["status"] == "reviewed" and row["findings"] == 2,
       f"and project close records the report: {row}")

    d = repo()                                                   # fix-mode loops are untouched
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    pass_through(d)
    ok("ready to accept" in run("status", cwd=d, check=True).stdout,
       "a task loop still reads `ready to accept`")


@case
def report_only_close_reads_the_same_conditions_as_the_acceptance_gate():
    """Two hand-written lists of "what makes a review not a review" drift, always one-sided.

    Report-only never calls `blockers()`, so it kept its own copy: the open-pass check
    reached it long after acceptance had it, and `allow_empty` never did — an area queued
    deliberately empty (`project init --allow-empty`, a directory not written yet) closed
    `incomplete` no matter how carefully its report was written. The conditions now come
    from one place; only the wording differs, because an area reviewer is told about the
    area and a task loop about the scope.
    """
    d = repo({"keep/a.py": "x\n"})
    run("project", "init", "--report-only", "--allow-empty", "--", "planned", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    run("validate", "--none", "--reason", "the directory is not written yet", cwd=d, check=True)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "justified-absent", cwd=d, check=True)
    write_text(os.path.join(d, stem), "A. nothing here yet\n")
    run("project", "close", cwd=d, check=True)
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "reviewed",
       f"an area queued empty on purpose must close reviewed: {area['status']}, {area['blockers']}")

    # And the exemption is opt-in: an area that lost its files still fails.
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9", "0", "trusted")
    write_text(os.path.join(d, stem), "A. clean\n")
    sh("git rm -q -r pkg && git commit -qm drop", d)
    run("project", "close", cwd=d, check=True)
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "incomplete" and any("files are gone" in b for b in area["blockers"]),
       f"an area that vanished must still be incomplete: {area['status']}, {area['blockers']}")


@case
def a_review_without_a_test_evidence_verdict_is_not_a_review_in_either_mode():
    """The verdict is required output of the reviewer, not an acceptance-only condition.

    Its absence was checked in `blockers()` alone, which report-only `project close` never
    calls: an area recorded `--understood` without `--test-evidence` closed `reviewed`, with
    `test_evidence: null` and no blocker — and the text ledger, which printed the verdict
    only when it was set, showed nothing missing either.
    """
    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg", "lib", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood", cwd=d, check=True)
    write_text(os.path.join(d, stem), "A. clean\nB. understood\nD. 9\n")
    run("project", "close", cwd=d, check=True)
    area = read_state(d, "project.json")["areas"][0]
    ok(area["status"] == "incomplete"
       and any("test evidence not assessed" in b for b in area["blockers"]),
       f"no verdict, no review: {area['status']}, {area['blockers']}")

    out = run("project", "next", cwd=d, check=True).stdout          # `lib`, with a verdict
    stem = out.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "9.5", "0", "trusted")
    write_text(os.path.join(d, stem), "A. clean\n")
    run("project", "close", cwd=d, check=True)
    rows = {l.split("]", 1)[1].split()[0]: l
            for l in run("project", "status", cwd=d, check=True).stdout.splitlines()
            if l.startswith("  [")}
    ok("test-evidence not assessed" in rows["pkg"],
       f"a missing verdict must be printed as missing: {rows['pkg']}")
    ok("findings-file " + stem in rows["lib"] and "test-evidence trusted" in rows["lib"],
       f"the per-area report names the verdict and the findings file: {rows['lib']}")

    # Fix mode still refuses acceptance on it, now from the shared list.
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood", cwd=d, check=True)
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "test evidence not assessed" in r.stdout + r.stderr,
       f"accept must name the missing verdict: {(r.stdout + r.stderr).strip()[:160]}")


@case
def report_only_close_refuses_to_read_an_open_pass_as_a_review():
    """Silence is not a verdict — in both modes.

    Fix mode meets a dangling pass through `blockers()`; report-only never calls it, so an
    area with a pass opened and neither recorded nor aborted closed `reviewed`, counted that
    pass in the ledger, and deleted the state that held it. The reviewer's verdict was never
    transcribed, and the row said the area had been reviewed.
    """
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "8", "1", "trusted")
    write_text(os.path.join(d, stem), "A. one finding\n")

    write(d, "pkg/a.py", "y\n")                        # a second pass is opened...
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    sh("git checkout -- pkg/a.py", d)                  # ...and the area returns to the reviewed state
    r = run("project", "close", cwd=d, check=True)
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "incomplete",
       f"an unrecorded pass must not read as a completed review, got {area['status']}")
    ok(any("neither recorded nor aborted" in b for b in area["blockers"]),
       f"and the reason must name it: {area['blockers']}")


@case
def report_only_close_accepts_a_pass_that_was_properly_aborted():
    """The other side: `pass-abort` is the documented exit, so it must actually close one."""
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    pass_through(d, "8", "1", "trusted")
    write_text(os.path.join(d, stem), "A. one finding\n")
    write(d, "pkg/a.py", "y\n")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    sh("git checkout -- pkg/a.py", d)
    run("pass-abort", "--reason", "the area moved mid-review", cwd=d, check=True)
    run("project", "close", cwd=d, check=True)
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "reviewed" and not area["blockers"],
       f"an aborted pass must not block the close: {area['status']}, {area['blockers']}")


@case
def reset_survives_odd_shapes_and_says_why_the_directory_stays():
    """`reset` is the way out of a broken loop, so it must not break on one.

    It asked `os.path.exists` and then `os.remove`d: a dangling symlink at `state.json`
    (exists() is False, so the link stayed) and a directory at `state.json.tmp` (os.remove
    raised IsADirectoryError, a traceback). And after `reset --project` the kept findings
    reports counted as "ours", so `.loop-review/` survived without a word — while SKILL.md
    has the agent name that directory in the report.
    """
    d = repo({"pkg/a.py": "x\n"})
    run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    findings(d, "pkg", "A. clean\n")
    run("project", "close", cwd=d, check=True)
    sd = os.path.join(d, ".loop-review")
    os.symlink("nowhere.json", os.path.join(sd, "state.json"))     # dangling
    os.makedirs(os.path.join(sd, "state.json.tmp"))                 # a directory at the name
    r = run("reset", "--project", cwd=d)
    ok(r.returncode == 0 and "Traceback" not in r.stderr,
       f"reset must not crash on a link or a directory: rc={r.returncode} {r.stderr.strip()[-200:]}")
    ok(not os.path.lexists(os.path.join(sd, "state.json")),
       "the dangling link is unlinked, not left behind")
    ok(os.path.isdir(os.path.join(sd, "state.json.tmp")),
       "a directory is not this script's to delete")
    ok(os.path.isdir(os.path.join(sd, "findings")), "the findings reports are kept")
    ok("kept" in r.stdout and "findings/" in r.stdout,
       f"reset must say the directory stays, and why: {r.stdout.strip()[:300]}")
    ok(".loop-review/state.json.tmp is a directory" in r.stdout,
       f"and name the directory it would not delete: {r.stdout.strip()[:300]}")

    # Without findings to explain it, the directory must still be named — not "not empty".
    d = repo({"a.py": "1\n"})
    write(d, "a.py", "2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    os.makedirs(os.path.join(d, ".loop-review", "project.json.tmp"))
    r = run("reset", cwd=d)
    ok(r.returncode == 0 and ".loop-review/project.json.tmp is a directory" in r.stdout
       and "not empty" not in r.stdout,
       f"a lone directory at an artifact name must be named: {r.stdout.strip()[:300]}")

    # A directory at project.json is no ledger, and `reset --project` cannot remove it: say
    # neither "no ledger" nor "run `reset --project`", which is the command that just ran.
    d = repo({"a.py": "1\n"})
    os.makedirs(os.path.join(d, ".loop-review", "project.json"))
    r = run("reset", "--project", cwd=d)
    both = r.stdout + r.stderr
    ok(r.returncode == 0 and ".loop-review/project.json is a directory" in r.stdout
       and "no project ledger to clear" not in both and "`reset --project` discards" not in both,
       f"a directory at project.json must be named, not advised away: {both.strip()[:400]}")


@case
def reset_clears_its_own_debris_and_says_so_only_about_a_stranger():
    """What `reset` removes and what it calls "not ours" must come from one list.

    They did not: `project.json.tmp` — crash debris from an interrupted ledger write —
    survived a plain `reset`, and because the ownership test listed a different set, the very
    next line announced that `.loop-review/` "holds files this script did not create" about
    the file the script had just left there.
    """
    d = repo({"pkg/a.py": "x\n"})
    sd = os.path.join(d, ".loop-review")
    run("project", "init", "--", "pkg", cwd=d, check=True)
    write_text(os.path.join(sd, "project.json.tmp"), '{"partial"')
    r = run("reset", cwd=d, check=True)
    ok(not os.path.exists(os.path.join(sd, "project.json.tmp")),
       "a plain reset must clear the ledger's own .tmp debris")
    ok("did not create" not in r.stdout + r.stderr,
       f"and must not disown its own file: {(r.stdout + r.stderr).strip()[:200]}")
    ok(os.path.exists(os.path.join(sd, "project.json")),
       "while the ledger itself survives a reset without --project")

    write_text(os.path.join(sd, "notes.txt"), "operator notes\n")
    r = run("reset", "--project", cwd=d, check=True)
    ok(os.path.exists(os.path.join(sd, "notes.txt")),
       "a file the operator put there must never be deleted")
    ok("did not create" in r.stdout + r.stderr,
       f"and that is what the note is for: {(r.stdout + r.stderr).strip()[:200]}")


@case
def every_command_that_echoes_the_loop_context_shows_both_fields():
    """The brief and the scope note are the only things that cross into the reviewer's prompt.

    Four commands echo them and each printed the pair by hand; `init` — the command that
    records them — printed the brief and dropped the note. The note was in `state.json`, so
    nothing looked broken, but the agent fills the prompt from this output: a `--scope-note`
    given at `init` reached no reviewer until some later command happened to show it.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    note, brief = "only the parser hunks", "a.py must end with v2"
    out = run("init", "--task-brief", brief, "--scope-note", note, "--", "src",
              cwd=d, check=True).stdout
    ok(brief in out and note in out, f"init must confirm both fields it recorded: {out}")

    run("validate", "--", "true", cwd=d)
    for cmd in (("status",), ("pass-start",)):
        out = run(*cmd, cwd=d, check=True).stdout
        ok(brief in out and note in out, f"{cmd[0]} must echo both: {out[:200]}")
    run("pass-abort", "--reason", "brief changes only between passes", cwd=d, check=True)
    out = run("brief", "--force", "--scope-note", "narrowed to the parser", cwd=d, check=True).stdout
    ok(brief in out and "narrowed to the parser" in out, f"brief must echo both: {out[:200]}")


@case
def replacing_a_no_check_declaration_with_real_checks_is_not_shrinkage():
    """A declaration is not a run, so it never joins the set a later pass must carry.

    `validate --none` answers exit condition 1 honestly, but nothing was executed. Counted as
    evidence, it inverted the rule that caught it: an area that gained real tests between
    passes had its evidence *grow*, and `pass-start` refused that as shrinkage — demanding
    the declaration be re-run, or retired with a `--force` the agent is not allowed to use.
    Both exits were closed at once, which is what made it a dead end rather than a nuisance.
    """
    d = repo({"docs/a.md": "1\n"})
    write(d, "docs/a.md", "2\n")
    run("init", "--", "docs", cwd=d, check=True)
    run("validate", "--none", "--reason", "documentation only", cwd=d, check=True)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "justified-absent", cwd=d, check=True)
    write(d, "docs/a.md", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo real tests", cwd=d)     # the area gained a real check
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0,
       f"real checks replacing a declaration must not read as shrinkage: {r.stderr.strip()[:200]}")

    # The gate itself is untouched: a real check the pass rested on is still demanded.
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--none", "--reason", "no linter here", cwd=d, check=True)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "echo suite" in r.stderr,
       f"a real check the pass rested on must still be demanded: {r.stderr.strip()[:200]}")
    ok("no executable check applies" not in r.stderr,
       f"and the declaration must not be demanded alongside it: {r.stderr.strip()[:200]}")


@case
def a_lone_declaration_is_never_summarised_as_green():
    """The header asked "are all exits zero?", and a declaration carries exit 0.

    So a loop whose entire validation was `validate --none` printed `1 command(s), GREEN` —
    the reading AGENTS.md forbids, on the line an agent transcribes into the reviewer's
    prompt. The per-record rendering was corrected for exactly this once; the header kept
    its own copy of the question, which is why the answer had to move into one helper.
    """
    d = repo({"docs/a.md": "1\n"})
    write(d, "docs/a.md", "2\n")
    run("init", "--", "docs", cwd=d, check=True)
    run("validate", "--none", "--reason", "prose only", cwd=d, check=True)
    out = run("status", cwd=d, check=True).stdout
    ok("GREEN" not in out, f"a declaration alone must not summarise as GREEN: {out[:200]}")
    ok("NOTHING RAN" in out, f"and must say that nothing ran: {out[:200]}")

    run("validate", "--", "echo lint", cwd=d)          # a real check joins it
    out = run("status", cwd=d, check=True).stdout
    ok("GREEN" in out, f"a real green check must still read GREEN: {out[:200]}")
    run("validate", "--", "false", cwd=d)
    out = run("status", cwd=d, check=True).stdout
    ok("RED" in out, f"and a failing one must still read RED: {out[:200]}")


@case
def a_declaration_is_never_rendered_as_a_command_that_passed():
    """Every list of validation records goes through one renderer, diagnostics included.

    `validate-drop`'s "recorded commands" list wrote its own loop and printed a `validate
    --none` record as `<no executable check applies> -> exit 0`: a declaration that nothing
    was run, dressed as a command that ran and passed — in the very list an operator reads to
    decide what to retract. `print_validation` had always rendered it as the claim it is.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--none", "--reason", "documentation only", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    err = run("validate-drop", "--", "pytest -q", cwd=d).stderr
    ok("no executable check applies: documentation only" in err,
       f"the declaration must read as a claim: {err.strip()[:200]}")
    ok("exit 0" not in err.split("no executable check applies")[1].split("\n")[0],
       f"and never as a command that exited 0: {err.strip()[:200]}")
    ok("echo lint  -> exit 0" in err, f"while a real run still shows its code: {err.strip()[:200]}")

    out = run("status", cwd=d, check=True).stdout
    ok("no executable check applies: documentation only" in out,
       f"status renders it the same way: {out[:200]}")


@case
def none_and_a_command_are_refused_together_by_both_validate_commands():
    """`--none` is about the declaration, a command after `--` is about a run: exactly one.

    `validate-drop` refused the pair; `validate` accepted it and dropped the command,
    recording "no executable check applies" for a surface that has a test suite — and that
    sentence is transcribed into the reviewer's prompt. A rejected typo costs a retry; this
    one told the reviewer there was nothing to run. Both refusals are asserted by their
    blocker: each command has several, and any of them would satisfy a code-only check.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    for cmd in ("validate", "validate-drop"):
        r = run(cmd, "--none", "--reason", "no checks", "--", "echo", "real", cwd=d)
        ok(r.returncode != 0 and "not both" in r.stderr,
           f"`{cmd} --none -- <command>` must be refused: rc={r.returncode} {r.stderr.strip()[:160]}")
        r = run(cmd, cwd=d)
        ok(r.returncode != 0 and "--none" in r.stderr,
           f"`{cmd}` with neither must name both ways in: {r.stderr.strip()[:160]}")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    ok(not st["validation"], f"nothing may have been recorded by a refused call: {st['validation']}")

    run("validate", "--", "echo real", cwd=d, check=True)
    run("validate", "--none", "--reason", "docs only", cwd=d, check=True)
    ok(run("validate-drop", "--none", cwd=d).returncode == 0,
       "and each form on its own must still work")

    # `--reason` is the sentence the reviewer is given in place of a check, so in `validate`
    # it belongs to `--none`. Passed with a command it was read by nothing and dropped in
    # silence: the operator's explanation went nowhere and the record kept no trace of it.
    r = run("validate", "--reason", "flaky on CI", "--", "echo real", cwd=d)
    ok(r.returncode != 0 and "--reason" in r.stderr,
       f"validate must refuse --reason alongside a command: rc={r.returncode} {r.stderr.strip()[:160]}")
    # In `validate-drop` the reason annotates the retraction, not a declaration, so both
    # forms carry one.
    ok(run("validate-drop", "--force", "--reason", "superseded", "--", "echo real",
           cwd=d).returncode == 0,
       "validate-drop must keep --reason with a command")


@case
def new_terms_make_the_last_review_stale_and_never_land_mid_review():
    """The brief and scope note are reviewed terms; the fingerprint covered only the code.

    `brief` changed them while a reviewer was reading, and over a recorded clean pass it left
    that pass "current": `accept` signed off on requirements no reviewer saw, and `pass-start`
    refused the new pass as unchanged. Every command that changes what a review rests on is
    refused while one is open, by one guard.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    for cmd in (("brief", "--task-brief", "a.py must end with v2"),
                ("brief", "--force", "--scope-note", "only the parser"),
                ("scope", "--", "other.py"),
                ("validate-drop", "--", "true")):
        r = run(*cmd, cwd=d)
        ok(r.returncode != 0 and "pass 1 is open" in r.stderr,
           f"`{' '.join(cmd)}` must wait for the open review: rc={r.returncode} "
           f"{r.stderr.strip()[:160]}")
    ok(read_state(d).get("task_brief") is None, "and the refused brief must not be recorded")
    run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "the fixture accepts on the terms it was given")

    for flags in (("--task-brief", "a.py must end with v2"),                 # set for the first time
                  ("--force", "--task-brief", "a.py must end with v3")):     # replaced
        run("brief", *flags, cwd=d, check=True)
        r = run("accept", cwd=d)
        ok(r.returncode != 0 and "brief or scope note changed since pass" in r.stdout,
           f"`brief {' '.join(flags)}`: a review given other terms must not be accepted: "
           f"{r.stdout.strip()[:200]}")
        r = run("pass-start", cwd=d)
        ok(r.returncode == 0,
           f"and the new pass on unchanged code must open: {r.stderr.strip()[:200]}")
        run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        r = run("accept", cwd=d)
        ok(r.returncode == 0, f"a review on the new terms accepts: {r.stdout.strip()[:200]}")


@case
def a_mistaken_no_check_declaration_can_be_withdrawn_by_name():
    """`validate --none` writes a record under an internal marker, so retracting it needed one.

    The marker is the script's own string, not something the CLI ever asks for or the docs
    name: an operator who declared "no executable check applies" by mistake could only undo
    it by typing an internal constant verbatim. Withdrawal is safe by construction — it takes
    away the record that satisfies exit condition 1, so every gate gets stricter, not looser.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--none", "--reason", "documentation only", cwd=d, check=True)
    ok(run("pass-start", cwd=d).returncode == 0, "precondition: the declaration satisfies the gate")
    run("pass-abort", "--reason", "fixture", cwd=d, check=True)

    r = run("validate-drop", "--none", "--reason", "recorded by mistake", cwd=d)
    ok(r.returncode == 0, f"--none must withdraw the declaration: {r.stderr.strip()[:160]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "no validation" in r.stderr,
       f"and the loop must then demand real validation: {r.stderr.strip()[:160]}")

    ok(run("validate-drop", "--none", "--", "true", cwd=d).returncode != 0,
       "`--none` and a command name two different records; both at once is a mistake")
    r = run("validate-drop", cwd=d)
    ok(r.returncode != 0 and "--none" in r.stderr,
       f"and the empty call must name both ways in: {r.stderr.strip()[:160]}")


@case
def validate_drop_describes_the_state_it_refuses_in():
    """The refusal is read by whoever must decide; it has to be true about the state.

    The inherited branch phrased every case as "it does not run here (exit N)", so a check
    re-run green produced "it does not run here (exit 0)" — a false statement about the very
    state the message was printed in, on the path where a human is asked whether to give up
    evidence. Asserting only the exit code left that invisible: the refusal was correct, and
    its explanation was not.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)

    err = run("validate-drop", "--", "echo suite", cwd=d).stderr        # inherited, not re-run
    ok("has not been re-run" in err, f"absent: {err.strip()[:160]}")

    run("validate", "--", "echo suite", cwd=d)                          # inherited, green here
    err = run("validate-drop", "--", "echo suite", cwd=d).stderr
    ok("green on the current state" in err and "does not run here" not in err,
       f"a check that just ran green must not be described as not running: {err.strip()[:160]}")

    run("validate", "--", "false", cwd=d)                               # ran and failed
    err = run("validate-drop", "--", "false", cwd=d).stderr
    ok("failed (exit 1)" in err, f"failed: {err.strip()[:160]}")

    run("validate", "--", "echo extra", cwd=d)                          # ran green, not inherited
    err = run("validate-drop", "--", "echo extra", cwd=d).stderr
    ok("green on the current state" in err and "exit 0" not in err,
       f"green: {err.strip()[:160]}")

    run("validate", "--", "nosuchtool --x", cwd=d)                      # never started
    ok(run("validate-drop", "--", "nosuchtool --x", cwd=d).returncode == 0,
       "a command that never ran and no pass rested on still clears freely")


@case
def a_retirement_holds_until_the_command_is_run_again():
    """Retiring a check is a decision about the check, not about one fingerprint.

    `retired` was read at the current fingerprint only, so a `validate-drop --force` made on
    one state vanished at the next edit and `pass-start` demanded the same `--force` again.
    Nothing was blocked for good, which is the point: the human escape hatch degraded into a
    keystroke the agent repeats reflexively, and a hatch pressed by reflex has stopped being
    a decision. It ends the moment the command is run again — that supersedes it.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")                           # fix batch
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    run("validate-drop", "--force", "--reason", "suite folded into lint", "--", "echo suite",
        cwd=d, check=True)

    write(d, "src/a.py", "4\n")                           # one more edit before the pass
    run("validate", "--", "echo lint", cwd=d)
    r = run("validate-drop", "--force", "--", "echo suite", cwd=d)
    ok(r.returncode != 0 and "already retired" in r.stderr,
       f"re-forcing a standing decision must be refused as such: {r.stderr.strip()[:160]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0,
       f"an edit must not undo a retirement: {r.stderr.strip()[:200]}")

    run("pass-record", "--score", "9", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "5\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    run("validate", "--", "echo suite", cwd=d)             # run again: retirement ends
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "6\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "echo suite" in r.stderr,
       f"a check run again is back in the set and must be demanded: {r.stderr.strip()[:200]}")


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
    write_text(tool, "#!/bin/sh\nexit 0\n")
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
def an_inherited_check_cannot_be_retired_on_the_reviewed_state_either():
    """The pass's evidence set is the same question whether or not the scope has moved.

    It was computed only when the current fingerprint differed from the pass's. Standing on
    the reviewed state — straight after `pass-record`, or after reverting a fix — the set
    came out empty, so a check re-run there to exit 126 read as a typo nobody leaned on: a
    plain `validate-drop` retracted the 126 *and* the green record the review was granted
    on, and `accept` signed off on `{true}` where the pass had rested on `{suite, true}`.
    """
    d = repo({"src/a.py": "1\n"})
    tool = os.path.join(d, "suite.sh")
    write_text(tool, "#!/bin/sh\nexit 0\n")
    os.chmod(tool, 0o755)
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)

    os.chmod(tool, 0o644)                             # same fingerprint; tool cannot start
    run("validate", "--", tool, cwd=d)                # -> exit 126
    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode != 0 and "carried pass 1" in r.stderr and "--force" in r.stderr,
       f"on the reviewed state an inherited check still needs --force: rc={r.returncode} "
       f"{r.stderr.strip()[:160]}")
    st = read_state(d)
    ok(not any(v.get("retracted") for v in st["validation"] if v["cmd"] == tool),
       "a refused drop must leave every record of the check in place")
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "validation red" in (r.stdout + r.stderr),
       f"accept must still see the check fail to start: {(r.stdout + r.stderr).strip()[:160]}")

    run("validate", "--", "pytset tests/", cwd=d)     # a typo after the pass, same state
    r = run("validate-drop", "--", "pytset tests/", cwd=d)
    ok(r.returncode == 0,
       f"a record made after the pass opened is not what it rested on: {r.stderr.strip()[:160]}")


@case
def a_never_ran_drop_retracts_only_what_never_ran():
    """The never-ran waiver covers the 126/127 record, not every record of the command.

    The verdict reads the latest record; the cleanup marked them all. A check green earlier
    on the same state lost that run to a plain `validate-drop` granted for a missing tool.
    """
    d = repo({"src/a.py": "1\n"})
    tool = os.path.join(d, "extra.sh")
    write_text(tool, "#!/bin/sh\nexit 0\n")
    os.chmod(tool, 0o755)
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)                # green, no pass rests on it
    os.chmod(tool, 0o644)
    run("validate", "--", tool, cwd=d)                # -> exit 126
    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode == 0 and "retracted 1 record" in r.stdout,
       f"only the never-ran record may go: rc={r.returncode} {r.stdout.strip()[:160]}")
    st = read_state(d)
    kept = [v for v in st["validation"] if v["cmd"] == tool and not v.get("retracted")]
    ok([v["exit"] for v in kept] == [0], f"the green run must survive: {kept}")

    # Correcting a record is not retiring the check. The retracted 126 was the command's
    # last entry, so `retired` read it as a retirement: the green run carried pass 1, and
    # the next pass opened without it and without --force.
    os.chmod(tool, 0o755)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "not re-run since the change" in r.stderr and tool in r.stderr,
       f"a corrected typo must not retire the check pass 1 rested on: rc={r.returncode} "
       f"{r.stderr.strip()[:200]}")


@case
def a_retirement_is_an_event_even_when_it_marks_records():
    """`retired` is the command's latest event by list position, so a retirement has to be one.

    Forcing a drop over records on the current state marked them in place and appended
    nothing. A run of the same command on another fingerprint, made later and then reverted
    away from, stayed the latest event: the check left the current state but never entered
    `retired`, and `pass-start` kept demanding the check a human had just retired.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")                       # fix batch, state B
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    write(d, "src/a.py", "4\n")                       # state C: suite runs here last
    run("validate", "--", "echo suite", cwd=d)
    write(d, "src/a.py", "3\n")                       # back to B, then retire it here
    run("validate-drop", "--force", "--reason", "suite folded into lint", "--", "echo suite",
        cwd=d, check=True)
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0,
       f"a forced retirement must take the check out of the set: {r.stderr.strip()[:200]}")
    run("pass-abort", "--reason", "probe", cwd=d, check=True)

    # The retirement supersedes the run on C too: returning there must not bring it back.
    st = read_state(d)
    fp_c = [v for v in st["validation"] if v["cmd"] == "echo suite"][-2]["fingerprint"]
    at_c, retired = module().validation_view(st, fp_c)
    ok("echo suite" not in at_c and "echo suite" in retired,
       f"a retired check must not count on any state: at C {sorted(at_c)}, retired {sorted(retired)}")


@case
def a_retirement_never_rewrites_what_the_pass_rested_on():
    """What a pass rested on is a fact about the pass; a later retirement cannot edit it.

    Retiring on the reviewed fingerprint marked the pass's own green record retracted, and
    the set was read back from those records. Run the check again — which ends the
    retirement — and it was in the set of no pass: edit the files and it was demanded by
    nothing; let it fail to start and a plain `validate-drop` retired it once more.
    """
    d = repo({"src/a.py": "1\n"})
    tool = os.path.join(d, "suite.sh")
    write_text(tool, "#!/bin/sh\nexit 0\n")
    os.chmod(tool, 0o755)
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("validate-drop", "--force", "--", tool, cwd=d, check=True)   # on the reviewed state
    run("validate", "--", tool, cwd=d)                                # run again: back in
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "not re-run since the change" in r.stderr and tool in r.stderr,
       f"a check run again after its retirement is demanded again: {r.stderr.strip()[:200]}")

    os.chmod(tool, 0o644)
    run("validate", "--", tool, cwd=d)                                # -> exit 126
    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode != 0 and "carried pass 1" in r.stderr,
       f"it is still the pass's evidence, so dropping it needs --force: rc={r.returncode} "
       f"{r.stderr.strip()[:160]}")


@case
def a_check_any_pass_rested_on_needs_force_not_only_the_last_one():
    """"No recorded pass rested on it" means no pass, not the last recorded one.

    A later pass opened without the check — after a human retired it — made it look
    unclaimed once it was run again: on the next state a re-run to 127 read as a typo and a
    plain `validate-drop` cleared it, though pass 1 had been granted on it.
    """
    d = repo({"src/a.py": "1\n"})
    tool = os.path.join(d, "suite.sh")
    write_text(tool, "#!/bin/sh\nexit 0\n")
    os.chmod(tool, 0o755)
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("validate-drop", "--force", "--", tool, cwd=d, check=True)   # the human retires it
    run("pass-start", cwd=d, check=True)             # pass 2 rests on `true` alone
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "4\n")                      # a new fingerprint
    run("resolve", "--fixed", "1", cwd=d, check=True)
    os.remove(tool)
    run("validate", "--", "true", cwd=d)
    run("validate", "--", tool, cwd=d)               # -> exit 127, ending the retirement
    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode != 0 and "carried pass 1" in r.stderr and "--force" in r.stderr,
       f"pass 1 rested on it, so dropping it needs --force: rc={r.returncode} "
       f"{r.stderr.strip()[:160]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "never ran" in r.stderr and "carried a recorded pass" in r.stderr,
       f"and pass-start's hint must say so too: {r.stderr.strip()[:240]}")


@case
def report_only_close_needs_validation_that_ran():
    """Red is allowed in report-only; absent or never-ran validation is not.

    `review_integrity` never asked about validation, and report-only close reads nothing
    else: withdrawing the area's `validate --none` after the pass, or a check that could not
    start on the reviewed state, still closed the area `reviewed`.
    """
    def area(kind):
        d = repo({"pkg/a.py": "x\n"})
        run("project", "init", "--report-only", "--", "pkg", cwd=d, check=True)
        out = run("project", "next", cwd=d, check=True).stdout
        stem = out.split("findings file: ")[1].splitlines()[0]
        if kind == "none":
            run("validate", "--none", "--reason", "prose only", cwd=d, check=True)
        else:
            run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "9", "--findings", "0", "--understood",
            "--test-evidence", "justified-absent", cwd=d, check=True)
        if kind == "none":
            run("validate-drop", "--none", cwd=d, check=True)
        else:
            run("validate", "--", "nosuchtool --x", cwd=d)    # exit 127, on the reviewed state
        write_text(os.path.join(d, stem), "A. clean\n")
        run("project", "close", cwd=d, check=True)
        return read_state(d, "project.json")["areas"][0]

    row = area("none")
    ok(row["status"] == "incomplete" and any("no validation" in b for b in row["blockers"]),
       f"no validation left, no review: {row['status']}, {row['blockers']}")
    row = area("never-ran")
    ok(row["status"] == "incomplete" and any("never ran" in b for b in row["blockers"]),
       f"a check that never ran is evidence about nothing: {row['status']}, {row['blockers']}")


@case
def pass_start_force_never_waives_the_evidence_set():
    """`--force` answers the limit and an unchanged scope; it never lets evidence shrink.

    `pass_start_blockers` skipped the shrinkage gate under `--force`, so the documented
    override for the pass limit also opened a pass on fewer checks, with no retirement in
    the log — and the next gates compared against that pass alone, so `accept` signed off
    without the check every earlier pass had rested on.
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--max-passes", "1", "--", "src", cwd=d, check=True)
    run("validate", "--", "echo suite", cwd=d)
    run("validate", "--", "echo lint", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "7", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "echo lint", cwd=d)                    # suite not re-run
    r = run("pass-start", "--force", cwd=d)                       # past the limit, by a human
    ok(r.returncode != 0 and "not re-run since the change" in r.stderr
       and "`echo suite` (pass 1)" in r.stderr,
       f"--force for the limit must not open a pass without `echo suite`: rc={r.returncode} "
       f"{r.stderr.strip()[:200]}")

    # A state written while the gate could be waived: pass 2 rests on `echo lint` alone.
    run("validate", "--", "echo suite", cwd=d)
    run("pass-start", "--force", cwd=d, check=True)
    run("pass-record", "--score", "9", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    st = read_state(d)
    st["passes"][-1]["rested_on"] = ["echo lint"]
    st["validation"] = [v for v in st["validation"]
                        if not (v["cmd"] == "echo suite" and v["fingerprint"] == st["passes"][-1]["fingerprint"])]
    write_state(d, st)
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "`echo suite` (pass 1)" in r.stdout + r.stderr,
       f"accept must still demand what pass 1 rested on: {(r.stdout + r.stderr).strip()[:200]}")


@case
def what_a_pass_rested_on_is_what_it_opened_on():
    """The evidence set is the pass's own record, not every run ever made on its state.

    Read from the log by fingerprint, a typo or a diagnostic failing run made on the reviewed
    state after the pass joined the set: after the next edit `pass-start` demanded it as
    something pass 1 had rested on. And the set holds what a project pass was opened *over*
    — red included — so it must never be described as "everything here passed".
    """
    d = repo({"src/a.py": "1\n"})
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "8", "--findings", "1", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("validate", "--", "pytset tests/", cwd=d)     # after the pass, on its state
    run("validate", "--", "false", cwd=d)             # a diagnostic run that fails
    write(d, "src/a.py", "3\n")
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0,
       f"runs made after pass 1 opened are not what it rested on: {r.stderr.strip()[:200]}")

    d = repo({"pkg/a.py": "x\n"})
    tool = os.path.join(d, "check.sh")
    write_text(tool, "#!/bin/sh\nexit 1\n")
    os.chmod(tool, 0o755)
    run("project", "init", "--", "pkg", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    run("validate", "--", tool, cwd=d)                # inherited red: the pass opens over it
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "6", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    os.chmod(tool, 0o644)
    run("validate", "--", tool, cwd=d)                # -> exit 126
    r = run("validate-drop", "--", tool, cwd=d)
    ok(r.returncode != 0 and "carried pass 1" in r.stderr
       and "failing when pass 1 opened" in r.stderr,
       f"a check the pass was opened over stays in its set, and the refusal says it was "
       f"failing: rc={r.returncode} {r.stderr.strip()[:240]}")


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
def resolution_is_read_from_the_log_by_every_gate():
    """`resolved` and `any(fixed)` were read beside the log, and each came apart from it.

    A stray `resolve --fixed` locked `accept` for good on the reviewed state even after every
    finding was withdrawn — a review that asked for no change at all. And `amend --findings`,
    which the clamp in `resolve` recommends, raised the total without re-reading the
    resolutions the log already held, so they stayed unresolved.
    """
    def loop(findings):
        d = repo()
        write(d, "a.py", "v2\n")
        run("init", "--", "a.py", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "8", "--findings", str(findings), "--understood",
            "--test-evidence", "trusted", cwd=d, check=True)
        return d

    d = loop(2)
    run("resolve", "--fixed", "1", cwd=d, check=True)              # typed in error
    run("resolve", "--withdrawn", "2", cwd=d, check=True)          # the reviewer withdrew both
    r = run("accept", cwd=d)
    ok(r.returncode == 0,
       f"nothing left to fix, so the reviewed state is the honest one: {r.stdout.strip()[:200]}")

    d = loop(2)                                                     # the gate still holds
    run("resolve", "--withdrawn", "1", "--fixed", "1", cwd=d, check=True)
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "recorded fixes are not present" in r.stdout,
       f"a finding that stands and was 'fixed' with no edit must still block: {r.stdout.strip()[:200]}")

    d = loop(1)
    run("resolve", "--withdrawn", "2", cwd=d, check=True)          # clamped to 1/1
    out = run("amend", "--findings", "2", cwd=d, check=True).stdout
    ok("2/2 resolved" in out, f"amend must reconcile the raised total with the log: {out.strip()[:200]}")
    r = run("accept", cwd=d)
    ok(r.returncode == 0, f"and accept must read the same: {r.stdout.strip()[:200]}")


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
    # Withdrawal and adjudication settle a finding without touching the code, so they are
    # what can unblock acceptance on this very state; `--fixed` cannot, and must not — see
    # a_recorded_fix_must_be_on_disk_before_acceptance.
    run("resolve", "--withdrawn", "1", "--adjudicated-invalid", "1", cwd=d, check=True)
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
def a_validation_command_is_checked_before_it_runs():
    """The command after `--` is input like any flag, and it was taken on trust.

    `validate -- ""` ran an empty shell line, exited 0 and was recorded GREEN — exit
    condition 1 met by running nothing. The declaration's key typed as a command was stored
    under that key, and `validate-drop --none` retracted it together with the real
    declaration; a blank `--reason` was recorded as the sentence the reviewer is given.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    nocheck = module().NO_CHECK
    for args, why in ((("validate", "--", ""), "is empty"),
                      (("validate", "--", "   ", ""), "is empty"),
                      (("validate", "--", nocheck), "not a command"),
                      (("validate", "--none", "--reason", "   "), "is blank"),
                      (("validate-drop", "--", " "), "is empty"),
                      (("validate-drop", "--", nocheck), "not a command")):
        r = run(*args, cwd=d)
        ok(r.returncode != 0 and why in r.stderr,
           f"`{' '.join(map(repr, args))}` must be refused ({why}): rc={r.returncode} "
           f"{(r.stderr or r.stdout).strip()[:160]}")
    ok(not read_state(d)["validation"], "and none of them may leave a record")

    run("validate", "--none", "--reason", "  prose only  ", cwd=d, check=True)
    ok(read_state(d)["validation"][-1]["reason"] == "prose only",
       "the reason is recorded as words, not padding")
    run("validate-drop", "--none", cwd=d, check=True)
    r = run("validate-drop", "--none", cwd=d)
    ok(r.returncode != 0 and "no `validate --none` declaration" in r.stderr
       and nocheck not in r.stderr and "retired" not in r.stderr,
       f"a second withdrawal says what is missing, in the operator's terms: {r.stderr.strip()[:160]}")


@case
def a_blank_path_argument_is_refused_not_read_as_the_cwd():
    """`os.path.abspath("")` is the cwd, so an empty shell variable became `.`.

    `scope -- "$EXTRA"` with EXTRA unset widened the loop to the whole repository — and scope
    never narrows, so for good; `init` and `project init` reviewed everything instead of
    refusing; `--task-brief-file ""` tried to read a directory.
    """
    d = repo({"a.py": "1\n", "b.py": "2\n"})
    write(d, "a.py", "v2\n")
    for args in (("init", "--", ""), ("init", "--", "a.py", "  "),
                 ("init", "--task-brief-file", "", "--", "a.py"),
                 ("project", "init", "--", ""), ("project", "init", "--", "a.py", "+", "")):
        r = run(*args, cwd=d)
        ok(r.returncode == 2 and "empty path" in r.stderr,
           f"`{' '.join(map(repr, args))}` must be refused as an empty path: rc={r.returncode} "
           f"{r.stderr.strip()[-160:]}")
    ok(not os.path.exists(os.path.join(d, ".loop-review", "state.json"))
       and not os.path.exists(os.path.join(d, ".loop-review", "project.json")),
       "and none of them may start a loop or a queue")
    run("init", "--", "a.py", cwd=d, check=True)
    r = run("scope", "--", "", cwd=d)
    ok(r.returncode == 2 and "empty path" in r.stderr and read_state(d)["scope"] == ["a.py"],
       f"`scope -- ''` must not widen the loop to the repository: {read_state(d)['scope']}")


@case
def a_check_killed_by_a_signal_reports_one_status_everywhere():
    """A shell killed by signal N returns -N to Python, and that number leaked three ways.

    The record and the message said `exit -9`; `sys.exit(-9)` left the script with 247. SKILL
    says `validate` "exits with its status", and a caller reading `$?` saw a third value. The
    shell convention, 128+N, is what all three now carry; the signal is recorded beside it.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    r = run("validate", "--", "kill -9 $$", cwd=d)
    rec = read_state(d)["validation"][-1]
    ok(r.returncode == 137 and rec["exit"] == 137 and rec.get("signal") == 9,
       f"exit status, record and signal must agree: rc={r.returncode} {rec}")
    ok("exit 137" in r.stdout and "SIGKILL" in r.stdout,
       f"the message must name the status and the signal: {r.stdout.strip()[-120:]}")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "validation is red" in r.stderr,
       f"a killed check is red, not 'never ran': {r.stderr.strip()[:160]}")


@case
def an_interrupted_or_unwritable_validation_records_nothing_and_says_so():
    """Ctrl-C mid-check, or a state file that cannot be written, ended in a traceback.

    Nothing false was recorded — the write is atomic and every gate recomputes the
    fingerprint — but the operator could not tell. An interrupted run is evidence about
    nothing: record nothing, say whether the scope moved meanwhile, and keep the gates
    seeing the state as it now is.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    r = run("validate", "--", "echo v3 > a.py; kill -INT $PPID; sleep 2", cwd=d)
    ok(r.returncode == 130 and "interrupted — nothing recorded" in r.stderr
       and "changed while it ran" in r.stderr and "Traceback" not in r.stderr,
       f"an interrupted check: rc={r.returncode} {r.stderr.strip()[-200:]}")
    ok(not read_state(d)["validation"], "and it leaves no record")
    r = run("pass-start", cwd=d)
    ok(r.returncode != 0 and "no validation recorded for the current state" in r.stderr,
       f"the gate sees the moved state as unvalidated: {r.stderr.strip()[:160]}")

    os.makedirs(os.path.join(d, ".loop-review", "state.json.tmp"))
    r = run("validate", "--", "true", cwd=d)
    ok(r.returncode == 1 and "nothing was recorded; the previous state is intact" in r.stderr
       and "Traceback" not in r.stderr,
       f"a state write that fails says so: rc={r.returncode} {r.stderr.strip()[-200:]}")
    ok(not read_state(d)["validation"], "and the old state is what remains")


@case
def the_operators_files_and_values_are_refused_by_name_not_by_traceback():
    """A brief file in another encoding, a state file that cannot be opened, and a blank
    brief or scope note — each was a traceback or a silent "none given".
    """
    d = repo()
    write(d, "a.py", "v2\n")
    with open(os.path.join(d, "brief.txt"), "wb") as f:
        f.write("Anforderung: Größe\n".encode("latin-1"))
    r = run("init", "--task-brief-file", "brief.txt", "--", "a.py", cwd=d)
    ok(r.returncode == 1 and "is not UTF-8 text" in r.stderr and "Traceback" not in r.stderr,
       f"a non-UTF-8 brief file is refused by name: rc={r.returncode} {r.stderr.strip()[-160:]}")
    write_text(os.path.join(d, "ok.txt"), "a.py must end with v2\n")
    for args, why in ((("init", "--task-brief", "", "--", "a.py"), "--task-brief is blank"),
                      (("init", "--task-brief", "   ", "--", "a.py"), "--task-brief is blank"),
                      (("init", "--scope-note", "", "--", "a.py"), "--scope-note is blank"),
                      (("init", "--task-brief", "", "--task-brief-file", "ok.txt", "--", "a.py"),
                       "not both"),
                      # In project mode the flag itself is wrong, whatever its value: "blank,
                      # give the requirements" sent the operator to a value refused next.
                      (("init", "--mode", "project", "--task-brief", "", "--", "a.py"),
                       "--task-brief applies to --mode changes only"),
                      (("init", "--mode", "project", "--scope-note", "", "--", "a.py"),
                       "--scope-note applies to --mode changes only")):
        r = run(*args, cwd=d)
        ok(r.returncode != 0 and why in r.stderr,
           f"`{' '.join(map(repr, args))}` must be refused ({why}): "
           f"rc={r.returncode} {r.stderr.strip()[:160]}")
    ok(not os.path.exists(os.path.join(d, ".loop-review", "state.json")), "and none of them did")

    run("init", "--", "a.py", cwd=d, check=True)
    r = run("brief", "--task-brief", "", "--scope-note", "only the parser", cwd=d)
    ok(r.returncode != 0 and "--task-brief is blank" in r.stderr and read_state(d).get("scope_note") is None,
       f"brief must not drop a blank brief and record the note anyway: {r.stderr.strip()[:160]}")

    p = repo({"pkg/a.py": "x\n"})                                # a project loop: name the flags
    run("project", "init", "--", "pkg", cwd=p, check=True)
    run("project", "next", cwd=p, check=True)
    for args, named in ((("brief", "--task-brief", ""), "--task-brief"),
                        (("brief", "--task-brief", "", "--scope-note", "x"), "--task-brief and --scope-note")):
        r = run(*args, cwd=p)
        ok(r.returncode != 0 and r.stderr.split("loop-review: ")[-1].startswith(named + ":"),
           f"`{' '.join(map(repr, args))}` must name every flag given, blank or not: {r.stderr.strip()[:160]}")

    sf = os.path.join(d, ".loop-review", "state.json")
    os.remove(sf)
    os.makedirs(sf)
    r = run("status", cwd=d)
    ok(r.returncode == 1 and "cannot be read" in r.stderr and "Traceback" not in r.stderr,
       f"a state file that cannot be opened is named: {r.stderr.strip()[-160:]}")
    os.rmdir(sf)
    os.makedirs(os.path.join(d, ".loop-review", "project.json"))
    r = run("project", "status", cwd=d)
    ok(r.returncode == 1 and "cannot be read" in r.stderr and "Traceback" not in r.stderr,
       f"so is a ledger that cannot be opened: {r.stderr.strip()[-160:]}")


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
    write_text(os.path.join(d, "sub", "brief.md"), "SUB-BRIEF\n")
    subprocess.run([sys.executable, LOOP, "init", "--task-brief-file", "brief.md",
                    "--", "../a.py"], cwd=os.path.join(d, "sub"),
                   capture_output=True, text=True)
    state = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
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
        good = json.loads(read_text(lr.STATE_FILE))
        try:
            lr.save(dict(good, poison=object()))
        except TypeError:
            pass
        try:
            after = json.loads(read_text(lr.STATE_FILE))
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
def an_empty_findings_file_is_no_review():
    """Existence was the whole check, and `project close` deletes the loop state right after.

    A `touch`, an interrupted `> file` redirect or a directory at the path passed, and the
    area closed `reviewed` — or `accepted` — with no review text anywhere. Both modes, and
    the refusal names what is wrong with the file rather than calling it missing.
    """
    for report_only, body, why in ((True, "", "is empty"), (True, " \n\t\n", "is empty"),
                                   (True, None, "is not a regular file"),
                                   (False, "", "is empty")):
        d = repo({"pkg/a.py": "x\n"})
        run("project", "init", *(["--report-only"] if report_only else []), "--", "pkg",
            cwd=d, check=True)
        out = run("project", "next", cwd=d, check=True).stdout
        stem = os.path.join(d, out.split("findings file: ")[1].splitlines()[0])
        pass_through(d, "9.5", "0", "trusted")
        if body is None:
            os.makedirs(stem)
        else:
            write_text(stem, body)
        label = f"{'report-only' if report_only else 'fix mode'}, {why} ({body!r})"
        r = run("project", "close", cwd=d)
        ok(r.returncode != 0 and why in r.stderr,
           f"{label}: close must refuse and say the file {why}: rc={r.returncode} "
           f"{r.stderr.strip()[:200]}")
        ok(os.path.exists(os.path.join(d, ".loop-review", "state.json")),
           f"{label}: the refusal must keep the loop state, the review's only other record")

        r = run("project", "close", "--force", cwd=d)
        row = read_state(d, "project.json")["areas"][0]
        ok(row["status"] == "incomplete" and row["findings_file"] is None
           and any(why in b for b in row["blockers"]),
           f"{label}: forced, it closes incomplete with no findings file on record: {row}")


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
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
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
    write_text(os.path.join(d, stem), "A. no actionable findings\nB. understood\n")
    ok(run("project", "close", cwd=d).returncode == 0, "a clean area with its file must close")
    area = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(area["status"] == "accepted" and area["findings_file"],
       f"and it must be recorded accepted with its file: {area['status']}, {area['findings_file']}")


@case
def an_area_whose_name_is_not_utf8_is_reviewed_like_any_other():
    """Paths come from git and argv as bytes; not all of them decode.

    `os.fsdecode` carries the undecodable ones as surrogates (PEP 383), and encoding those
    with the default utf-8 raises. It raised inside the findings-file slug, so an area with
    such a name did not fail a gate — `project next` died with a traceback, and the queue
    stopped on it. Every other path in the loop already handles these bytes; this one was
    the exception, so the case walks the whole area, not just the crash.
    """
    d = repo({"keep/a.py": "x\n"})
    area = os.fsdecode(b"pkg_\xff")
    os.makedirs(os.path.join(d, area))
    write_text(os.path.join(d, area, "a.py"), "x\n")
    sh("git add -A && git commit -qm add", d)
    run("project", "init", "--report-only", "--", area, cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True)
    ok("undecodable bytes" in out.stdout + out.stderr,
       f"the printed path is escaped, so the operator must be told: {(out.stdout + out.stderr)[:300]}")
    pass_through(d, "9.5", "0", "trusted")
    # Built through the script, not copied from its output: the printed form is escaped.
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    stem = os.path.join(".loop-review", "findings",
                        module().area_slug(led["areas"][0]) + ".md")
    write_text(os.path.join(d, stem), "A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok(led["areas"][0]["status"] == "reviewed" and led["areas"][0]["findings_file"],
       f"the area must close like any other: {led['areas'][0]['status']}, "
       f"{led['areas'][0]['findings_file']}")
    written = os.listdir(os.path.join(d, ".loop-review", "findings").encode())
    ok(any(b"\xff" in f for f in written),
       f"and its report must be on disk under the original bytes: {written}")


@case
def a_findings_file_is_never_hidden_by_a_leading_dot():
    """In report-only that file is the whole deliverable, so it has to be findable.

    The slug was the path with `/` flattened, so the repository root (`.`) produced
    `.-<hash>.md` and an area like `.github` produced `.github-<hash>.md` — dotfiles that a
    plain `ls`, a glob copy or an archive step skips, for exactly the artifact `project
    close` leaves behind after deleting the loop state.
    """
    d = repo({".github/w.yml": "on: push\n", "github/b.py": "x\n"})
    run("project", "init", "--report-only", "--", ".github", "github", cwd=d, check=True)
    first = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    ok(not os.path.basename(first).startswith("."),
       f"a dotted area must not yield a hidden findings file: {first}")
    write_text(os.path.join(d, first), "A. none\nB. understood\n")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    run("project", "close", cwd=d, check=True)
    second = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    ok(second != first, f"`.github` and `github` must stay distinct: {first} vs {second}")
    listed = [f for f in os.listdir(os.path.join(d, ".loop-review", "findings"))
              if not f.startswith(".")]
    ok(os.path.basename(first) in listed, f"the written report must show in a plain listing: {listed}")


@case
def the_repository_root_as_an_area_is_named_not_dotted():
    d = repo({"src/a.py": "x\n"})
    run("project", "init", "--report-only", "--", ".", cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = os.path.basename(out.split("findings file: ")[1].splitlines()[0])
    ok(stem.startswith("root-"), f"the root area must be named `root-<hash>`, got {stem}")


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
    write_text(os.path.join(d, stem), "x\n")
    ok(os.path.exists(os.path.join(d, stem)), "the findings file must be creatable")


@case
def a_findings_file_name_is_bounded_in_bytes_not_characters():
    """The filesystem's limit is 255 *bytes*; the stem was cut at 80 characters.

    80 emoji are 320 bytes, so an area of non-ASCII names got a findings path `open()`
    refuses with ENAMETOOLONG — and in report-only that file is the whole deliverable, and
    `project close` will not run without it.
    """
    names = ["\U0001F600" * 20 + str(i) for i in range(4)]       # 81 bytes each on disk
    d = repo({f"{n}/f.py": "x\n" for n in names})
    args = []
    for n in names:
        args += (["+"] if args else []) + [n]
    run("project", "init", "--report-only", "--", *args, cwd=d, check=True)
    out = run("project", "next", cwd=d, check=True).stdout
    stem = out.split("findings file: ")[1].splitlines()[0]
    base = os.fsencode(os.path.basename(stem))
    ok(len(base) + len(b".prev") <= 255,
       f"the findings name and its .prev rotation must fit 255 bytes, got {len(base)}")
    try:
        write_text(os.path.join(d, stem), "A. clean\n")
        pass_through(d, "9.5", "0", "trusted")
        r = run("project", "close", cwd=d)
        ok(r.returncode == 0, f"and the area must close on it: {r.stderr.strip()[:200]}")
    except OSError as e:
        ok(False, f"the findings file could not even be written: {e}")

    m = module()
    ok(m.stem_within("a" * 90, 80) == "a" * 80, "an ASCII stem is cut exactly where it was")
    for s in ("\u044f" * 50, "x" + "\U0001F600" * 30, "\udcff" * 90 + "\u044f" * 5):
        cut = m.stem_within(s, 80)
        raw = os.fsencode(cut)
        ok(len(raw) <= 80 and s.startswith(cut) and os.fsdecode(raw) == cut,
           f"a cut must fit the budget and never split a character: {cut!r}")


@case
def a_mistyped_path_inside_a_glued_area_is_reported():
    """Emptiness is asked of the area; a path inside one can still be invisible.

    `scope_is_empty` answers for the area as a whole, so in `pkg + non_exitent` the healthy
    neighbour covered the typo: the area queued, opened, and reached the reviewer naming a
    path it was told to read whole and cannot find. Task mode has warned about this since
    `init`; project mode had no `warn_unseen` at all, at either moment it could speak.
    """
    d = repo({"pkg/a.py": "x\n"})
    r = run("project", "init", "--report-only", "--", "pkg", "+", "non_exitent", cwd=d, check=True)
    ok("non_exitent" in r.stderr and "does not exist" in r.stderr,
       f"the queue must report the path git cannot see: {r.stderr.strip()[:200]}")
    r = run("project", "next", cwd=d, check=True)
    ok("non_exitent" in r.stderr,
       f"and so must the area's own opening, where the prompt is filled: {r.stderr.strip()[:200]}")

    # A path can also vanish between queueing and opening — that is the second moment.
    d = repo({"a/f.py": "x\n", "b/f.py": "y\n", "c/f.py": "z\n"})
    run("project", "init", "--report-only", "--", "a", "b", "+", "c", cwd=d, check=True)
    sh("git rm -q -r c && git commit -qm drop", d)
    r = run("project", "next", cwd=d, check=True)
    ok("`c`" not in r.stderr, f"the first area must not be warned about someone else's path: {r.stderr[:200]}")
    pass_through(d, "9", "0", "trusted")
    findings(d, "a")
    run("project", "close", cwd=d, check=True)
    r = run("project", "next", cwd=d, check=True)
    ok("`c`" in r.stderr and "does not exist" in r.stderr,
       f"a path deleted after queueing must be reported when its area opens: {r.stderr.strip()[:200]}")


@case
def a_glue_operator_missing_a_path_is_refused_wherever_it_sits():
    """`+` joins the path on its left to the one on its right — every position where one of
    those is missing is the same fault, and each was found separately.

    Leading was refused from the start; trailing was dropped in silence, queueing two areas
    where the operator asked for one; `+ +` glued the two paths around it, which is also what
    a command line that lost a path between them looks like. Each refusal is asserted by its
    reason, not by a non-zero exit: three different faults answered by one message would read
    as coverage while two of them went unchecked.
    """
    d = repo({"a/x.py": "1\n", "b/y.py": "2\n"})
    for args, reason in ((("a", "b", "+"), "cannot end the list"),
                         (("+", "a"), "cannot open the list"),
                         (("a", "+", "+", "b"), "is missing"),
                         (("a", "+", "+"), "is missing")):
        r = run("project", "init", "--", *args, cwd=d)
        ok(r.returncode != 0 and reason in r.stderr,
           f"`{' '.join(args)}` must be refused for `{reason}`: rc={r.returncode} {r.stderr.strip()[:120]}")
        ok(not os.path.exists(os.path.join(d, ".loop-review", "project.json")),
           f"`{' '.join(args)}`: a refused queue must not be written to the ledger")

    r = run("project", "init", "--", "a", "+", "b", cwd=d, check=True)
    ok(r.stdout.count("\n  - ") == 1, "the glued form must still queue one area")
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok(led["areas"][0]["paths"] == ["a", "b"], f"joined into one area: {led['areas'][0]['paths']}")
    r = run("project", "init", "--force", "--", "a", "b", cwd=d, check=True)
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok([x["paths"] for x in led["areas"]] == [["a"], ["b"]],
       f"and without `+` they stay separate: {[x['paths'] for x in led['areas']]}")


def call_arg(node, index, name):
    """An argument of a call, whether it was passed positionally or by keyword.

    A guard that reads `node.args[1]` alone sees `open(p, "rb")` and misses
    `open(p, mode="rb")` — and misses it in the direction that accuses correct code, which is
    how a check stops being trusted.
    """
    if len(node.args) > index:
        return node.args[index]
    return next((k.value for k in node.keywords if k.arg == name), None)


def bare_text_opens(src):
    """Lines in `src` where `open()` decodes by whatever locale the machine has.

    Extracted from the case below so the check itself can be exercised: a guard nothing
    tests is a claim about the codebase resting on one reading of the AST.
    """
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "open"):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        mode = call_arg(node, 1, "mode")
        if isinstance(mode, ast.Constant) and "b" in mode.value:
            continue
        out.append(node.lineno)
    return out


@case
def the_locale_guard_reads_a_keyword_mode_too():
    """The guard is checked against known-good and known-bad calls, not only against the repo.

    It inspected `node.args[1]` and nothing else, so `open(p, mode="rb")` — a binary read
    written the other legal way — was reported as decoding by the platform locale. Wrong in
    the accusing direction: the suite would have failed on correct code, and the next person
    would have learned to work around the check rather than trust it.
    """
    ok(bare_text_opens('open(p)') == [1], "a bare text open must be reported")
    ok(bare_text_opens('open(p, "r")') == [1], "an explicit text mode too")
    ok(bare_text_opens('open(p, "rb")') == [], "a positional binary mode is exempt")
    ok(bare_text_opens('open(p, mode="rb")') == [], "and so is a keyword binary mode")
    ok(bare_text_opens('open(p, encoding="utf-8")') == [], "an explicit encoding is exempt")
    ok(bare_text_opens('open(p, "w", encoding="utf-8")') == [], "in either argument order")


@case
def no_text_file_is_opened_at_the_mercy_of_the_platform_locale():
    """`open()` without `encoding=` decodes by whatever locale the machine has.

    The state files are the loop's whole memory and a brief or a reviewer note may hold any
    character, so reading them under a non-UTF-8 locale is a corrupted state rather than a
    clear error. The first pass at this fixed the call sites it happened to look at: four
    carried `encoding="utf-8"` while `load`, `load_project` and the atomic write did not.
    Binary mode is exempt — `fingerprint()` reads files as bytes precisely to avoid decoding.

    This suite is checked too, and for a sharper reason than tidiness: it is where the
    surrogate and non-UTF-8 paths are exercised, so its own fixtures reading `state.json`
    through an ASCII codec fail on content the product handles correctly — a green product
    reported as broken by its own tests. 48 of its 83 `open()` calls were bare, including
    `repo()` and `write()`, which every case goes through.
    """
    bare = []
    for rel in ("scripts/loop_review.py", "scripts/selftest.py"):
        bare += [f"{rel}:{n}" for n in bare_text_opens(read_text(os.path.join(ROOT, rel)))]
    ok(not bare, "text open() without encoding= at " + ", ".join(bare))


def declared_parameters(node):
    """Every parameter of a function definition, in every form the syntax allows.

    `args + kwonlyargs` is the pair people write from memory, and it silently omits
    positional-only parameters (`def fn(x, /)`) and the `*args`/`**kwargs` catch-alls. A
    dead parameter written in one of those forms escaped the check entirely — a guard that
    fails open, which is the kind that reads as coverage while covering nothing.
    """
    a = node.args
    named = a.posonlyargs + a.args + a.kwonlyargs
    return [x.arg for x in named] + [x.arg for x in (a.vararg, a.kwarg) if x]


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def scope_bindings(scope):
    """The names `scope` (a function, lambda or comprehension) binds locally.

    Parameters, assignment targets, `except ... as`, imports, and nested `def`/`class`
    names — collected from this scope only, never from a nested one, and minus `global` /
    `nonlocal` declarations. A comprehension binds its loop targets and nothing else.
    """
    if isinstance(scope, COMPS):
        return {t.id for g in scope.generators for t in ast.walk(g.target)
                if isinstance(t, ast.Name)}
    names, shared = set(declared_parameters(scope)), set()
    body = [scope.body] if isinstance(scope, ast.Lambda) else scope.body

    def collect(n):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((x.asname or x.name.split(".")[0]) for x in n.names)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            shared.update(n.names)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            return                    # its body is its own scope; its header is not ours
        if isinstance(n, FUNCS + COMPS):
            return
        for c in ast.iter_child_nodes(n):
            collect(c)

    for stmt in body:
        collect(stmt)
    return names - shared


def resolve_loads(tree, skip=None):
    """`(module_loads, local_loads)`: every `Name` load, resolved the way Python does.

    `module_loads` holds names no enclosing function, lambda or comprehension binds — reads
    of module-level things such as helpers. `local_loads` maps each such scope to the names
    of its own bindings read there, nested scopes included. Defaults, decorators and
    annotations are evaluated outside the function they belong to, and a comprehension's
    first iterable outside the comprehension, so they are resolved there. `skip` is a
    subtree left out entirely (a helper's own body, so recursion is no caller).
    """
    module, local = set(), {}

    def load(name, chain):
        for scope, bound in reversed(chain):
            if name in bound:
                local.setdefault(scope, set()).add(name)
                return
        module.add(name)

    def visit(n, chain):
        if n is skip:
            return
        if isinstance(n, FUNCS):
            a = n.args
            outside = list(a.defaults) + [d for d in a.kw_defaults if d is not None]
            if not isinstance(n, ast.Lambda):
                outside += list(n.decorator_list) + ([n.returns] if n.returns else [])
                outside += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs
                            + [x for x in (a.vararg, a.kwarg) if x] if x.annotation]
            for c in outside:
                visit(c, chain)
            inner = chain + [(n, scope_bindings(n))]
            for c in ([n.body] if isinstance(n, ast.Lambda) else n.body):
                visit(c, inner)
            return
        if isinstance(n, COMPS):
            visit(n.generators[0].iter, chain)
            inner = chain + [(n, scope_bindings(n))]
            for i, g in enumerate(n.generators):
                if i:
                    visit(g.iter, inner)
                for c in g.ifs:
                    visit(c, inner)
            for c in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
                visit(c, inner)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            load(n.id, chain)
        for c in ast.iter_child_nodes(n):
            visit(c, chain)

    visit(tree, [])
    return module, local


def names_loaded(node, skip=None):
    """Names read as module-level names under `node`: never attributes, assignment targets,
    or a local of the same name in some function.

    Both static guards counted `n.attr` and every `ast.Name`, so an unused parameter called
    `path` was "read" by `os.path`, and a dead helper called `strip` was "called" by any
    `.strip()` in the file. `resolve_loads` is the one place a read is decided.
    """
    return resolve_loads(node, skip)[0]


def unread_parameters(src):
    """`name(param) at line N` for every parameter its own body never reads.

    Extracted so the check can be exercised on known cases rather than only on this
    repository, where whichever forms happen to appear decide what it is proved to catch.
    """
    stale = []
    tree = ast.parse(src)
    locals_by_scope = resolve_loads(tree)[1]
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        # Reads that resolve to *this* function's binding: not an attribute, not a shadowing
        # parameter or comprehension variable in a nested scope, not the header (defaults
        # and decorators are evaluated outside the function).
        read = locals_by_scope.get(node, set())
        for arg in declared_parameters(node):
            if arg in read or (arg == "a" and node.name.startswith("cmd_")):
                continue
            stale.append(f"{node.name}({arg}) at line {node.lineno}")
    return stale


@case
def the_unread_parameter_guard_sees_every_parameter_form():
    """Checked against known cases, because the repository only proves what it happens to use.

    The guard read `args + kwonlyargs`, so a dead positional-only parameter or an unused
    `*args` was invisible to it — it failed open, and a guard that fails open reads as
    coverage while covering nothing. Every form the syntax allows is asserted here, together
    with the one exemption, so neither can quietly go missing.
    """
    ok(unread_parameters("def f(x):\n    return 1"), "a plain unused parameter must be caught")
    ok(not unread_parameters("def f(x):\n    return x"), "a used one must not be")
    ok(unread_parameters("def f(x, /):\n    return 1"), "positional-only counts too")
    ok(unread_parameters("def f(*, x):\n    return 1"), "keyword-only counts too")
    ok(unread_parameters("def f(*rest):\n    return 1"), "an unused *args counts too")
    ok(unread_parameters("def f(**kw):\n    return 1"), "an unused **kwargs counts too")
    ok(not unread_parameters("def f(*rest):\n    return rest"), "a used *args must not be")
    ok(not unread_parameters("def cmd_x(a):\n    return 1"),
       "the dispatcher's `a.fn(a)` contract stays exempt")
    ok(unread_parameters("import os\ndef f(path):\n    return os.path.sep"),
       "an attribute of the same name is not a read of the parameter")
    ok(unread_parameters("def f(x):\n    x = 1\n    return 2"),
       "assigning a parameter is not reading it")
    ok(not unread_parameters("def f(x):\n    def g():\n        return x\n    return g"),
       "a closure that reads it does")
    ok(unread_parameters("def f(x):\n    return [x for x in (1, 2)]"),
       "a comprehension variable of the same name is not the parameter")
    ok(unread_parameters("def f(x):\n    def g(x):\n        return x\n    return g"),
       "a nested function's own parameter of the same name is not the parameter")
    ok(unread_parameters("x = 1\ndef f(x, y=x):\n    return y") == ["f(x) at line 2"],
       "a default is evaluated outside the function: it reads the module's x")
    ok(unread_parameters("x = 1\n@x\ndef f(x):\n    return 1"),
       "so is a decorator")


@case
def no_helper_declares_a_parameter_it_never_reads():
    """A parameter nobody reads is a claim about the code that nothing enforces.

    `findings_are_current(ffile, area)` kept its `area` long after the digest matching that
    used it was replaced by the `.prev.md` rotation, so both the signature and every call
    site said the check was tied to the area object while it was a plain existence test.
    Command functions are exempt: the dispatcher calls every one as `a.fn(a)`, so `a` is
    part of the contract whether that command needs it or not.
    """
    stale = unread_parameters(read_text(os.path.join(ROOT, "scripts", "loop_review.py")))
    ok(not stale, "parameter(s) never read: " + "; ".join(stale))


@case
def the_script_carries_no_unreachable_helper():
    """A helper nobody calls is a claim about the workflow that nothing enforces.

    `findings_digest` outlived the rule it implemented: the digest comparison was replaced by
    the `.prev.md` rotation, and the function stayed behind for releases — read as live
    machinery by anyone auditing how the findings gate works, while the real gate was a plain
    existence check somewhere else. Names referenced only from `selftest.py` count as live;
    everything else must be reachable from within the script.
    """
    src = read_text(os.path.join(ROOT, "scripts", "loop_review.py"))
    # The entry point is called by the interpreter, and selftest drives the CLI through these.
    live = {"main", "build_parser"}
    live |= set(re.findall(r"module\(\)\.([A-Za-z_]\w*)",
                           read_text(os.path.join(ROOT, "scripts", "selftest.py"))))
    dead = unreachable_helpers(src, live)
    ok(not dead, "unreachable helper(s): " + "; ".join(f"{n} at line {l}" for n, l in dead))


def unreachable_helpers(src, live=()):
    """`(name, line)` for each top-level function nothing outside its own body reads."""
    tree = ast.parse(src)
    dead = []
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        if fn.name not in live and fn.name not in names_loaded(tree, skip=fn):
            dead.append((fn.name, fn.lineno))
    return sorted(dead)


@case
def the_unreachable_helper_guard_sees_through_names_that_merely_match():
    """Known cases: the guard counted every attribute and every name in the module.

    A dead helper called itself, or shared its name with a method (`.strip()`) or a local
    variable elsewhere (`state = load()`), and was "used" for good.
    """
    dead = lambda src: [n for n, _ in unreachable_helpers(src)]
    ok(dead("def f():\n    return 1") == ["f"], "an uncalled helper is caught")
    ok(dead("def f():\n    return 1\ndef main():\n    return f()") == ["main"],
       "a called one is not")
    ok("f" in dead("def f(n):\n    return f(n - 1)"), "recursion alone is no caller")
    ok("strip" in dead("def strip():\n    return 1\ndef g(s):\n    return s.strip()"),
       "a method of the same name is no caller")
    ok("state" in dead("def state():\n    return 1\ndef g():\n    state = 2\n    return state"),
       "a local variable of the same name is no caller")
    ok("cb" not in dead("def cb():\n    return 1\ndef g(h=cb):\n    return h"),
       "a reference as a value, not only a call, keeps it live")
    live = lambda src: [n for n, _ in unreachable_helpers(src, {"g"})]
    ok(live("def f():\n    return 1\ndef g(xs):\n    a = f()\n    return a, [f for f in xs]") == [],
       "a comprehension variable elsewhere in the caller does not hide the call")
    ok(live("def f():\n    return 1\ndef g():\n    a = f()\n    def h():\n        f = 2\n        return f\n    return a, h") == [],
       "nor does a nested function's local of the same name")
    ok(live("def cb():\n    return 1\ndef g(cb=cb):\n    return cb") == [],
       "a default naming the helper reads the module's helper, even when a parameter shadows it")
    ok(live("def cb():\n    return 1\ndef g(h=cb):\n    cb = 1\n    return h") == [],
       "and a local assigned later in the body does not change that")
    ok("f" in dead("def f():\n    return 1\ndef g():\n    try:\n        pass\n    except Exception as f:\n        return f"),
       "an `except ... as` name is a local, no caller")
    ok("f" in dead("def f():\n    return 1\ndef g():\n    import os as f\n    return f"),
       "so is an imported name")
    ok("f" in dead("def f():\n    return 1\ng = lambda *f: f"),
       "and a lambda's *args")


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


def cli_surface(p, prefix=""):
    """`{subcommand: its flags}` for every leaf subcommand of `p`, flags or not.

    It recorded a subcommand only when it had a flag besides `-h`, so `scope`, `accept`,
    `fingerprint` and `project next` were never in it — and "not described at all", asked
    only of what it held, could not be asked of them.
    """
    out, subs = {}, [a for a in p._actions if a.__class__.__name__ == "_SubParsersAction"]
    for act in subs:
        for name, sub in act.choices.items():
            out.update(cli_surface(sub, (prefix + " " + name).strip()))
    if not subs and prefix:                          # a leaf: a command one can run
        out[prefix] = {o for a in p._actions for o in a.option_strings
                       if o not in ("-h", "--help")}   # `-h` alone is not a documented flag
    return out


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
    """Every invocation the script *tells the operator to run*: every string it can print.

    A message naming a command that does not exist is the same dead end as prose naming one,
    and it is worse hidden: `cmd_project_close` advised `reset --force` for months, a flag
    `reset` never had, while the only other route out of that state was refused as well.
    Messages are prose too — they are read by the agent and are part of the CLI's contract.

    Every string constant in the module, not the arguments of die()/print() alone. Most
    messages are built before they are printed — the reasons `blockers()` and
    `review_integrity()` return, the `tail` and `advice` of `validate-drop`, the hints passed
    as arguments — and that scan saw 49 of 113 recommendations; `amend --test-evidence ...`
    in an acceptance blocker was among the unseen. Docstrings are the exception: they explain
    the code, quoting past dead ends by name, and `--help` has a case of its own.
    """
    return [(f"scripts/loop_review.py:{n}", raw, body) for n, raw, body in
            string_hits(read_text(os.path.join(ROOT, "scripts", "loop_review.py")))]


def string_hits(src):
    """`(line, text, body)` for every invocation in the printable strings of `src`."""
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    out = []
    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for node in ast.walk(tree):
        # An f-string is read whole, its interpolations as `ARG`. Read as separate constants
        # — the old way — `validate-drop -- {cmd}` came apart into "`validate-drop -- " and
        # "`", no piece held a backtick pair, and the command was never parsed.
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "ARG" for v in node.values)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings and id(node) not in in_fstring):
            text = node.value
        else:
            continue
        for body in command_hits(text):
            out.append((node.lineno, text.strip(), body))
    return out


def invocations():
    """Every invocation this skill tells someone to run, from every place it says so.

    The prose, the evals' prompts and expected outputs, and every string the script can
    print — one list, so a new source of recommendations is added here once rather than
    remembered by each check. Each hit goes to the real parser; nothing is matched as text.
    """
    return doc_command_lines() + script_message_lines()


# A value a message may leave as a placeholder (`...`, or nothing after the flag), filled in
# with one the parser accepts. A word or a number satisfies a free-text or typed flag; a flag
# with `choices` rejects both, so it needs its own — and a flag missing from this table reads
# as a broken recommendation, which is the safe direction.
PLACEHOLDERS = {"--test-evidence": "trusted"}


def prose_hits(text):
    """`(line, body)` for every invocation in a document's text."""
    text = text.replace("\\\n", " ")                         # fenced line continuations
    # An inline span wrapped across a line — `project close\n  --force` — was cut in two by
    # splitlines(), no half held a backtick pair, and the flag went unparsed. Spans are
    # matched in order from the start, as Markdown pairs them, fenced blocks skipped, and a
    # line break is collapsed only *inside* one. A regex joining "`…\n…`" anywhere paired
    # the closing backtick of one span with the opening one of the next, hiding exactly
    # the wrapped span it was for; a blank line ends a paragraph, so it ends no span.
    def join(m):
        body = m.group(1)
        if body is None or re.search(r"\n\s*\n", body):
            return m.group(0)
        return "`" + re.sub(r"\s*\n\s*", " ", body) + "`"
    text = re.sub(r"```.*?```|(?<!`)`([^`]+)`(?!`)", join, text, flags=re.S)
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        out += [(raw.strip(), body) for body in command_hits(line)]
    return out


def flags_named(entry):
    """The flags a help entry names, as whole tokens — `--task-brief-file` is not `--task-brief`."""
    return set(re.findall(r"(?<![\w-])--[a-z][\w-]*", entry))


def doc_command_lines():
    """Every loop_review invocation printed in the normative prose, normalised for parsing.

    Placeholders are made concrete (`<paths...>` -> a word, `a|b` -> `a`, `[--flag v]` ->
    `--flag v`) so the line can be handed to the real parser: what is being checked is that
    the subcommand and its flags exist, not that the example values are meaningful.
    """
    out = []
    for rel in DOCS:
        out += [(rel, raw, body) for raw, body in prose_hits(read_text(os.path.join(ROOT, rel)))]
    # The evals are the agent's behavioural spec and name the CLI as densely as the prose;
    # an expected output that names a flag the CLI lacks grades the agent against fiction.
    out += evals_hits(json.loads(read_text(os.path.join(ROOT, "evals", "evals.json")))["evals"])
    return out


def evals_hits(evals):
    """Invocations in the evals' prompts and expected outputs, read as any document is.

    Through prose_hits: line by line, a wrapped span joined. Handed to command_hits whole,
    a span's whitespace class ran across a line break and swallowed the next line into it.
    """
    return [(f"evals/evals.json#{e.get('id')}.{field}", raw, body)
            for e in evals for field in ("prompt", "expected_output")
            for raw, body in prose_hits(e.get(field, ""))]


def assert_invocations_parse(lines, failures=None):
    """Hand every named invocation to the real parser; collect what it will not accept.

    Failures go to `failures` (the suite's list by default), so a case can run it on planted
    lines without editing the global list it is itself being judged by.
    """
    failures = FAILURES if failures is None else failures
    parser = module().build_parser()
    for rel, raw, body in lines:
        try:
            tokens = shlex.split(body)
        except ValueError:
            failures.append(f"{rel}: unparseable command line: {raw}")
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
                failures.append(f"{rel}: `{raw}` names no such subcommand")
            continue
        # A placeholder is `...`, `ARG` (from `<...>` or an f-string interpolation) or an
        # all-caps word as the help writes them (`--findings N`). After a flag with choices it
        # takes that flag's accepted value, whichever form it has; anywhere else it is also
        # tried as a number. The CLI has no upper-case choice or subcommand to mistake.
        def placeholder(t):
            return t == "..." or re.fullmatch(r"[A-Z][A-Z_]*", t) is not None
        tokens = [PLACEHOLDERS.get(tokens[i - 1], t) if i and placeholder(t) else t
                  for i, t in enumerate(tokens)]
        fill = [PLACEHOLDERS[tokens[-1]]] if tokens[-1] in PLACEHOLDERS else ["ARG"]
        numbered = ["1" if placeholder(t) else t for t in tokens]
        for attempt in (tokens, tokens + fill, tokens + ["1"], numbered, numbered + fill,
                        numbered + ["1"]):
            buf = _io.StringIO()
            try:
                with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                    parser.parse_args(attempt)
                break
            except (SystemExit, argparse.ArgumentError):
                last = buf.getvalue().strip().splitlines()[-1:] or [""]
        else:
            failures.append(f"{rel}: `{raw}` is not accepted by the CLI ({last})")


@case
def every_invocation_from_every_source_exists_in_the_cli():
    """Prose, evals and the script's own messages, through one list and one parser.

    The prose is executed by a model, and a flag it names that the CLI lacks is a dead end
    exercising the script cannot catch. A refusal is the same: `project close` sent the
    operator to `reset --force`, which `reset` has never accepted, while the plain `reset`
    it also implied was refused mid-area — the dead end was quoted in a die() string, not in
    a document. Two cases read two sources each; a source added to `invocations()` is
    checked here without anyone remembering to.
    """
    lines = invocations()
    seen = {}
    for src, _, _ in lines:
        kind = src.split(":")[0].split("#")[0]
        kind = "script" if kind == "scripts/loop_review.py" else "evals" if kind.startswith("evals") else "prose"
        seen[kind] = seen.get(kind, 0) + 1
    ok(seen.get("prose", 0) >= 12 and seen.get("script", 0) >= 10 and seen.get("evals", 0) >= 5,
       f"every source must contribute to the command surface: {seen}")
    assert_invocations_parse(lines)


def refused_for_a_missing_argument(r):
    """True only when a recommended command stopped for an argument the message could not
    carry — never for a flag it does not have, a value it rejects, or a gate.

    Exit 2 was read as "missing argument" outright, but three different things exit 2:
    argparse's missing argument (fine), argparse's unknown flag or bad value (the exact
    `reset --force` dead end these runners exist to catch), and `NO_CHANGES`, a gate. Ask
    the message instead: argparse's own wording for a missing argument, or a `die()` that
    says the command "needs" one.
    """
    err = r.stderr or ""
    if r.returncode == 2 and re.search(
            r"error: (the following arguments are required|argument \S+: expected "
            r"(one|at least one) argument)", err):
        return True
    first = (err.strip().splitlines() or [""])[0]
    return r.returncode == 1 and first.startswith("loop-review: ") and " needs " in first


def stuck_states():
    """Build each state whose refusal names a way out, and return what it printed.

    Each entry is `(label, repo, failing invocation, stderr)`. Parsing exists in the case
    below; building the states is here so the list reads as what it is — the recoveries this
    skill promises an operator who is stuck.
    """
    out = []

    d = repo({"a/f.py": "x\n", "b/g.py": "y\n"})              # loop state lost mid-area
    run("project", "init", "--", "a", "b", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    os.remove(os.path.join(d, ".loop-review", "state.json"))
    out.append(("project next, state lost", d, ("project", "next"),
                run("project", "next", cwd=d).stderr))

    d = repo({"a/f.py": "x\n", "b/g.py": "y\n"})              # the open loop is not this area's
    run("project", "init", "--", "a", "b", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.loads(read_text(sf))
    st["area"] = st["scope"] = ["b"]                   # a loop belonging to another area
    write_text(sf, json.dumps(st, indent=2))
    out.append(("project close, foreign loop", d, ("project", "close"),
                run("project", "close", cwd=d).stderr))

    d = repo({"src/a.py": "1\n"})                              # a pass already recorded
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    out.append(("pass-record, already recorded", d,
                ("pass-record", "--score", "9", "--findings", "0", "--test-evidence", "trusted"),
                run("pass-record", "--score", "9", "--findings", "0",
                    "--test-evidence", "trusted", cwd=d).stderr))

    d = repo({"src/a.py": "1\n"})                              # a command that never ran
    write(d, "src/a.py", "2\n")
    run("init", "--", "src", cwd=d, check=True)
    run("validate", "--", "definitely-not-a-command", cwd=d)
    out.append(("pass-start, never-ran check", d, ("pass-start",),
                run("pass-start", cwd=d).stderr))

    # `project init` / `project next` with a state.json in every relation to the ledger. Both
    # refused on the file's existence and named `project close`, which refuses unless an
    # area is in progress and the loop is its own.
    def queue(*extra):
        d = repo({"a/f.py": "x\n", "b/g.py": "y\n"})
        run("project", "init", *extra, "--", "a", "b", cwd=d, check=True)
        return d

    def close_first(d):
        run("project", "next", cwd=d, check=True)
        pass_through(d, "9.5", "0", "trusted")
        findings(d, "a", "A. clean\n")
        run("project", "close", cwd=d, check=True)

    d = queue("--report-only")                                  # a loop `init` made between areas
    close_first(d)
    write(d, "b/g.py", "z\n")
    run("init", "--", "b/g.py", cwd=d, check=True)
    out.append(("project next, loop outside the queue", d, ("project", "next"),
                run("project", "next", cwd=d).stderr))

    d = queue()                                                 # foreign loop left by close --force
    run("project", "next", cwd=d, check=True)
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.loads(read_text(sf))
    st["area"] = st["scope"] = ["b"]
    write_text(sf, json.dumps(st, indent=2))
    run("project", "close", "--force", cwd=d, check=True)
    out.append(("project next, foreign loop left behind", d, ("project", "next"),
                run("project", "next", cwd=d).stderr))

    d = queue()                                                 # a foreign loop, area in progress
    run("project", "next", cwd=d, check=True)
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.loads(read_text(sf))
    st["area"] = st["scope"] = ["b"]
    write_text(sf, json.dumps(st, indent=2))
    out.append(("project next, foreign loop mid-area", d, ("project", "next"),
                run("project", "next", cwd=d).stderr))

    for label, cmd in (("project next, own loop not closed", ("project", "next")),
                       ("project init --force, own loop not closed",
                        ("project", "init", "--force", "--", "a"))):
        d = queue("--report-only")                              # the area's own loop, finished
        run("project", "next", cwd=d, check=True)
        pass_through(d, "9.5", "0", "trusted")
        findings(d, "a", "A. clean\n")
        out.append((label, d, cmd, run(*cmd, cwd=d).stderr))

    d = repo({"a/f.py": "x\n"})                                 # a plain loop, no ledger at all
    write(d, "a/f.py", "y\n")
    run("init", "--", "a", cwd=d, check=True)
    out.append(("project init, plain loop open", d, ("project", "init", "--", "a"),
                run("project", "init", "--", "a", cwd=d).stderr))
    return out


@case
def only_a_missing_argument_excuses_a_recommended_command():
    """Exit 2 is three different things; only one of them is a missing argument.

    Both recovery runners accepted any exit 2, which also covers argparse rejecting a flag
    that does not exist — the `reset --force` dead end they were written to catch — and the
    `NO_CHANGES` gate. Each kind is produced for real here and classified.
    """
    d = repo()
    write(d, "a.py", "v2\n")
    run("init", "--", "a.py", cwd=d, check=True)
    cases = (
        (("reset", "--force"), False, "an unknown flag is a dead end"),
        (("amend", "--test-evidence", "maybe"), False, "a rejected value is a dead end"),
        (("amend", "--test-evidence"), True, "a flag left without its value is incomplete"),
        (("scope",), True, "a die() that says the command needs an argument is incomplete"),
    )
    for args, excused, label in cases:
        r = run(*args, cwd=d)
        ok(r.returncode != 0 and refused_for_a_missing_argument(r) is excused,
           f"{label}: `{' '.join(args)}` rc={r.returncode} {r.stderr.strip()[-120:]}")
    sh("git checkout -q -- a.py", d)                             # the scope empties
    run("validate", "--", "true", cwd=d)
    r = run("pass-start", cwd=d)
    ok(r.returncode == 2 and not refused_for_a_missing_argument(r),
       f"the NO_CHANGES gate is a refusal, not a missing argument: {r.stderr.strip()[:120]}")


@case
def project_init_force_never_discards_a_ledger_whose_area_lost_its_loop():
    """`project init --force` asked about an open `state.json` only.

    With the area's loop state lost, nothing stood in the way: the ledger, with every
    finished area's outcome, was replaced by a fresh queue — what evals 14 and 15 forbid,
    since `project close --force` is the recovery that keeps them. The refusal must name a
    way out that works there, and starting over must still be possible on purpose.
    """
    d = repo({"a/x.py": "1\n", "b/y.py": "2\n"})
    run("project", "init", "--report-only", "--", "a", "b", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    pass_through(d, "9.5", "0", "trusted")
    findings(d, "a", "A. clean\n")
    run("project", "close", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)                   # `b`, then its state is lost
    os.remove(os.path.join(d, ".loop-review", "state.json"))
    r = run("project", "init", "--force", "--", "a", "b", cwd=d)
    led = read_state(d, "project.json")
    ok(r.returncode != 0 and "loop state is gone" in r.stderr and "project close --force" in r.stderr,
       f"init --force must refuse over a stuck area: rc={r.returncode} {r.stderr.strip()[:200]}")
    ok(led["areas"][0]["status"] == "reviewed", "and leave the finished area's outcome in place")
    ok(run("project", "close", "--force", cwd=d).returncode == 0
       and run("project", "next", cwd=d).returncode == 0,
       "the named recovery works there and the queue continues")

    r = run("project", "init", "--force", "--", "a", "b", cwd=d)  # deliberate start-over
    ok(r.returncode == 0 and "discarded 2 recorded area outcome(s)" in r.stderr,
       f"a deliberate --force still starts over, and says what it cost: {r.stderr.strip()[:200]}")


@case
def a_recommended_recovery_works_in_the_state_that_printed_it():
    """Parsing a recommendation only proves the flags exist. Running it proves it is a way out.

    `project next` sent an operator whose loop state was lost to `project close`, which is
    refused for precisely that state a few lines into `project close` — and to
    `project init --force`, which rebuilds the queue and discards every outcome already
    earned. Both parse. This case takes the recommendation out of the message the script
    actually printed and runs it there, so the text and the behaviour are checked together.
    """
    for label, d, failing, err in stuck_states():
        ok(err.strip(), f"{label}: the refusal must say something")
        named = [b for b in command_hits(err) if b.split()[0] in
                 ("init", "validate", "validate-drop", "pass-start", "pass-abort", "pass-record",
                  "amend", "resolve", "accept", "status", "scope", "brief", "reset", "project")]
        ok(named, f"{label}: the refusal names no way out: {err.strip()[:200]}")
        # The first named invocation is the one an operator follows. Later ones are
        # alternatives or warnings ("`reset` here would discard the outcome instead").
        tokens = shlex.split(named[0])
        r = run(*tokens, cwd=d)
        # A message may name a command whose arguments belong to the operator's situation,
        # not to the message — "retract the record with `validate-drop`" cannot carry the
        # command being retracted. Refusal for a *missing argument* is therefore allowed;
        # refusal by a gate is not, and that is the whole difference between a way out and
        # a detour through a certain "no".
        ok(r.returncode == 0 or refused_for_a_missing_argument(r),
           f"{label}: `{' '.join(tokens)}` is named as the way out but the state refuses it: "
           f"{(r.stderr or r.stdout).strip()[:200]}")


@case
def every_command_an_acceptance_blocker_names_runs_in_that_state():
    """A blocker's recovery is checked by running it where `accept` printed it.

    `blockers()` went on judging the last recorded pass after a newer one was opened, and
    its reasons carried recoveries — `amend --test-evidence`, `resolve` — that
    `require_recorded_pass` refuses while a pass is open. Parsing them proved only that the
    flags exist. Each state below is built, `accept` is asked, and every command its reasons
    name is run in a fresh copy of that state; a gate refusing it is a dead end.
    """
    def loop(findings="0", evidence=("--test-evidence", "trusted")):
        d = repo()
        write(d, "a.py", "v2\n")
        run("init", "--", "a.py", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "8", "--findings", findings, "--understood",
            *evidence, cwd=d, check=True)
        return d

    states = []
    d = loop(evidence=())                                       # verdict missing
    states.append(("no test-evidence verdict", d))
    d = loop("1")                                                # "fixed" with no edit
    run("resolve", "--fixed", "1", cwd=d, check=True)
    states.append(("fix recorded on the reviewed state", d))
    d = loop("1", ())                                            # pass 1 superseded by pass 2
    run("resolve", "--fixed", "1", cwd=d, check=True)
    run("pass-start", "--force", cwd=d, check=True)
    states.append(("a newer pass open", d))
    superseded = d

    fill = {"--test-evidence": "trusted", "--reason": "selftest"}
    seen = 0
    for label, d in states:
        out = run("accept", cwd=d)
        ok(out.returncode != 0, f"{label}: the fixture must be refused")
        for named in [c for line in out.stdout.splitlines() for c in command_hits(line)]:
            tokens = shlex.split(named)
            tokens = [fill.get(tokens[i - 1], t) if t in ("...", "ARG") else t
                      for i, t in enumerate(tokens)]
            # `command_hits` strips a trailing `...` with the punctuation, leaving the flag
            # bare; argparse would then exit 2 and the dead end would pass as "needs an
            # argument". Give the flag its value so the gate is what answers.
            if tokens[-1] in fill:
                tokens.append(fill[tokens[-1]])
            copy = tempfile.mkdtemp(prefix="loop-selftest-")
            REPOS.append(copy)
            shutil.copytree(d, copy, dirs_exist_ok=True)
            r = run(*tokens, cwd=copy)
            seen += 1
            ok(r.returncode == 0 or refused_for_a_missing_argument(r),
               f"{label}: accept names `{named}` but this state refuses it: "
               f"{(r.stderr or r.stdout).strip()[:200]}")
    ok(seen >= 2, f"the fixtures must name recoveries to run, found {seen}")

    out = run("accept", cwd=superseded).stdout
    ok("pass 2 opened" in out and "pass 1" not in out and "unresolved" not in out
       and "recorded fixes" not in out,
       f"with pass 2 open, pass 1 is no longer what accept judges: {out.strip()[:300]}")


def docstring_signatures():
    """The module docstring's `Commands:` block, as `{command: its whole entry}`.

    An entry starts at a line naming a command and runs until the next one, so a signature
    wrapped across lines is read whole rather than truncated at the first.
    """
    doc = read_text(LOOP).split('"""')[1].split("Commands:")[1]
    entries, cur = {}, None
    for raw in doc.splitlines():
        name = next((c for c in sorted(SUBCOMMANDS, key=len, reverse=True)
                     if raw.startswith("  " + c) and not raw.startswith("   ")), None)
        if name:
            cur = name
            entries[cur] = entries.get(cur, "") + raw
        elif cur:
            entries[cur] += " " + raw.strip()
    return entries


@case
def the_builtin_help_lists_every_flag_the_parser_accepts():
    """`--help` is the CLI's own description of itself, and it drifted from the parser.

    `--force` exists on `init`, `pass-start`, `project init` and `project close`, and the
    docstring showed it on none of them — while showing it on `validate-drop`, so the block
    read as deliberate. A flag the help omits is a capability the agent cannot know it has,
    and `--force` is the audit surface AGENTS.md insists on keeping complete.
    """
    entries = docstring_signatures()
    parser = module().build_parser()

    declared = cli_surface
    # The hand-kept list the prose checks recognise commands by is the parser's, exactly:
    # a subcommand missing from it is one no document or message is ever checked for.
    ok(set(SUBCOMMANDS) == set(declared(parser)),
       f"SUBCOMMANDS must be the parser's subcommands: only in SUBCOMMANDS "
       f"{sorted(set(SUBCOMMANDS) - set(declared(parser)))}, only in the parser "
       f"{sorted(set(declared(parser)) - set(SUBCOMMANDS))}")

    missing, invented = [], []
    surface = declared(parser)
    for cmd, flags in sorted(surface.items()):
        entry = entries.get(cmd)
        if entry is None:
            missing.append(f"{cmd}: not described at all")
            continue
        # Flags as tokens, never substrings: `--task-brief` was "described" by
        # `--task-brief-file`, so either could vanish from the help with this check green.
        named = flags_named(entry)
        missing += [f"{cmd}: {flag}" for flag in sorted(flags - named)]
        # And the other way: a flag the help shows that the parser does not accept.
        invented += [f"{cmd}: {flag}" for flag in sorted(named - flags - {"--help"})]
    ok(not missing, "flags the parser accepts and the help omits: " + "; ".join(missing))
    ok(not invented, "flags the help shows and the parser rejects: " + "; ".join(invented))

    # The passport's escape-hatch list is the same claim in prose; keep it honest too — the
    # list itself, not the paragraph around it, which names `reset` for another reason.
    passport = read_text(os.path.join(ROOT, "AGENTS.md"))
    hatches = passport.split("Escape hatches stay explicit")[1]
    listed = set(re.findall(r"`([a-z][\w -]*)`",
                            re.search(r"`--force` exists on (.*?);", hatches, re.S).group(1)))
    forced = {cmd for cmd, flags in surface.items() if "--force" in flags}
    ok(listed == forced, f"AGENTS.md's --force list must be the parser's: listed {sorted(listed)}, "
                         f"parser {sorted(forced)}")


@case
def the_invocation_collector_sees_every_form_a_recommendation_takes():
    """Known cases for the extraction the CLI checks rest on: each form was a blind spot.

    A span wrapped across a line, a command inside an f-string with an interpolation, a
    flag "named" only as part of a longer one — each passed the checks unseen, so a dead
    flag there could not fail them. The evals are a source too, and the first run of the
    collector over them found `--findings N`, a placeholder the parse could not yet read.
    """
    bodies = [b for _, b in prose_hits("close it with `project close\n   --frce` instead")]
    ok("project close --frce" in bodies, f"a span wrapped across a line is one command: {bodies}")
    hits = [b for _, _, b in string_hits('def f(c):\n    die(f"use `validate-drop -- {c} --forse`")')]
    ok(any(b.startswith("validate-drop -- ARG --forse") for b in hits),
       f"a command inside an f-string is read whole: {hits}")
    ok("--task-brief" not in flags_named("init [--task-brief-file PATH]"),
       "a longer flag does not name a shorter one")
    ok(any(src.startswith("evals/evals.json#") for src, _, _ in invocations()),
       "the evals are among the sources")
    found = []
    assert_invocations_parse([("planted", "x", "pass-record --findings N"),
                              ("planted", "y", "resolve --fixed ARG"),
                              ("planted", "v", "amend --test-evidence VERDICT"),
                              ("planted", "w", "amend --test-evidence ARG"),
                              ("planted", "z", "reset --frce")], found)
    ok(len(found) == 1 and "`z`" in found[0],
       f"placeholders parse wherever they stand, a dead flag does not: {found}")
    bodies = [b for _, b in prose_hits("Never `reset` inside an area: `project close` frees it.\n"
                                       "If it is foreign — `project\nclose --frce` settles it, "
                                       "and `reset` then\nfrees it.")]
    ok("project close --frce" in bodies,
       f"a wrapped span after a line with other spans is still joined: {bodies}")
    found = [b for _, _, b in evals_hits([{"id": 0, "prompt": "Run `project close\n  --frce` "
                                           "and then `init`\nlater text `x`"}])]
    ok("project close --frce" in found and not any("later" in b for b in found),
       f"an eval field is read line by line, a wrapped span joined: {found}")


@case
def the_second_passport_name_stays_a_link_never_a_copy():
    """One passport, two runtime names — and the difference is load-bearing.

    Claude Code reads `CLAUDE.md`, Codex reads `AGENTS.md`, and a symlink answers both from
    one file. Materialise it into a real copy and the two drift the first time either is
    edited; it also makes the duplication read as a defect to anyone diffing contents, which
    is how the same non-issue was filed three times. The mode in the index is the fact — the
    working tree may not support links at all.
    """
    entry = subprocess.run(["git", "ls-files", "-s", "CLAUDE.md"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    ok(entry and entry[0] == "120000",
       f"CLAUDE.md must be a symlink in the index, got mode {entry[0] if entry else 'missing'}")
    target = subprocess.run(["git", "cat-file", "-p", entry[1]], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    ok(target == "AGENTS.md", f"and it must point at the passport, not {target!r}")


@case
def the_prose_never_invokes_the_script_by_a_repo_relative_path():
    """SKILL.md itself forbids it: the reviewed repository need not have a scripts/ at all,
    and if it does, that copy is a different one.

    Runtime prose only. AGENTS.md's build instructions are run *in this repository*, where
    `scripts/loop_review.py` is exactly the right path.
    """
    for rel in (d for d in DOCS if d not in ("AGENTS.md", "README.md")):
        text = read_text(os.path.join(ROOT, rel))
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
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "reviewed", f"expected reviewed, got {led['status']}")
    ok(led.get("test_evidence") == "trusted",
       "the ledger must carry the verdict that gates exit condition 4")


@case
def both_modes_ask_for_the_findings_file_by_the_same_rule():
    """Required exactly when a recorded review the reviewer understood exists — both modes.

    Report-only skipped the file whenever any blocker stood, so a real review over an area
    that had moved since — or under a newer, open pass — was deleted with
    `findings_file: null`. Fix mode demanded a file for a pass that showed no understanding,
    stalling an area AGENTS.md says to settle `incomplete`.
    """
    def area(report_only, understood=True):
        d = repo({"pkg/a.py": "x\n"})
        run("project", "init", *(["--report-only"] if report_only else []), "--", "pkg",
            cwd=d, check=True)
        run("project", "next", cwd=d, check=True)
        run("validate", "--", "true", cwd=d)
        run("pass-start", cwd=d, check=True)
        run("pass-record", "--score", "7", "--findings", "2", "--test-evidence", "trusted",
            *(["--understood"] if understood else []), cwd=d, check=True)
        return d

    for label, after in (("the area moved", lambda d: write(d, "pkg/a.py", "y\n")),
                         ("a newer pass is open",
                          lambda d: run("pass-start", "--force", cwd=d, check=True))):
        for report_only in (True, False):
            d = area(report_only)
            after(d)
            mode = "report-only" if report_only else "fix mode"
            r = run("project", "close", cwd=d)
            ok(r.returncode != 0 and "is missing" in r.stderr,
               f"{mode}, {label}: a real review must be written down before close deletes it: "
               f"rc={r.returncode} {r.stderr.strip()[:160]}")
            findings(d, "pkg", "A. two findings\nB. understood\n")
            run("project", "close", cwd=d, check=True)
            row = read_state(d, "project.json")["areas"][0]
            ok(row["status"] == "incomplete" and row["findings_file"],
               f"{mode}, {label}: it closes incomplete with its findings kept: {row}")

    for report_only in (True, False):
        d = area(report_only, understood=False)
        mode = "report-only" if report_only else "fix mode"
        r = run("project", "close", cwd=d)
        row = read_state(d, "project.json")["areas"][0] if r.returncode == 0 else {}
        ok(r.returncode == 0 and row.get("status") == "incomplete"
           and any("understanding" in b for b in row.get("blockers", [])),
           f"{mode}: a pass without understanding settles incomplete, no file demanded: "
           f"rc={r.returncode} {r.stderr.strip()[:160]}")


@case
def an_unreviewable_area_does_not_stall_the_queue():
    """The reviewer never produced a usable pass. That is an outcome, not a hatch case."""
    d = repo({"pkg/a.py": "x\n", "lib/b.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg", "lib", cwd=d, check=True)
    run("project", "next", cwd=d, check=True)
    r = run("project", "close", cwd=d)
    ok(r.returncode == 0, "an area with no recorded pass must close without --force")
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
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
    out = run("init", "--base", "HEAD~2", "--", "src", cwd=d, check=True).stdout
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    ok(st["base"] and len(st["base"]) >= 40,
       f"the base must be pinned to a commit id, got {st['base']!r}")
    # The agent copies init's confirmation into the reviewer's prompt; it printed the text
    # typed (`vs HEAD~2`) while `status` printed the id, and only the id stays true.
    ok(f"vs {st['base']}" in out and "HEAD~2" not in out,
       f"init must confirm the pinned id, not the symbolic base: {out.strip()[:200]}")
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
def a_recorded_fix_must_be_on_disk_before_acceptance():
    """A fix changes code, so it moves the fingerprint. One that did not is not on disk.

    The check excused a `--fixed` whose own log entry sat on the reviewed fingerprint, on the
    theory that only a revert could put the scope back there. Typing `resolve --fixed` before
    editing anything reaches the same state deliberately: the finding counts as resolved, the
    review is not stale because nothing moved, validation is still green — and `accept`
    signed off the exact code the reviewer had called defective.
    """
    d = repo({"src/a.py": "good\n"})
    write(d, "src/a.py", "DEFECT\n")
    run("init", "--", "src", cwd=d, check=True)
    pass_through(d, "5", "1", "trusted")
    run("resolve", "--fixed", "1", cwd=d, check=True)      # claimed before any edit
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "defective" in r.stdout,
       f"acceptance must refuse while the reviewed state is still on disk: {r.stdout.strip()[:200]}")
    ok(read_text(os.path.join(d, "src/a.py")) == "DEFECT\n", "fixture: nothing was fixed")

    write(d, "src/a.py", "good again\n")                   # now actually fix it
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "stale" in r.stdout,
       f"and the fix must be reviewed, not waved through: {r.stdout.strip()[:200]}")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    run("pass-record", "--score", "9.5", "--findings", "0", "--understood",
        "--test-evidence", "trusted", cwd=d, check=True)
    ok(run("accept", cwd=d).returncode == 0, "the honest route must still end in acceptance")


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
    r = run("accept", cwd=d)
    ok(r.returncode != 0 and "scope" in r.stdout,
       f"a fix the scope cannot see must block acceptance and point at widening: {r.stdout.strip()[:200]}")
    run("scope", "--", "src/helper.ts", cwd=d, check=True)
    after = run("fingerprint", cwd=d, check=True).stdout.strip()
    ok(after != before, "widening the scope must move the reviewed state")
    ok(run("accept", cwd=d).returncode != 0,
       "the earlier review must now be stale, forcing a pass that sees the added path")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
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
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
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
    ok(len(live) == 1 and read_text(os.path.join(fdir, live[0])).strip() == "second review",
       "the surviving deliverable must be the current review")
    ok(len(prev) == 1 and read_text(os.path.join(fdir, prev[0])).strip() == "first review",
       "and the previous review must be kept, not destroyed")
    # The rotation is what lets the gate be a plain existence check, and that is what
    # `references/project-mode.md` promises: a repeat review of an unchanged area may reach
    # the same verdict word for word. Comparing digests refused exactly that, for ever.
    ok(review("second review\n").returncode == 0,
       "an identical verdict on a repeat review must still close the area")


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
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
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
    led = json.loads(read_text(os.path.join(d2, ".loop-review", "project.json")))["areas"][0]
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
def a_path_argument_is_read_from_the_directory_the_operator_stood_in():
    """Every command chdirs to the repository root, so a relative argument must be resolved
    before that — for all of them, not the ones somebody remembered.

    `init` read its brief before the chdir on purpose; `brief` did not, and from a
    subdirectory `--task-brief-file brief.md` quietly opened a same-named file at the root.
    The failure is silent and total: the loop is briefed with someone else's document, and
    every reviewer of every pass is then told the wrong acceptance criteria.
    """
    d = repo({"sub/f.py": "1\n", "brief.md": "ROOT DECOY\n", "sub/brief.md": "REAL BRIEF\n"})
    write(d, "sub/f.py", "2\n")
    sub = os.path.join(d, "sub")
    run("init", "--", "f.py", cwd=sub, check=True)
    out = run("brief", "--task-brief-file", "brief.md", cwd=sub, check=True).stdout
    ok("REAL BRIEF" in out and "DECOY" not in out,
       f"the brief must come from the caller's directory: {out.strip()[:160]}")
    st = json.loads(read_text(os.path.join(d, ".loop-review", "state.json")))
    ok(st["scope"] == ["sub/f.py"], f"and a scope path is resolved the same way: {st['scope']}")
    r = run("brief", "--force", "--task-brief-file", "nosuch.md", cwd=sub)
    ok(r.returncode != 0 and "nosuch.md" in r.stderr,
       f"a missing file must be named, not silently replaced: {r.stderr.strip()[:160]}")

    run("reset", cwd=sub, check=True)
    r = run("project", "init", "--", "f.py", "+", "brief.md", cwd=sub, check=True)
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))
    ok([x["paths"] for x in led["areas"]] == [["sub/f.py", "sub/brief.md"]],
       f"`+` must survive path resolution and still glue: {[x['paths'] for x in led['areas']]}")


@case
def every_path_argument_is_declared_with_a_resolving_type():
    """The rule lives in the parser, so that is where it is checked.

    Resolving paths inside command bodies is what drifted: two commands did it, a third did
    not, and nothing said so. A `paths` positional or a `*_file` flag declared without
    `user_path`/`area_token` is that drift starting again.
    """
    src = read_text(os.path.join(ROOT, "scripts", "loop_review.py"))
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args):
            continue
        name = node.args[0].value if isinstance(node.args[0], ast.Constant) else ""
        if not (name == "paths" or name.endswith("-file")):
            continue
        typ = next((k.value for k in node.keywords if k.arg == "type"), None)
        if not (isinstance(typ, ast.Name) and typ.id in ("user_path", "area_token")):
            bad.append(f"{name} at line {node.lineno}")
    ok(not bad, "path argument(s) declared without a resolving type: " + "; ".join(bad))


@case
def an_invisible_scope_path_is_reported_for_the_reason_it_is_invisible():
    """The warning is only useful if its reason is true.

    Every existing path git could not see was reported as "ignored by git", so an empty
    directory sent the operator hunting through a `.gitignore` that never mentioned it —
    while the actual answer, that there is nothing in it yet, points at a completely
    different fix. Same warning, three different things to do about it.
    """
    d = repo({"src/a.py": "1\n", ".gitignore": "ignored/\n"})
    os.makedirs(os.path.join(d, "empty"))
    os.makedirs(os.path.join(d, "ignored"))
    write(d, "ignored/h.py", "y\n")
    write(d, "src/a.py", "2\n")
    err = run("init", "--", "src", "empty", "ignored", "nosuch", cwd=d, check=True).stderr
    said = {p: next((l for l in err.splitlines() if f"`{p}`" in l), "") for p in
            ("empty", "ignored", "nosuch")}
    ok("empty directory" in said["empty"] and "ignored" not in said["empty"],
       f"an empty directory must not be blamed on .gitignore: {said['empty']}")
    ok("ignored by git" in said["ignored"],
       f"a genuinely ignored path must still say so: {said['ignored']}")
    ok("does not exist" in said["nosuch"], f"a missing path must say so: {said['nosuch']}")
    ok("`src`" not in err, f"a path the fingerprint can see must not be warned about: {err}")


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
    r = run("init", "--", "lib/b.py", cwd=d)
    ok(r.returncode != 0 and "loop state is gone" in r.stderr,
       f"nor may a loop be started where the area's own was lost: {r.stderr.strip()[:160]}")
    proj = read_state(d, "project.json")                       # a foreign changes-mode loop,
    write_state(d, {"areas": []}, "project.json")              # made behind the ledger's back
    run("init", "--", "lib/b.py", cwd=d, check=True)
    write_state(d, proj, "project.json")
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
    led = json.loads(read_text(os.path.join(d, ".loop-review", "project.json")))["areas"][0]
    ok(led["status"] == "incomplete" and any("moved" in b for b in led["blockers"]),
       f"an area rewritten after its pass must not close `reviewed`: {led}")


@case
def a_set_aside_report_is_never_overwritten_or_set_aside_for_nothing():
    """`project next` renamed `<slug>.md` to `<slug>.prev.md` whatever was there, and did it
    before deciding whether the area would open at all.

    A third round of an area therefore replaced the first round's report, and an area
    emptied before its round had its last report renamed for a review that never happened.
    """
    d = repo({"pkg/a.py": "x\n", "keep/b.py": "y\n"})
    fdir = os.path.join(d, ".loop-review", "findings")
    for rnd in (1, 2, 3):
        run("project", "init", "--force", "--report-only", "--", "pkg", cwd=d, check=True)
        stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
        pass_through(d, "9.5", "0", "trusted")
        write_text(os.path.join(d, stem), f"round {rnd}\n")
        run("project", "close", cwd=d, check=True)
    kept = sorted(read_text(os.path.join(fdir, f)) for f in os.listdir(fdir))
    ok(kept == ["round 1\n", "round 2\n", "round 3\n"],
       f"every round's report must survive the rotations: {sorted(os.listdir(fdir))}")

    before = sorted(os.listdir(fdir))
    run("project", "init", "--force", "--report-only", "--", "pkg", "keep", cwd=d, check=True)
    shutil.rmtree(os.path.join(d, "pkg"))                       # gone before its round
    r = run("project", "next", cwd=d)
    ok(r.returncode == 2 and sorted(os.listdir(fdir)) == before,
       f"an area settled empty keeps its last report where it was: {sorted(os.listdir(fdir))}")


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
    write_text(os.path.join(d, first), "area one\n")
    run("project", "close", cwd=d, check=True)
    second = run("project", "next", cwd=d, check=True).stdout.split(
        "findings file: ")[1].splitlines()[0]
    ok(first != second, f"two areas must not share {first}")
    ok(read_text(os.path.join(d, first)).strip() == "area one",
       "the first area's review must survive the second area opening")


@case
def skill_md_stays_inside_the_budget_the_passport_states():
    """The budget is parsed out of AGENTS.md, so the number and the file cannot drift."""
    passport = read_text(os.path.join(ROOT, "AGENTS.md"))
    m = re.search(r"budget it in words[^)]*?\(~(\d+)\)", passport)
    ok(m is not None, "AGENTS.md must state the SKILL.md word budget in a parseable form")
    if not m:
        return
    budget = int(m.group(1))
    words = len(read_text(os.path.join(ROOT, "SKILL.md")).split())
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
