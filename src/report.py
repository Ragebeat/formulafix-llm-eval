"""Metrics table for one or several runs of a configuration.

  uv run python src/report.py --label node-parity
  uv run python src/report.py --run 3 4 5

Every number comes with its denominator, and two views are shown:
  answered : among answers the model actually gave (status ok)
  overall  : among all cases; a failed call counts as not correct
Run score.py first (and the judge step for fix_mode = judge).
One run is not a conclusion: with several runs the report shows the mean, the
min-max spread and the stability (same verdict and error type in every run).
"""

import argparse
import sys
from collections import Counter, defaultdict

import psycopg

import config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SELECT = """
SELECT rr.run_id, rr.case_id, rr.status, rr.verdict, rr.error_type, rr.issues,
       rr.latency_ms, rr.output_tokens, rr.reasoning_tokens, rr.attempts,
       rr.verdict_ok, rr.error_type_ok, rr.fix_ok, rr.fix_scored_by, rr.cost_usd, rr.served_by,
       g.split, g.category, g.expected_verdict, g.fix_mode, g.case_group, rr.cause_ok, rr.cause_named, rr.action_ok
FROM run_results rr JOIN golden_cases g ON g.id = rr.case_id
WHERE rr.run_id = ANY(%s)
ORDER BY rr.run_id, rr.case_id
"""
COLS = ["run_id", "case_id", "status", "verdict", "error_type", "issues", "latency_ms",
        "output_tokens", "reasoning_tokens", "attempts", "verdict_ok", "error_type_ok",
        "fix_ok", "fix_scored_by", "cost_usd", "served_by", "split", "category", "expected_verdict", "fix_mode", "case_group", "cause_ok", "cause_named", "action_ok"]


def frac(n: int, d: int) -> str:
    return f"{n / d:.3f} ({n}/{d})" if d else "n/a"


