"""Run the golden dataset through the analyzer and store every answer.

  uv run python src/run_golden.py --label smoke --limit 5
  uv run python src/run_golden.py --label node-parity --repeat 3
  uv run python src/run_golden.py --label node-parity --resume 4     # finish an interrupted run

One run = one pass over the cases = one row in `runs` plus one row per case in
`run_results`, written as it goes (a crash keeps what was done). --repeat N makes
N separate runs: one comparison is not a conclusion, the noise has to be measured.

Three rate_limited answers in a row stop the run (a daily limit makes the rest
pointless); --resume ID later redoes only the cases whose call FAILED for
infrastructure reasons (rate_limited, timeout, api_error) or never ran, inside the
same run, with the same model and prompt. invalid_json and schema_violation are
the model's own answers and are kept: re-rolling them would hide how often the
model breaks the format.

Scoring (verdict / error type / fix correct) is a separate step: this script only
records what the model said, how long it took and what it cost.
"""

import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse

import httpx
import psycopg

import config
import prompts
import tracing
from analyze import Outcome, analyze
from llm import LLMConfig

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

STOP_AFTER_RATE_LIMITED = 3

INSERT_RUN = """
INSERT INTO runs (label, provider, model, temperature, max_tokens, prompt_version,
                  prompt_sha256, golden_sha256, split_filter, note, extra_body)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
"""

