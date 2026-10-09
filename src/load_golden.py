"""Validate a case file and load it into Postgres.

  uv run python src/load_golden.py --check                 # golden_v1: validation only, no database
  uv run python src/load_golden.py                         # golden_v1: validate, then upsert
  uv run python src/load_golden.py --set validation --file data/validation_raw.jsonl --check
  uv run python src/load_golden.py --set validation        # data/validation_v1.jsonl: validate, upsert

Two sets, kept apart: the golden set (g001..g080, split dev / heldout) and the validation
set (v001..v035, split validation) that is never used while tuning. Both files are frozen
(read-only) once checked: the success criteria and the metrics were fixed before any run,
and every run stores the file's sha256, so a changed file cannot pass unnoticed.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import psycopg
from pydantic import ValidationError

import config
from schema import GoldenCase, ValidationCase
from score import normalize_formula

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GOLDEN_QUOTAS = {
    "#N/A": 6, "#REF!": 6, "#VALUE!": 6, "#NAME?": 5, "#DIV/0!": 5, "#SPILL!": 4,
    "circular": 5, "syntax": 7, "logic": 6, "ok": 15, "unparseable": 5, "locale": 10,
}
HELDOUT_TOTAL = 20
HELDOUT_MIN = {c: 1 for c in GOLDEN_QUOTAS} | {"ok": 2, "locale": 2}

VALIDATION_QUOTAS = {"locale": 12, "hidden_cause": 8, "valid_unusual": 8, "generic": 7}
VALIDATION_TOTAL = 35


def read_cases(path: Path, model) -> tuple[list, list[str]]:
    cases, errors = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(model.model_validate_json(line))
        except ValidationError as e:
            parts = [
                (".".join(map(str, err["loc"])) + ": " if err["loc"] else "") + err["msg"]
                for err in e.errors()
            ]
            errors.append(f"line {n}: " + " | ".join(parts))
    return cases, errors


def _duplicates(cases) -> list[str]:
    problems = []
    for what, values in (
        ("id", [c.id for c in cases]),
        ("formula", [normalize_formula(c.formula) for c in cases]),
    ):
        dups = [v for v, k in Counter(values).items() if k > 1]
        if dups:
            problems.append(f"duplicate {what}: {dups}")
    return problems


def check_golden(cases: list[GoldenCase]) -> list[str]:
    problems = _duplicates(cases)
    by_cat = Counter(c.category for c in cases)
    for cat, quota in GOLDEN_QUOTAS.items():
        if by_cat[cat] != quota:
            problems.append(f"category {cat}: {by_cat[cat]} cases, quota {quota}")
    heldout = [c for c in cases if c.split == "heldout"]
    if len(heldout) != HELDOUT_TOTAL:
        problems.append(f"heldout: {len(heldout)} cases, expected {HELDOUT_TOTAL}")
    held_by_cat = Counter(c.category for c in heldout)
    for cat, minimum in HELDOUT_MIN.items():
        if held_by_cat[cat] < minimum:
            problems.append(f"heldout category {cat}: {held_by_cat[cat]}, minimum {minimum}")
    return problems


def check_validation(cases: list[ValidationCase]) -> list[str]:
    problems = _duplicates(cases)
    expected_ids = [f"v{n:03d}" for n in range(1, VALIDATION_TOTAL + 1)]
    if sorted(c.id for c in cases) != expected_ids:
        problems.append(f"ids are not v001..v{VALIDATION_TOTAL:03d} without gaps or extras")
    by_group = Counter(c.group for c in cases)
    for grp, quota in VALIDATION_QUOTAS.items():
        if by_group[grp] != quota:
            problems.append(f"group {grp}: {by_group[grp]} cases, quota {quota}")
    for c in cases:
        if c.group == "locale" and c.category != "locale":
            problems.append(f"{c.id}: group locale must have category locale")
        if c.group == "valid_unusual" and not (c.category == "ok" and c.expected_verdict == "ok"):
            problems.append(f"{c.id}: group valid_unusual must be category ok, verdict ok")
        if c.group == "hidden_cause" and c.expected_verdict != "broken":
            problems.append(f"{c.id}: group hidden_cause must be broken")
        if c.group == "generic" and c.category in ("ok", "unparseable", "locale"):
            problems.append(f"{c.id}: group generic must be an ordinary error category")
    # the whole point of the set is that it is new: no formula may repeat one from golden
    golden = {
        normalize_formula(json.loads(line)["formula"])
        for line in config.GOLDEN_PATH.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    }
    overlap = [c.id for c in cases if normalize_formula(c.formula) in golden]
    if overlap:
        problems.append(f"formula also in the golden set: {overlap}")
    return problems


def report(cases) -> None:
    print(f"cases: {len(cases)}")
    keys = [
        ("split", lambda c: c.split),
        ("dialect", lambda c: c.dialect),
        ("fix_mode", lambda c: c.fix_mode),
        ("label_certainty", lambda c: c.label_certainty),
    ]
    if hasattr(cases[0], "group"):
        keys.insert(0, ("group", lambda c: c.group))
    for title, key in keys:
        print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(map(key, cases)).items())))


def upsert(cases, model) -> None:
    fields = list(model.model_fields)
    cols = ["case_group" if f == "group" else f for f in fields]  # "group" is an SQL keyword
    marks = ", ".join(["%s"] * len(cols))
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    sql = (f"INSERT INTO golden_cases ({', '.join(cols)}) VALUES ({marks}) "
           f"ON CONFLICT (id) DO UPDATE SET {sets}")
    rows = [tuple(getattr(c, f) for f in fields) for c in cases]
    with psycopg.connect(config.database_url()) as conn:
        conn.cursor().executemany(sql, rows)
        all_rows = conn.execute("SELECT split, count(*) FROM golden_cases GROUP BY split ORDER BY split").fetchall()
    print("golden_cases in database: " + ", ".join(f"{s}={n}" for s, n in all_rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=["golden", "validation"], default="golden")
    parser.add_argument("--file", help="case file; default data/golden_v1.jsonl or data/validation_v1.jsonl")
    parser.add_argument("--check", action="store_true", help="validate only, do not touch the database")
    args = parser.parse_args()

    if args.set == "golden":
        model, check = GoldenCase, check_golden
        path = Path(args.file) if args.file else config.GOLDEN_PATH
    else:
        model, check = ValidationCase, check_validation
        path = Path(args.file) if args.file else config.VALIDATION_PATH
    if not path.exists():
        raise SystemExit(f"not found: {path}")

    cases, errors = read_cases(path, model)
    problems = errors + (check(cases) if not errors else [])
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  -", p)
        raise SystemExit(1)

    report(cases)
    print("sha256:", config.sha256_of(path))
    print("OK: schema, quotas and consistency are as planned")
    if not args.check:
        upsert(cases, model)


if __name__ == "__main__":
    main()