def pctl(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[round(q * (len(s) - 1))]


def metric(rows: list[dict], pick, flag: str) -> tuple[int, int, int]:
    """(correct, answered, overall) over the rows selected by pick()."""
    sel = [r for r in rows if pick(r)]
    answered = [r for r in sel if r["status"] == "ok"]
    return sum(1 for r in sel if r[flag] is True), len(answered), len(sel)


def run_block(run: dict, rows: list[dict]) -> float | None:
    status = Counter(r["status"] for r in rows)
    print(f"run {run['id']}  {run['label']}  {run['model']}  T={run['temperature']}  "
          f"prompt={run['prompt_version']}  cases={len(rows)}")
    print("  status: " + ", ".join(f"{s}={n}" for s, n in sorted(status.items())))
    unscored = sum(1 for r in rows if r["status"] == "ok" and r["verdict_ok"] is None)
    if unscored:
        print(f"  !! {unscored} answers are NOT SCORED yet: run score.py first, the numbers below are meaningless")

    ok, ans, tot = metric(rows, lambda r: True, "verdict_ok")
    print(f"  verdict       answered {frac(ok, ans)}   overall {frac(ok, tot)}")
    ok, ans, tot = metric(rows, lambda r: r["expected_verdict"] == "broken", "error_type_ok")
    print(f"  error_type    answered {frac(ok, ans)}   overall {frac(ok, tot)}   (expected-broken cases)")
    is_fix = lambda r: r["fix_mode"] in ("any_of", "judge")
    ok, ans, tot = metric(rows, is_fix, "fix_ok")
    pending = sum(1 for r in rows if is_fix(r) and r["status"] == "ok" and r["fix_ok"] is None)
    exact = sum(1 for r in rows if r["fix_scored_by"] == "normalized-match")
    note = f"exact match {exact}, pending judge {pending}" if pending else f"exact match {exact}, rest judged"
    print(f"  fix correct   answered {frac(ok, ans)}   overall {frac(ok, tot)}   ({note})")

    for split in sorted({r["split"] for r in rows}):
        ok, _, tot = metric(rows, lambda r, s=split: r["split"] == s, "verdict_ok")
        print(f"  verdict {split:<10} overall {frac(ok, tot)}")
    for grp in sorted({r["case_group"] for r in rows if r["case_group"]}):
        ok, _, tot = metric(rows, lambda r, g=grp: r["case_group"] == g, "verdict_ok")
        print(f"  group {grp:<14} verdict overall {frac(ok, tot)}")
    ok, ans, tot = metric(rows, lambda r: r["case_group"] == "hidden_cause", "cause_ok")
    if tot:
        judged = sum(1 for r in rows if r["case_group"] == "hidden_cause" and r["status"] == "ok" and r["cause_ok"] is not None)
        print(f"  cause found (hidden_cause only, judge reads the explanation)  overall {frac(ok, tot)}"
              + (f"   !! {ans - judged} not judged yet: run judge_cause.py" if judged < ans else ""))
    if any(r["case_group"] == "hidden_cause" for r in rows):
        for flag, title in (("cause_named", "cause named (rubric v2)"), ("action_ok", "action accepted (rubric v2)")):
            ok, ans, tot = metric(rows, lambda r: r["case_group"] == "hidden_cause", flag)
            print(f"  {title:<28} overall {frac(ok, tot)}")
    print(f"  inconsistent answers: {sum(1 for r in rows if r['issues'])}")
    lat = [r["latency_ms"] for r in rows if r["status"] == "ok" and r["latency_ms"] is not None]
    out = [r["output_tokens"] for r in rows if r["output_tokens"] is not None]
    rsn = [r["reasoning_tokens"] for r in rows if r["reasoning_tokens"] is not None]
    mean = lambda v: round(sum(v) / len(v)) if v else None
    print(f"  latency p50/p95: {pctl(lat, 0.5)}/{pctl(lat, 0.95)} ms   "
          f"output tokens mean: {mean(out)}   reasoning tokens mean: {mean(rsn)}   "
          f"retries: {sum((r['attempts'] or 1) - 1 for r in rows)}")
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    if costs:
        print(f"  cost: ${sum(costs):.4f} for the run")
    served = Counter(r["served_by"] for r in rows if r["served_by"])
    if served:
        print("  served by: " + ", ".join(f"{k}={v}" for k, v in served.most_common()))
    ok, _, tot = metric(rows, lambda r: True, "verdict_ok")
    return ok / tot if tot else None


def compare_runs(by_run: dict[int, list[dict]], scores: list[float]) -> None:
    n = len(by_run)
    print(f"\n=== {n} runs together ===")
    print(f"verdict overall: mean {sum(scores) / n:.3f}, min-max {min(scores):.3f}-{max(scores):.3f}")

    per_case: dict[str, list[dict]] = defaultdict(list)
    for rows in by_run.values():
        for r in rows:
            per_case[r["case_id"]].append(r)

    stable_pool = [c for c, rs in per_case.items() if len(rs) == n and all(r["status"] == "ok" for r in rs)]
    unstable = [c for c in stable_pool
                if len({(r["verdict"], r["error_type"]) for r in per_case[c]}) > 1]
    print(f"stability: {frac(len(stable_pool) - len(unstable), len(stable_pool))} cases answered "
          f"in all runs gave the same verdict and error type")
    if unstable:
        print("  unstable: " + " ".join(sorted(unstable)))

    always_wrong = sorted(c for c, rs in per_case.items()
                          if len(rs) == n and all(r["verdict_ok"] is False for r in rs))
    if always_wrong:
        cats = {c: per_case[c][0]["category"] for c in always_wrong}
        print("verdict wrong in EVERY run (model errors, not noise): "
              + ", ".join(f"{c}[{cats[c]}]" for c in always_wrong))
    flaky_fail = sorted(c for c, rs in per_case.items()
                        if any(r["status"] != "ok" for r in rs))
    if flaky_fail:
        print("had a failed call (rate limit / json / ...) in some run: " + " ".join(flaky_fail))


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--label")
    g.add_argument("--run", type=int, nargs="+")
    p.add_argument("--last", type=int, default=None, help="with --label: only the last N runs")
    args = p.parse_args()

    with psycopg.connect(config.database_url()) as conn:
        if args.run:
            ids = args.run
        else:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM runs WHERE label = %s ORDER BY id", (args.label,)).fetchall()]
            if not ids:
                raise SystemExit(f"no runs with label {args.label!r}")
            if args.last:
                ids = ids[-args.last:]
        runs = {r[0]: dict(zip(["id", "label", "model", "temperature", "prompt_version"], r))
                for r in conn.execute(
                    "SELECT id, label, model, temperature, prompt_version FROM runs WHERE id = ANY(%s)",
                    (ids,)).fetchall()}
        rows = [dict(zip(COLS, r)) for r in conn.execute(SELECT, (ids,)).fetchall()]

    by_run: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)
    scores = []
    for rid in ids:
        if rid not in by_run:
            print(f"run {rid}: no results")
            continue
        s = run_block(runs[rid], by_run[rid])
        if s is not None:
            scores.append(s)
        print()
    if len(scores) > 1:
        compare_runs({rid: by_run[rid] for rid in ids if rid in by_run}, scores)


if __name__ == "__main__":
    main()
