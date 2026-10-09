"""Score stored answers against the golden dataset (the deterministic part).

  uv run python src/score.py --label node-parity     # every run with this label
  uv run python src/score.py --run 3 4 5

Fills run_results.verdict_ok / error_type_ok / fix_ok. Rules, fixed before any run:

  verdict_ok     answer's verdict == expected verdict.
  error_type_ok  only where the expected verdict is "broken": the answer's
                 error_type equals the expected one or one of alt_error_types.
  fix_ok         only where fix_mode = any_of: True when the answer's fixed_formula equals
                 one of acceptable_fixes after normalization (see below) and the answer's
                 verdict is "broken". A NON-match is left NULL, not False: it goes to
                 the judge (judge.py), because an equivalent formula written differently
                 is not wrong. fix_mode = judge is likewise left to the judge. This script
                 only ever writes True, so re-running it cannot overwrite a judge verdict.

Rows whose status is not "ok" (rate limit, timeout, broken JSON, schema) are
NOT scored: they stay NULL. The report counts them as failures separately.

normalize_formula(): outside quotes, whitespace removed, upper case, leading "="
added, ",FALSE)" == ",0)" and ",TRUE)" == ",1)". Argument separators are NOT
normalized: for the locale cases the separator IS the fix, and returning the
formula unchanged must not count as correct.
"""

import argparse
import sys

import psycopg

import config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def normalize_formula(f: str) -> str:
    out: list[str] = []
    quote: str | None = None  # inside "text" or 'sheet name': kept exactly as is
    for ch in f.strip():
        if quote is None:
            if ch in "\"'":
                quote = ch
                out.append(ch)
            elif not ch.isspace():
                out.append(ch.upper())
        else:
            out.append(ch)
            if ch == quote:
                quote = None
    s = "".join(out)
    if not s.startswith("="):
        s = "=" + s
    return s.replace(",FALSE)", ",0)").replace(",TRUE)", ",1)")


SELECT = """
SELECT rr.run_id, rr.case_id, rr.status, rr.verdict, rr.error_type, rr.fixed_formula,
       g.expected_verdict, g.expected_error_type, g.alt_error_types, g.fix_mode, g.acceptable_fixes
FROM run_results rr JOIN golden_cases g ON g.id = rr.case_id
WHERE rr.run_id = ANY(%s)
"""


def run_ids(conn: psycopg.Connection, args) -> list[int]:
    if args.run:
        return args.run
    rows = conn.execute("SELECT id FROM runs WHERE label = %s ORDER BY id", (args.label,)).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        raise SystemExit(f"no runs with label {args.label!r}")
    return ids[-args.last :] if args.last else ids


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--label")
    g.add_argument("--run", type=int, nargs="+")
    p.add_argument("--last", type=int, default=None, help="with --label: only the last N runs")
    args = p.parse_args()

    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        ids = run_ids(conn, args)
        rows = conn.execute(SELECT, (ids,)).fetchall()

        core, matched = [], []
        for (run_id, case_id, status, verdict, etype, fixed,
             exp_verdict, exp_etype, alts, fix_mode, acceptable) in rows:
            if status != "ok":
                core.append((None, None, run_id, case_id))
                continue
            verdict_ok = verdict == exp_verdict
            etype_ok = (etype == exp_etype or etype in alts) if exp_verdict == "broken" else None
            core.append((verdict_ok, etype_ok, run_id, case_id))
            if fix_mode == "any_of" and verdict == "broken" and fixed:
                if normalize_formula(fixed) in {normalize_formula(a) for a in acceptable}:
                    matched.append((run_id, case_id))

        cur = conn.cursor()
        cur.executemany(
            "UPDATE run_results SET verdict_ok = %s, error_type_ok = %s WHERE run_id = %s AND case_id = %s",
            core,
        )
        cur.executemany(
            "UPDATE run_results SET fix_ok = TRUE, fix_scored_by = 'normalized-match', "
            "fix_judge_note = NULL WHERE run_id = %s AND case_id = %s",
            matched,
        )
    print(f"scored {len(core)} answers in runs {ids}; exact fix matches: {len(matched)}; "
          f"the rest of the fixes wait for judge.py")


if __name__ == "__main__":
    main()
