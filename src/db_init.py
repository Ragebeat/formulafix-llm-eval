"""Create the tables. Safe to run again (CREATE ... IF NOT EXISTS).

  uv run python src/db_init.py

golden_cases : the frozen golden dataset (loaded by load_golden.py)
runs         : one row per pass over the cases — the configuration that produced it
run_results  : one row per case per run — the answer, timings, tokens, scoring

Comparing two configurations is then plain SQL over run_results.
"""

import psycopg

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS golden_cases (
    id                  text PRIMARY KEY,
    dialect             text NOT NULL,
    formula             text NOT NULL,
    context             text NOT NULL,
    category            text NOT NULL,
    expected_verdict    text NOT NULL,
    expected_error_type text,
    alt_error_types     text[] NOT NULL,
    fix_mode            text NOT NULL,
    acceptable_fixes    text[] NOT NULL,
    fix_criteria        text NOT NULL,
    label_certainty     text NOT NULL,
    split               text NOT NULL,
    note                text NOT NULL,
    case_group          text                -- validation set only: locale | hidden_cause | ...
);

CREATE TABLE IF NOT EXISTS runs (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    label          text NOT NULL,      -- human name: 'node-parity', 'v2-temp0', ...
    provider       text NOT NULL,
    model          text NOT NULL,      -- exact id, e.g. qwen/qwen3.8-27b
    temperature    double precision,
    max_tokens     integer,
    prompt_version text NOT NULL,
    prompt_sha256  text NOT NULL,
    golden_sha256  text NOT NULL,      -- which golden file this run saw
    split_filter   text,               -- 'dev' | 'heldout' | NULL = all
    note           text,
    extra_body     text                -- LLM_EXTRA_BODY used: provider options such as reasoning off
);

CREATE TABLE IF NOT EXISTS run_results (
    run_id           bigint NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    case_id          text   NOT NULL REFERENCES golden_cases(id),
    -- ok | rate_limited | timeout | api_error | invalid_json | truncated | schema_violation
    -- rate_limited / timeout / api_error are INFRASTRUCTURE failures (--resume redoes
    -- them); invalid_json / truncated / schema_violation are the MODEL's answer in a
    -- bad format and are kept. All are reported separately from wrong answers.
    status           text   NOT NULL,
    verdict          text,
    error_type       text,
    whats_broken     text,
    why              text,
    fixed_formula    text,
    what_changed     text,
    if_ok_suggestion text,
    confidence       double precision,
    issues           text[] NOT NULL DEFAULT '{}',   -- AnalyzeResult.consistency_issues()
    raw_text         text,
    error_detail     text,
    latency_ms       integer,
    input_tokens     integer,
    output_tokens    integer,
    reasoning_tokens integer,
    attempts         integer,          -- calls made for this case (retries on 429/5xx/timeout)
    finish_reason    text,             -- 'stop' | 'length' (cut off by max_tokens) | NULL if unknown
    served_by        text,             -- upstream provider that answered (OpenRouter), else NULL
    cost_usd         double precision, -- cost of the call when the provider reports it
    -- scoring, filled by the eval step
    verdict_ok       boolean,
    error_type_ok    boolean,
    fix_ok           boolean,
    fix_scored_by    text,             -- 'normalized-match' | 'rule:no-fix' | 'judge:<model>'
    fix_judge_note   text,             -- the judge's one-sentence reason, or 'judge_failed: ...'
    PRIMARY KEY (run_id, case_id)
);

-- for a database created before `attempts` existed
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS attempts integer;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS fix_judge_note text;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS finish_reason text;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS served_by text;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS cost_usd double precision;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS extra_body text;
ALTER TABLE golden_cases ADD COLUMN IF NOT EXISTS case_group text;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS cause_ok boolean;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS cause_judge_note text;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS cause_named boolean;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS action_ok boolean;
ALTER TABLE run_results ADD COLUMN IF NOT EXISTS cause_v2_note text;
"""


def main() -> None:
    with psycopg.connect(config.database_url()) as conn:
        conn.execute(SCHEMA)
    print("schema ready: golden_cases, runs, run_results")


if __name__ == "__main__":
    main()
