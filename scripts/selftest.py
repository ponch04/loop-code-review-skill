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
    # Ask the script for the name instead of re-deriving it. The private copy hashed
    # `slug.split("__")`, which is the flattened stem taken apart again: for a nested area
    # like `apps/portal/src/lib` that is four paths where the script hashes one, so the file
    # landed under a name `project close` does not look for — and the case would have failed
    # as "findings file is missing", pointing at the product for a defect in the fixture.
    # It also knew nothing of the stem's length cap or its leading-dot stripping.
    ledger = os.path.join(d, ".loop-review", "project.json")
    area = None
    if os.path.exists(ledger):
        areas = json.load(_io.open(ledger, encoding="utf-8"))["areas"]
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
    with open(os.path.join(fdir, name), "w", encoding="utf-8") as f:
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
    st = json.load(_io.open(sf, encoding="utf-8"))
    st["area"] = ["pkg/a", "pkg/b"]                    # a loop opened for a different area
    st["scope"] = ["pkg/a", "pkg/b"]
    json.dump(st, _io.open(sf, "w", encoding="utf-8"), indent=2)
    pass_through(d, "9.5", "0", "trusted")
    _io.open(os.path.join(d, stem), "w").write("A. no actionable findings\n")
    r = run("project", "close", cwd=d)
    ok(r.returncode != 0 and "not area" in r.stderr,
       f"a wider loop from elsewhere must not close this row: rc={r.returncode} {r.stderr.strip()[:160]}")

    # The other side, which is why containment was there in the first place.
    d = repo({"pkg/a/f.py": "x\n", "pkg/b/g.py": "y\n"})
    run("project", "init", "--report-only", "--", "pkg/a", "pkg/b", cwd=d, check=True)
    stem = run("project", "next", cwd=d, check=True).stdout.split("findings file: ")[1].splitlines()[0]
    run("scope", "--", "pkg/b", cwd=d, check=True)     # this area's own loop, widened
    pass_through(d, "9.5", "0", "trusted")
    _io.open(os.path.join(d, stem), "w").write("A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)
    area = json.load(_io.open(os.path.join(d, ".loop-review", "project.json"),
                              encoding="utf-8"))["areas"][0]
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
    st = json.load(open(sf))
    # A loop opened for another area. `area` is the identity — forging `scope` alone would
    # only look like this area's own loop after a legitimate `scope --` widening.
    st["area"] = st["scope"] = ["b"]
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
    area = json.load(_io.open(os.path.join(d, ".loop-review", "project.json"),
                              encoding="utf-8"))["areas"][0]
    ok(area["status"] == "reviewed" and area["findings_file"],
       f"the ledger must record the surviving report: {area['status']}, {area['findings_file']}")


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
    _io.open(os.path.join(d, stem), "w").write("A. no actionable findings\n")
    run("project", "close", cwd=d, check=True)

    sh("git rm -q -r gone && git commit -qm drop", d)               # 2. files gone
    run("project", "next", cwd=d)

    run("project", "next", cwd=d, check=True)                       # 3. loop state lost
    os.remove(os.path.join(d, ".loop-review", "state.json"))
    run("project", "close", "--force", cwd=d, check=True)

    run("project", "next", cwd=d, check=True)                       # 4. a foreign loop
    sf = os.path.join(d, ".loop-review", "state.json")
    st = json.load(_io.open(sf, encoding="utf-8"))
    st["area"] = st["scope"] = ["ok"]                  # a loop belonging to another area
    json.dump(st, _io.open(sf, "w", encoding="utf-8"), indent=2)
    run("project", "close", "--force", cwd=d, check=True)

    rows = [a for a in json.load(_io.open(os.path.join(d, ".loop-review", "project.json"),
                                          encoding="utf-8"))["areas"]
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
    _io.open(os.path.join(d, stem), "w").write("A. no actionable findings\n")
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
    _io.open(os.path.join(d, "brief.md"), "w", encoding="utf-8").write("requirements\n")
    r = run("brief", "--task-brief-file", "brief.md", cwd=d)
    ok(r.returncode != 0 and "--task-brief-file" in r.stderr,
       f"brief must name the file flag it was given: {r.stderr.strip()[:160]}")
    r = run("init", "--force", "--mode", "project", "--task-brief-file", "brief.md",
            "--", "pkg", cwd=d)
    ok(r.returncode != 0 and "--task-brief-file" in r.stderr,
       f"init must name it too: {r.stderr.strip()[:160]}")
    st = json.load(_io.open(os.path.join(d, ".loop-review", "state.json"), encoding="utf-8"))
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
    ok("[inherited]" in out and "[regressed]" not in out,
       f"a moved-scope run must not count as this check having been green: {out}")


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
    _io.open(os.path.join(d, stem), "w").write("A. one finding\n")

    write(d, "pkg/a.py", "y\n")                        # a second pass is opened...
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    sh("git checkout -- pkg/a.py", d)                  # ...and the area returns to the reviewed state
    r = run("project", "close", cwd=d, check=True)
    area = json.load(_io.open(os.path.join(d, ".loop-review", "project.json"),
                              encoding="utf-8"))["areas"][0]
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
    _io.open(os.path.join(d, stem), "w").write("A. one finding\n")
    write(d, "pkg/a.py", "y\n")
    run("validate", "--", "true", cwd=d)
    run("pass-start", cwd=d, check=True)
    sh("git checkout -- pkg/a.py", d)
    run("pass-abort", "--reason", "the area moved mid-review", cwd=d, check=True)
    run("project", "close", cwd=d, check=True)
    area = json.load(_io.open(os.path.join(d, ".loop-review", "project.json"),
                              encoding="utf-8"))["areas"][0]
    ok(area["status"] == "reviewed" and not area["blockers"],
       f"an aborted pass must not block the close: {area['status']}, {area['blockers']}")


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
    _io.open(os.path.join(sd, "project.json.tmp"), "w").write('{"partial"')
    r = run("reset", cwd=d, check=True)
    ok(not os.path.exists(os.path.join(sd, "project.json.tmp")),
       "a plain reset must clear the ledger's own .tmp debris")
    ok("did not create" not in r.stdout + r.stderr,
       f"and must not disown its own file: {(r.stdout + r.stderr).strip()[:200]}")
    ok(os.path.exists(os.path.join(sd, "project.json")),
       "while the ledger itself survives a reset without --project")

    _io.open(os.path.join(sd, "notes.txt"), "w").write("operator notes\n")
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
    out = run("brief", "--force", "--scope-note", "narrowed to the parser", cwd=d, check=True).stdout
    ok(brief in out and "narrowed to the parser" in out, f"brief must echo both: {out[:200]}")


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
    r = run("pass-start", cwd=d)
    ok(r.returncode == 0,
       f"an edit must not undo a retirement: {r.stderr.strip()[:200]}")
    r = run("validate-drop", "--force", "--", "echo suite", cwd=d)
    ok(r.returncode != 0 and "already retired" in r.stderr,
       f"re-forcing a standing decision must be refused as such: {r.stderr.strip()[:160]}")

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
    _io.open(os.path.join(d, first), "w").write("A. none\nB. understood\n")
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
def no_text_file_is_opened_at_the_mercy_of_the_platform_locale():
    """`open()` without `encoding=` decodes by whatever locale the machine has.

    The state files are the loop's whole memory and a brief or a reviewer note may hold any
    character, so reading them under a non-UTF-8 locale is a corrupted state rather than a
    clear error. The first pass at this fixed the call sites it happened to look at: four
    carried `encoding="utf-8"` while `load`, `load_project` and the atomic write did not.
    Binary mode is exempt — `fingerprint()` reads files as bytes precisely to avoid decoding.
    """
    src = _io.open(os.path.join(ROOT, "scripts", "loop_review.py"), encoding="utf-8").read()
    bare = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "open"):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        mode = node.args[1] if len(node.args) > 1 else None
        if isinstance(mode, ast.Constant) and "b" in mode.value:
            continue
        bare.append(node.lineno)
    ok(not bare, "text open() without encoding= at line(s): " + ", ".join(map(str, bare)))


@case
def no_helper_declares_a_parameter_it_never_reads():
    """A parameter nobody reads is a claim about the code that nothing enforces.

    `findings_are_current(ffile, area)` kept its `area` long after the digest matching that
    used it was replaced by the `.prev.md` rotation, so both the signature and every call
    site said the check was tied to the area object while it was a plain existence test.
    Command functions are exempt: the dispatcher calls every one as `a.fn(a)`, so `a` is
    part of the contract whether that command needs it or not.
    """
    src = _io.open(os.path.join(ROOT, "scripts", "loop_review.py"), encoding="utf-8").read()
    stale = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.FunctionDef):
            continue
        read = ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})
        for arg in [a.arg for a in node.args.args + node.args.kwonlyargs]:
            if arg in read or (arg == "a" and node.name.startswith("cmd_")):
                continue
            stale.append(f"{node.name}({arg}) at line {node.lineno}")
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
    st = json.load(_io.open(sf, encoding="utf-8"))
    st["area"] = st["scope"] = ["b"]                   # a loop belonging to another area
    json.dump(st, _io.open(sf, "w", encoding="utf-8"), indent=2)
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
    return out


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
        incomplete = r.returncode == 2 or " needs " in r.stderr.split("\n")[0]
        ok(r.returncode == 0 or incomplete,
           f"{label}: `{' '.join(tokens)}` is named as the way out but the state refuses it: "
           f"{(r.stderr or r.stdout).strip()[:200]}")


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
    ok(_io.open(os.path.join(d, "src/a.py")).read() == "DEFECT\n", "fixture: nothing was fixed")

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
    st = json.load(_io.open(os.path.join(d, ".loop-review", "state.json"), encoding="utf-8"))
    ok(st["scope"] == ["sub/f.py"], f"and a scope path is resolved the same way: {st['scope']}")
    r = run("brief", "--force", "--task-brief-file", "nosuch.md", cwd=sub)
    ok(r.returncode != 0 and "nosuch.md" in r.stderr,
       f"a missing file must be named, not silently replaced: {r.stderr.strip()[:160]}")

    run("reset", cwd=sub, check=True)
    r = run("project", "init", "--", "f.py", "+", "brief.md", cwd=sub, check=True)
    led = json.load(_io.open(os.path.join(d, ".loop-review", "project.json"), encoding="utf-8"))
    ok([x["paths"] for x in led["areas"]] == [["sub/f.py", "sub/brief.md"]],
       f"`+` must survive path resolution and still glue: {[x['paths'] for x in led['areas']]}")


@case
def every_path_argument_is_declared_with_a_resolving_type():
    """The rule lives in the parser, so that is where it is checked.

    Resolving paths inside command bodies is what drifted: two commands did it, a third did
    not, and nothing said so. A `paths` positional or a `*_file` flag declared without
    `user_path`/`area_token` is that drift starting again.
    """
    src = _io.open(os.path.join(ROOT, "scripts", "loop_review.py"), encoding="utf-8").read()
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