INSERT_RESULT = """
INSERT INTO run_results (run_id, case_id, status, verdict, error_type, whats_broken, why,
    fixed_formula, what_changed, if_ok_suggestion, confidence, issues, raw_text, error_detail,
    latency_ms, input_tokens, output_tokens, reasoning_tokens, attempts, finish_reason,
    served_by, cost_usd)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def fetch_cases(conn: psycopg.Connection, split: str, limit: int | None,
                skip_ok_in_run: int | None = None) -> list[tuple]:
    sql = "SELECT id, dialect, formula, context FROM golden_cases WHERE true"
    params: list = []
    if split == "all":
        sql += " AND split IN ('dev', 'heldout')"  # the validation set is never part of "all"
    else:
        sql += " AND split = %s"
        params.append(split)
    if skip_ok_in_run is not None:
        sql += " AND id NOT IN (SELECT case_id FROM run_results WHERE run_id = %s)"
        params.append(skip_ok_in_run)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def store(conn: psycopg.Connection, run_id: int, case_id: str, o: Outcome) -> None:
    r = o.result
    conn.execute(
        INSERT_RESULT,
        (
            run_id, case_id, o.status,
            r.verdict if r else None,
            r.error_type if r else None,
            r.whats_broken if r else None,
            r.why if r else None,
            r.fixed_formula if r else None,
            r.what_changed if r else None,
            r.if_ok_suggestion if r else None,
            r.confidence if r else None,
            o.issues, o.raw_text, o.error_detail,
            o.latency_ms, o.input_tokens, o.output_tokens, o.reasoning_tokens, o.attempts,
            o.finish_reason, o.served_by, o.cost_usd,
        ),
    )


def open_run(conn, cfg: LLMConfig, args, prompt_sha: str, note: str) -> tuple[int, list[tuple]]:
    """A new run, or (with --resume) the existing one minus the cases already answered."""
    if args.resume:
        run_id = args.resume
        row = conn.execute("SELECT model, prompt_version FROM runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            raise SystemExit(f"run {run_id} not found")
        if (row[0], row[1]) != (cfg.model, args.prompt):
            raise SystemExit(
                f"run {run_id} was made with model {row[0]!r}, prompt {row[1]!r}; "
                f"now {cfg.model!r}, {args.prompt!r}. Resuming with another setup would mix configurations."
            )
        conn.execute(
            "DELETE FROM run_results WHERE run_id = %s "
            "AND status IN ('rate_limited', 'timeout', 'api_error')", (run_id,))
        cases = fetch_cases(conn, args.split, args.limit, skip_ok_in_run=run_id)
        if not cases:
            raise SystemExit(f"run {run_id} has nothing left to do")
        print(f"--- resuming run {run_id}: {len(cases)} cases left, model {cfg.model}")
        return run_id, cases

    cases = fetch_cases(conn, args.split, args.limit)
    if not cases:
        raise SystemExit("no cases in golden_cases: run load_golden.py first")
    run_id = conn.execute(
        INSERT_RUN,
        (
            args.label, urlparse(cfg.base_url).hostname, cfg.model, cfg.temperature,
            cfg.max_tokens, args.prompt, prompt_sha,
            config.sha256_of(config.VALIDATION_PATH if args.split == "validation" else config.GOLDEN_PATH),
            None if args.split == "all" else args.split, note,
            json.dumps(cfg.extra_body) if cfg.extra_body else None,
        ),
    ).fetchone()[0]
    print(f"--- run {run_id}: {args.label}, {len(cases)} cases, model {cfg.model}")
    return run_id, cases


async def run_once(conn, cfg: LLMConfig, args, template: str, prompt_sha: str, note: str) -> bool:
    """Returns False if the run was stopped early (repeated rate limits)."""
    run_id, cases = open_run(conn, cfg, args, prompt_sha, note)
    completed = True
    streak = 0

    async with httpx.AsyncClient() as client:
        for i, (case_id, dialect, formula, context) in enumerate(cases):
            # Node trimmed both before the call; same here
            o = await analyze(
                client, cfg, template, formula.strip(), context.strip(), dialect,
                max_retries=args.retries,
                trace_attrs={
                    "session.id": f"run-{run_id}", "formulafix.run_id": run_id,
                    "formulafix.case_id": case_id, "formulafix.run_label": args.label,
                    "formulafix.prompt": args.prompt, "formulafix.source": "golden",
                },
            )
            store(conn, run_id, case_id, o)
            r = o.result
            print(
                f"[{run_id}] {case_id} {o.status:<16} {(r.verdict if r else '-'):<11} "
                f"{(r.error_type if r and r.error_type else '-'):<9} {o.latency_ms:>6}ms x{o.attempts}"
            )
            streak = streak + 1 if o.status == "rate_limited" else 0
            if streak >= STOP_AFTER_RATE_LIMITED:
                print(f"STOP: {streak} rate_limited in a row (a daily limit?). "
                      f"Continue later with: --resume {run_id} --label {args.label}")
                completed = False
                break
            if i < len(cases) - 1:
                await asyncio.sleep(args.delay)

    rows = conn.execute(
        "SELECT status, count(*) FROM run_results WHERE run_id = %s GROUP BY status ORDER BY status",
        (run_id,),
    ).fetchall()
    print(f"run {run_id} {'done' if completed else 'STOPPED'}: " + ", ".join(f"{s}={n}" for s, n in rows))
    return completed


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True, help="human name of the configuration, e.g. node-parity")
    p.add_argument("--split", choices=["all", "dev", "heldout", "validation"], default="all")
    p.add_argument("--prompt", default="v1_node_parity", help="file name in prompts/ without .txt")
    p.add_argument("--repeat", type=int, default=1, help="number of separate runs")
    p.add_argument("--resume", type=int, default=None, metavar="RUN_ID",
                   help="finish this run: redo the cases without an ok answer")
    p.add_argument("--retries", type=int, default=3, help="retries on 429/5xx/timeout per case")
    p.add_argument("--delay", type=float, default=4.0, help="seconds between cases (provider rate limits)")
    p.add_argument("--limit", type=int, default=None, help="first N cases only (smoke test)")
    p.add_argument("--note", default=None)
    p.add_argument("--trace", action="store_true",
                   help="send traces to Phoenix (needs PHOENIX_COLLECTOR_ENDPOINT)")
    args = p.parse_args()
    if args.resume:
        args.repeat = 1

    if args.trace:
        tracing.init_tracing()
    cfg = LLMConfig.from_env()
    template, prompt_sha = prompts.load_template(args.prompt)
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        for k in range(1, args.repeat + 1):
            note = args.note or ""
            if args.repeat > 1:
                note = f"{note} (repeat {k}/{args.repeat})".strip()
            if not await run_once(conn, cfg, args, template, prompt_sha, note):
                break
    tracing.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
