"""Did the model find the real cause? (group hidden_cause of the validation set only)

  uv run python src/judge_cause.py --label deepseek-v41-val
  uv run python src/judge_cause.py --run 15 17 18

In hidden_cause cases the formula is valid and the problem sits elsewhere (manual calculation,
"show formulas" mode, SUM inside a filter...). The model often names the cause in why /
what_changed but answers verdict=ok, so verdict_ok and fix_ok cannot see it. This script adds a
SEPARATE column, cause_ok; verdict_ok and fix_ok are left untouched.

The judge reads the model's explanation (unlike judge.py, which reads only the formula) and
compares it with the case's fix_criteria. Same judge model and endpoint rules as judge.py.
Run it like judge.py: without ENV_FILE, unless JUDGE_BASE_URL / JUDGE_API_KEY are set.
"""

import argparse
import asyncio
import sys

import httpx
import psycopg

import config
import prompts
from analyze import extract_json
from judge import judge_config
from llm import LLMError, complete

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RUBRICS = {"v1": "judge_cause_v1", "v2": "judge_cause_v2"}

SELECT = """
SELECT rr.run_id, rr.case_id, rr.verdict, rr.whats_broken, rr.why, rr.what_changed, rr.fixed_formula,
       r.model, g.dialect, g.formula, g.context, g.fix_criteria
FROM run_results rr
JOIN golden_cases g ON g.id = rr.case_id
JOIN runs r ON r.id = rr.run_id
WHERE rr.run_id = ANY(%s) AND rr.status = 'ok' AND g.case_group = 'hidden_cause'
  AND rr.{flag} IS NULL
  AND (rr.{note} IS NULL OR rr.{note} LIKE 'judge_failed%%')
ORDER BY rr.run_id, rr.case_id
"""

UPDATE_V1 = "UPDATE run_results SET cause_ok = %s, cause_judge_note = %s WHERE run_id = %s AND case_id = %s"
UPDATE_V2 = ("UPDATE run_results SET cause_named = %s, action_ok = %s, cause_v2_note = %s "
             "WHERE run_id = %s AND case_id = %s")


def user_message(dialect, formula, context, criteria, verdict, broken, why, changed, fixed) -> str:
    return (
        f"Product: {prompts.PRODUCT[dialect]}\n"
        f"User formula: {formula}\n"
        f"User context: {context or 'none'}\n"
        f"Reference (real cause and accepted actions): {criteria}\n"
        f"Assistant's answer:\n"
        f"- verdict: {verdict}\n"
        f"- what is broken: {broken or '(none)'}\n"
        f"- why: {why or '(none)'}\n"
        f"- what changed: {changed or '(none)'}\n"
        f"- new formula: {fixed or '(none)'}"
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--label")
    g.add_argument("--run", type=int, nargs="+")
    p.add_argument("--rubric", choices=["v1", "v2"], default="v1",
                   help="v1: cause AND action in one flag (cause_ok); v2: cause_named and action_ok separately")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--retries", type=int, default=3)
    args = p.parse_args()

    cfg = judge_config()
    prompt_name = RUBRICS[args.rubric]
    template, sha = prompts.load_template(prompt_name)
    flag, note = ("cause_ok", "cause_judge_note") if args.rubric == "v1" else ("cause_named", "cause_v2_note")
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        if args.run:
            ids = args.run
        else:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM runs WHERE label = %s ORDER BY id", (args.label,)).fetchall()]
            if not ids:
                raise SystemExit(f"no runs with label {args.label!r}")
        rows = conn.execute(SELECT.format(flag=flag, note=note), (ids,)).fetchall()
        print(f"judge {cfg.model}, prompt {prompt_name} sha {sha[:12]}; {len(rows)} answers in runs {ids}")
        tally = {"correct": 0, "incorrect": 0, "failed": 0}
        async with httpx.AsyncClient() as client:
            for i, (run_id, case_id, verdict, broken, why, changed, fixed, tested_model,
                    dialect, formula, context, criteria) in enumerate(rows):
                if cfg.model == tested_model:
                    raise SystemExit("judge model equals the model under test: use another family")
                msg = user_message(dialect, formula.strip(), context.strip(), criteria,
                                   verdict, broken, why, changed, fixed)
                try:
                    reply = await complete(client, cfg, template, msg, max_retries=args.retries, extra_body={})
                    data = extract_json(reply.text)
                    if args.rubric == "v1":
                        correct = data.get("correct")
                        if not isinstance(correct, bool):
                            raise ValueError(f"'correct' is not a boolean: {correct!r}")
                    else:
                        named, action = data.get("cause_named"), data.get("action_ok")
                        if not isinstance(named, bool) or not isinstance(action, bool):
                            raise ValueError(f"cause_named/action_ok are not booleans: {named!r}, {action!r}")
                        correct = named
                    reason = str(data.get("reason", ""))[:500]
                except (LLMError, ValueError) as e:
                    detail = e.detail if isinstance(e, LLMError) else str(e)
                    failed_note = f"judge_failed: {detail}"[:500]
                    if args.rubric == "v1":
                        conn.execute(UPDATE_V1, (None, failed_note, run_id, case_id))
                    else:
                        conn.execute(UPDATE_V2, (None, None, failed_note, run_id, case_id))
                    tally["failed"] += 1
                    print(f"[{run_id}] {case_id} judge FAILED: {detail[:80]}")
                else:
                    if args.rubric == "v1":
                        conn.execute(UPDATE_V1, (correct, reason, run_id, case_id))
                    else:
                        conn.execute(UPDATE_V2, (named, action, reason, run_id, case_id))
                    tally["correct" if correct else "incorrect"] += 1
                    print(f"[{run_id}] {case_id} {'correct' if correct else 'incorrect'}")
                if i < len(rows) - 1:
                    await asyncio.sleep(args.delay)
        print("done: " + ", ".join(f"{k}={v}" for k, v in tally.items()))


if __name__ == "__main__":
    asyncio.run(main())
