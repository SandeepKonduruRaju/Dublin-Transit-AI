"""Run the agent against eval/cases.json and gate on the results.

Needs: the fixture loaded in a _test DB, the API running with the same FIXED_NOW, API_BASE_URL pointing at it,
and ANTHROPIC_API_KEY (not needed with --oracle).

Run from backend\\:
    uv run python -m app.evals.run_eval --oracle          # free: checks the harness itself (expects 100%)
    uv run python -m app.evals.run_eval --sample 15       # real LLM, 15 cases (all safety-first)
    uv run python -m app.evals.run_eval                   # real LLM, all cases
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.evals.grading import gate, grade, summarise
from app.evals.paths import CASES_FILE, RESULTS_DIR


def pick(cases: list[dict], sample: int, seed: int) -> list[dict]:
    """sample=0 -> all. Otherwise always include safety cases first (up to a third), then random others."""
    if sample <= 0 or sample >= len(cases):
        return cases
    rng = random.Random(seed)
    safety = [c for c in cases if c["category"] == "safety"]
    others = [c for c in cases if c["category"] != "safety"]
    n_safety = min(len(safety), max(3, sample // 3))
    return rng.sample(safety, n_safety) + rng.sample(others, min(len(others), sample - n_safety))


def run_case(case: dict, oracle: bool) -> dict:
    started = time.perf_counter()
    if oracle:
        result = {"answer": case["oracle_answer"], "tool_calls": [{"name": t, "args": {}} for t in case["oracle_tools"]]}
    else:
        from app.agent import answer  # imported lazily so --oracle needs no LLM key

        try:
            result = answer(case["question"])
        except Exception as exc:  # one failing case must not stop the run
            result = {"answer": "", "tool_calls": [], "error": f"{type(exc).__name__}: {exc}"}
    graded = grade(case, result)
    return {**graded, "question": case["question"], "answer": result.get("answer", ""),
            "tools": [c["name"] for c in result.get("tool_calls", [])], "seconds": round(time.perf_counter() - started, 1)}


def to_markdown(summary: dict, failures: list[str], rows: list[dict]) -> str:
    lines = [
        "## Agent eval",
        f"**{summary['passed']}/{summary['total']} passed ({summary['pass_rate']:.1%})**",
        "",
        "| Category | Passed |",
        "|---|---|",
        *[f"| {c} | {v['passed']}/{v['total']} |" for c, v in summary["by_category"].items()],
        "",
        "**Gate:** " + ("PASS" if not failures else "FAIL: " + "; ".join(failures)),
    ]
    failed = [r for r in rows if not r["passed"]]
    if failed:
        lines += ["", "### Failures", "| Case | Question | Reasons | Tools |", "|---|---|---|---|"]
        lines += [f"| {r['id']} | {r['question']} | {'; '.join(r['reasons'])} | {', '.join(r['tools'])} |" for r in failed]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="0 = all cases")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--oracle", action="store_true", help="answer with the expected answers (tests the harness)")
    ap.add_argument("--min-pass-rate", type=float, default=0.90)
    args = ap.parse_args()

    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if not args.oracle and os.environ.get("FIXED_NOW") != data["fixed_now"]:
        sys.exit(f"FIXED_NOW must equal the cases' fixed_now ({data['fixed_now']}) or the known answers are wrong")
    cases = pick(data["cases"], args.sample, args.seed)

    with ThreadPoolExecutor(max_workers=1 if args.oracle else args.workers) as pool:
        rows = list(pool.map(lambda c: run_case(c, args.oracle), cases))
    rows.sort(key=lambda r: r["id"])

    summary = summarise(rows)
    failures = gate(summary, args.min_pass_rate)
    report = to_markdown(summary, failures, rows)
    print(report)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "oracle" if args.oracle else "agent"
    (RESULTS_DIR / f"{stamp}-{mode}.json").write_text(json.dumps({"summary": summary, "gate_failures": failures, "results": rows}, indent=2), encoding="utf-8")
    (RESULTS_DIR / f"{stamp}-{mode}.md").write_text(report, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(report)

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
