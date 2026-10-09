"""Re-validate stored raw answers with the current schema. No API calls.

  uv run python src/reparse.py --label node-parity

Why it exists: the first version of AnalyzeResult required `whats_broken` and `why`
to be strings, the Node original accepted null, and ~5% of the baseline answers
(all of them for formulas that are fine) ended as schema_violation. The model's
actual answer is stored in raw_text, so the fix is to read it again with the
corrected schema rather than to spend tokens on new runs.

Only status = schema_violation rows with a raw_text are touched. A row that is
still invalid stays as it was. The old error is kept in error_detail.
"""

import argparse
import sys

import psycopg
from pydantic import ValidationError

import config
from analyze import extract_json
from schema import AnalyzeResult

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SELECT = """
SELECT rr.run_id, rr.case_id, rr.raw_text, rr.error_detail
FROM run_results rr JOIN runs r ON r.id = rr.run_id
WHERE rr.status = 'schema_violation' AND rr.raw_text IS NOT NULL
  AND (%s::text IS NULL OR r.label = %s) AND (%s::bigint[] IS NULL OR rr.run_id = ANY(%s))
ORDER BY rr.run_id, rr.case_id
"""

UPDATE = """
UPDATE run_results SET status = 'ok', verdict = %s, error_type = %s, whats_broken = %s, why = %s,
    fixed_formula = %s, what_changed = %s, if_ok_suggestion = %s, confidence = %s, issues = %s,
    error_detail = %s
WHERE run_id = %s AND case_id = %s
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label")
    p.add_argument("--run", type=int, nargs="+")
    args = p.parse_args()
    if not args.label and not args.run:
        raise SystemExit("give --label or --run")

    fixed = still = 0
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        rows = conn.execute(SELECT, (args.label, args.label, args.run, args.run)).fetchall()
        for run_id, case_id, raw, old_detail in rows:
            try:
                r = AnalyzeResult.model_validate(extract_json(raw))
            except (ValueError, ValidationError) as e:
                still += 1
                print(f"[{run_id}] {case_id} still invalid: {str(e)[:100]}")
                continue
            conn.execute(
                UPDATE,
                (r.verdict, r.error_type, r.whats_broken, r.why, r.fixed_formula, r.what_changed,
                 r.if_ok_suggestion, r.confidence, r.consistency_issues(),
                 f"reparsed after schema fix; was: {old_detail}"[:300], run_id, case_id),
            )
            fixed += 1
            print(f"[{run_id}] {case_id} -> ok ({r.verdict})")
    print(f"reparsed: {fixed}, still invalid: {still}")


if __name__ == "__main__":
    main()
