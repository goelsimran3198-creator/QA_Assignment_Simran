#!/usr/bin/env python3
"""Run the whole suite:   python3 run_tests.py

Options:
  --regression-only   skip tests that are automated proofs of known defects (tests tagged @defect)
  -k TEXT             run only tests whose id contains TEXT
Writes a readable summary to evidence/test_report.txt and exits non-zero if anything failed.
Works with plain `python3 -m unittest -v` and with pytest too.
"""
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for t in flatten(item):
                yield t
        else:
            yield item


def case_of(test):
    """subTest failures are wrapped; unwrap to the real TestCase."""
    return getattr(test, "test_case", test)


def defect_of(test):
    case = case_of(test)
    method = getattr(case, case._testMethodName, None)
    return getattr(method, "defect_id", None)


def main():
    args = sys.argv[1:]
    regression_only = "--regression-only" in args
    keyword = args[args.index("-k") + 1] if "-k" in args else None

    discovered = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    tests = [t for t in flatten(discovered)
             if (not regression_only or not defect_of(t)) and (not keyword or keyword in t.id())]
    suite = unittest.TestSuite(tests)

    started = time.time()
    result = unittest.TextTestRunner(verbosity=2, resultclass=unittest.TextTestResult).run(suite)
    elapsed = time.time() - started

    failed = {}   # one entry per failing test METHOD (sub-case failures are merged)
    for test, tb in result.failures + result.errors:
        failed[case_of(test).id()] = (defect_of(test), tb)
    method_count = len(tests)

    lines = ["Subscription Reliability API - test report",
             "Ran %d tests in %.1fs: %d passed, %d failed" % (
                 method_count, elapsed, method_count - len(failed), len(failed)), ""]
    by_bug = {}
    for tid, (bug, _) in failed.items():
        by_bug.setdefault(bug or "UNTAGGED (new/unexpected failure!)", []).append(tid.replace("tests.", ""))
    lines.append("Failures grouped by defect (see BUGS.md):")
    for bug in sorted(by_bug):
        lines.append("  %s" % bug)
        for tid in by_bug[bug]:
            lines.append("      - %s" % tid)
    if not by_bug:
        lines.append("  none")
    report = "\n".join(lines)
    print("\n" + report)

    os.makedirs(os.path.join(ROOT, "evidence"), exist_ok=True)
    with open(os.path.join(ROOT, "evidence", "test_report.txt"), "w") as fh:
        fh.write(report + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
