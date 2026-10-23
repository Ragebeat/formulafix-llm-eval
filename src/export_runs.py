"""Run results as files: export every run from Postgres to data/runs/, or import them back.

The runs live in Postgres (runs + run_results). Exported, they are committed with the
repository, so every number in the README can be recomputed on a fresh clone without a
single LLM call:

  uv run python src/export_runs.py                 # database -> data/runs/<id>-<label>.json
  uv run python src/export_runs.py --import        # data/runs/*.json -> an empty database

Import needs the schema and both case sets loaded first (db_init.py, load_golden.py,
load_golden.py --set validation): run_results references golden_cases. Run ids are kept,
so `report.py --run 25 26 27` means the same runs everywhere.
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

import config

RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "runs"


def _plain(row: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def export() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(config.database_url(), row_factory=dict_row) as conn:
        runs = conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        for run in runs:
            results = conn.execute(
                "SELECT * FROM run_results WHERE run_id = %s ORDER BY case_id", (run["id"],)).fetchall()
            label = re.sub(r"[^\w.-]+", "_", run["label"])
            path = RUNS_DIR / f"{run['id']:03d}-{label}.json"
            path.write_text(json.dumps({"run": _plain(run), "results": [_plain(r) for r in results]},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{path.name}: {len(results)} results")
    print(f"exported {len(runs)} runs -> {RUNS_DIR}")


def _insert(conn, table: str, row: dict) -> None:
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
                 " ON CONFLICT DO NOTHING", [row[c] for c in cols])


def import_() -> None:
    files = sorted(RUNS_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"no run files in {RUNS_DIR}")
    with psycopg.connect(config.database_url()) as conn:
        for f in files:
            d = json.loads(f.read_text(encoding="utf-8"))
            _insert(conn, "runs", d["run"])
            for r in d["results"]:
                _insert(conn, "run_results", r)
        # explicit ids were inserted: move the sequence past them for new runs
        conn.execute("SELECT setval('runs_id_seq', (SELECT max(id) FROM runs))")
    print(f"imported {len(files)} runs from {RUNS_DIR}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--import", dest="do_import", action="store_true", help="load data/runs/*.json into the database")
    a = p.parse_args()
    import_() if a.do_import else export()
