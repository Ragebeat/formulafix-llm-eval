"""Judge the fixes that string matching cannot decide.

  uv run python src/judge.py --label node-parity
  uv run python src/judge.py --run 3 4 5 --delay 3

Which answers go to the judge (expected verdict "broken", status ok, fix_ok still NULL):
  - fix_mode = judge : always (criteria written in the golden set)
  - fix_mode = any_of: only those that did NOT match a reference fix after
    normalization (score.py). A non-match is not "wrong" until the judge agrees:
    an equivalent formula written differently must not be punished.
An answer with no fix at all (verdict other than "broken", empty fixed_formula) is
scored False without a call (scored_by = rule:no-fix).

The judge must come from a DIFFERENT model family than the model under test: a judge
from the same family is blind in the same places. Default openai/gpt-oss-120b.
Every verdict keeps the judge's one-sentence reason (fix_judge_note): read a sample
by hand before trusting the numbers.

Env (all optional): JUDGE_MODEL, JUDGE_BASE_URL, JUDGE_API_KEY, JUDGE_MAX_TOKENS,
JUDGE_REASONING_EFFORT (sent as reasoning_effort, e.g. "low" for gpt-oss on Groq).
"""

import argparse
import asyncio
import os
import sys

import httpx
import psycopg

import config
import prompts
from analyze import extract_json
from llm import LLMConfig, LLMError, complete

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JUDGE_PROMPT = "judge_fix_v1"

SELECT = """
SELECT rr.run_id, rr.case_id, rr.verdict, rr.fixed_formula, r.model,
       g.dialect, g.formula, g.context, g.expected_error_type, g.alt_error_types,
       g.fix_mode, g.acceptable_fixes, g.fix_criteria
FROM run_results rr
JOIN golden_cases g ON g.id = rr.case_id
JOIN runs r ON r.id = rr.run_id
WHERE rr.run_id = ANY(%s) AND rr.status = 'ok' AND g.expected_verdict = 'broken'
  AND g.fix_mode IN ('any_of', 'judge') AND rr.fix_ok IS NULL
  AND (rr.fix_judge_note IS NULL OR rr.fix_judge_note LIKE 'judge_failed%%')
ORDER BY rr.run_id, rr.case_id
"""

UPDATE = """
UPDATE run_results SET fix_ok = %s, fix_scored_by = %s, fix_judge_note = %s
WHERE run_id = %s AND case_id = %s
"""


def judge_config() -> LLMConfig:
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise SystemExit("Set LLM_API_KEY (or JUDGE_API_KEY) in .env")
    return LLMConfig(
        base_url=os.environ.get("JUDGE_BASE_URL")
        or os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        api_key=key,
        model=os.environ.get("JUDGE_MODEL", "openai/gpt-oss-120b"),
        temperature=0.0,
        max_tokens=int(os.environ.get("JUDGE_MAX_TOKENS", "1500")),
        timeout_s=60.0,
    )


def user_message(dialect, formula, context, exp_type, alts, fix_mode, acceptable, criteria, proposed) -> str:
    diagnosis = exp_type + (f" (also acceptable: {', '.join(alts)})" if alts else "")
    if fix_mode == "any_of":
        reference = (
            "Reference fixes known to be correct (the proposed fix may be written "
            "differently if it is equivalent):\n" + "\n".join(f"- {a}" for a in acceptable)
        )
        if criteria.strip():
            reference += f"\nAdditional criteria: {criteria}"
    else:
        reference = f"Criteria for a correct fix:\n{criteria}"
    return (
        f"Product: {prompts.PRODUCT[dialect]}\n"
        f"User formula: {formula}\n"
        f"User context: {context or 'none'}\n"
        f"Expected diagnosis: {diagnosis}\n"
        f"{reference}\n"
        f"Proposed fix: {proposed}"
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--label")
    g.add_argument("--run", type=int, nargs="+")
    p.add_argument("--last", type=int, default=None, help="with --label: only the last N runs")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between judge calls")
    p.add_argument("--retries", type=int, default=3)
    args = p.parse_args()

    cfg = judge_config()
    template, sha = prompts.load_template(JUDGE_PROMPT)
    extra = {}
    if os.environ.get("JUDGE_REASONING_EFFORT"):
        extra["reasoning_effort"] = os.environ["JUDGE_REASONING_EFFORT"]

    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        if args.run:
            ids = args.run
        else:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM runs WHERE label = %s ORDER BY id", (args.label,)).fetchall()]
            if not ids:
                raise SystemExit(f"no runs with label {args.label!r}")
            if args.last:
                ids = ids[-args.last:]
        rows = conn.execute(SELECT, (ids,)).fetchall()
        print(f"judge {cfg.model}, prompt {JUDGE_PROMPT} sha {sha[:12]}; {len(rows)} answers to judge in runs {ids}")

        tally = {"correct": 0, "incorrect": 0, "no-fix": 0, "failed": 0}
        async with httpx.AsyncClient() as client:
            for i, (run_id, case_id, verdict, fixed, tested_model, dialect, formula, context,
                    exp_type, alts, fix_mode, acceptable, criteria) in enumerate(rows):
                if cfg.model == tested_model:
                    raise SystemExit("judge model equals the model under test: use another family")
                if verdict != "broken" or not (fixed or "").strip():
                    conn.execute(UPDATE, (False, "rule:no-fix", f"answer verdict was {verdict}, no fix offered",
                                          run_id, case_id))
                    tally["no-fix"] += 1
                    print(f"[{run_id}] {case_id} no-fix")
                    continue
                msg = user_message(dialect, formula.strip(), context.strip(), exp_type, alts,
                                   fix_mode, acceptable, criteria, fixed.strip())
                try:
                    reply = await complete(client, cfg, template, msg, max_retries=args.retries,
                                           extra_body=extra)
                    data = extract_json(reply.text)
                    correct = data.get("correct")
                    if not isinstance(correct, bool):
                        raise ValueError(f"'correct' is not a boolean: {correct!r}")
                    reason = str(data.get("reason", ""))[:500]
                except (LLMError, ValueError) as e:
                    detail = e.detail if isinstance(e, LLMError) else str(e)
                    conn.execute(UPDATE, (None, None, f"judge_failed: {detail}"[:500], run_id, case_id))
                    tally["failed"] += 1
                    print(f"[{run_id}] {case_id} judge FAILED: {detail[:80]}")
                else:
                    conn.execute(UPDATE, (correct, f"judge:{cfg.model}", reason, run_id, case_id))
                    tally["correct" if correct else "incorrect"] += 1
                    print(f"[{run_id}] {case_id} {'correct' if correct else 'incorrect'}")
                if i < len(rows) - 1:
                    await asyncio.sleep(args.delay)
        print("done: " + ", ".join(f"{k}={v}" for k, v in tally.items()))


if __name__ == "__main__":
    asyncio.run(main())
