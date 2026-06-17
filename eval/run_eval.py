"""LaravelGraph eval runner.

Usage:
    python -m eval.run_eval --mode structural --app tiny
    python -m eval.run_eval --mode structural --app real
    python -m eval.run_eval --mode agent --app tiny        # needs ANTHROPIC_API_KEY

Structural mode is deterministic and needs no API key — it calls the tool that
should answer each question and checks the ground-truth facts are present. It is
the CI regression gate and the WS3 non-regression baseline.

Agent mode is the opt-in A/B: it runs an LLM agent twice per question (with the
LaravelGraph tools vs file-access-only) and LLM-judges both answers, producing
accuracy_with vs accuracy_without — the headline "does it help" number.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from eval.client import index_app, resolve_app_paths, run_calls

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def load_dataset(path: Path) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise ValueError(f"Dataset {path} must be a YAML list of questions")
    return data


def _check(response: str, expect_all: list[str]) -> list[str]:
    """Return the list of expected facts MISSING from the response."""
    low = response.lower()
    return [fact for fact in expect_all if str(fact).lower() not in low]


def run_structural(app: str, dataset: Path) -> dict:
    questions = load_dataset(dataset)
    source, into, cleanup = resolve_app_paths(app)

    t0 = time.time()
    indexed = index_app(source, into=into)
    index_secs = time.time() - t0

    calls = [(q["tool"], q.get("args") or {}) for q in questions]
    responses = run_calls(indexed.root, calls)

    results = []
    passed = 0
    for q, resp in zip(questions, responses, strict=False):
        missing = _check(resp, q.get("expect_all", []))
        ok = not missing and not resp.startswith("__TOOL_ERROR__")
        passed += int(ok)
        results.append(
            {
                "id": q["id"],
                "category": q.get("category", ""),
                "tool": q["tool"],
                "passed": ok,
                "missing": missing,
                "errored": resp.startswith("__TOOL_ERROR__"),
            }
        )

    if cleanup:
        indexed.cleanup()

    total = len(questions)
    return {
        "mode": "structural",
        "app": app,
        "total": total,
        "passed": passed,
        "structural_correctness": round(100.0 * passed / total, 1) if total else 0.0,
        "index_seconds": round(index_secs, 1),
        "results": results,
    }


def render_scorecard(report: dict) -> str:
    lines = [f"# Eval Scorecard — {report['mode']} / app={report['app']}", ""]
    if report["mode"] == "agent":
        lines += [
            f"- **accuracy_with:** {report['accuracy_with']}%  "
            f"**accuracy_without:** {report['accuracy_without']}%  "
            f"**lift:** {report['lift']:+}pp",
            f"- model: `{report.get('model', '?')}`  ({report['total']} questions)",
            "",
            "| id | category | with LaravelGraph | without (files only) |",
            "|----|----------|-------------------|----------------------|",
        ]
        for r in report["results"]:
            lines.append(
                f"| {r['id']} | {r['category']} | "
                f"{'✅' if r['with'] else '❌'} | {'✅' if r['without'] else '❌'} |"
            )
        return "\n".join(lines) + "\n"

    lines += [
        f"- **structural_correctness:** {report['structural_correctness']}% "
        f"({report['passed']}/{report['total']})",
        f"- index time: {report.get('index_seconds', '?')}s",
        "",
        "| id | category | tool | result | missing facts |",
        "|----|----------|------|--------|---------------|",
    ]
    for r in report["results"]:
        status = "✅" if r["passed"] else ("⚠️ ERROR" if r["errored"] else "❌")
        miss = ", ".join(r["missing"]) if r["missing"] else ""
        lines.append(f"| {r['id']} | {r['category']} | {r['tool']} | {status} | {miss} |")
    return "\n".join(lines) + "\n"


def write_results(report: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{report['mode']}_{report['app']}"
    (RESULTS_DIR / f"{stem}.json").write_text(json.dumps(report, indent=2))
    md_path = RESULTS_DIR / f"{stem}.md"
    md_path.write_text(render_scorecard(report))
    return md_path


def main() -> int:
    ap = argparse.ArgumentParser(description="LaravelGraph eval harness")
    ap.add_argument("--mode", choices=["structural", "agent"], default="structural")
    ap.add_argument("--app", choices=["tiny", "real"], default="tiny")
    ap.add_argument("--dataset", type=Path, default=None, help="override dataset path")
    args = ap.parse_args()

    dataset = args.dataset or (DATASET_DIR / f"{args.app}.yaml")
    if not dataset.exists():
        print(f"ERROR: dataset not found: {dataset}", file=sys.stderr)
        return 2

    if args.mode == "structural":
        report = run_structural(args.app, dataset)
    else:
        from eval.agent_eval import run_agent_ab

        report = run_agent_ab(args.app, dataset)

    md_path = write_results(report)
    print(render_scorecard(report))
    print(f"\nWrote {md_path}")

    if args.mode == "structural":
        # Non-zero exit if anything regressed, so CI can gate on it.
        return 0 if report["passed"] == report["total"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
