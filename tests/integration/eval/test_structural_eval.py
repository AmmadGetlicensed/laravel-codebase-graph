"""CI gate: the structural eval must stay at 100% on the tiny fixture.

This is the regression guard for the tool revamp — if consolidation or any
other change stops a tool returning a known ground-truth fact, this fails.
"""

from __future__ import annotations

import pytest

from eval.run_eval import DATASET_DIR, run_structural


def test_structural_eval_tiny_is_perfect():
    report = run_structural("tiny", DATASET_DIR / "tiny.yaml")
    failures = [r for r in report["results"] if not r["passed"]]
    assert report["passed"] == report["total"], (
        f"Structural eval regressed to {report['structural_correctness']}% "
        f"({report['passed']}/{report['total']}). Failing: "
        + ", ".join(f"{f['id']} missing={f['missing']} errored={f['errored']}" for f in failures)
    )


def test_dataset_is_wellformed():
    """Every question has the fields the runner needs."""
    import yaml

    data = yaml.safe_load((DATASET_DIR / "tiny.yaml").read_text())
    assert isinstance(data, list) and data
    ids = set()
    for q in data:
        for field in ("id", "question", "tool", "expect_all"):
            assert field in q, f"question missing '{field}': {q}"
        assert q["id"] not in ids, f"duplicate id {q['id']}"
        ids.add(q["id"])
        assert isinstance(q["expect_all"], list) and q["expect_all"]
