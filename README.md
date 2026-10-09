**English** | [Русский](README.ru.md) | [日本語](README.ja.md)

# FormulaFix

Paste a broken Excel or Google Sheets formula (`#REF!`, `#VALUE!`, `#N/A`, a circular reference, a typo in a function name), get a plain-language explanation of what is wrong and a corrected formula.

This repository is the LLM core of a small product, ported from Node/Next.js to Python/FastAPI and rebuilt around **measurement**: a golden dataset with reference diagnoses, an evaluation harness, run storage in Postgres, prompts under git. The product itself was shelved (spreadsheet vendors ship built-in fix assistants); the engineering is what this repository is for.

Same discipline as the [RAG project](../RAG): **every change is backed by a number on a fixed dataset, the success criteria are written down before the runs, and a single comparison of two LLM runs is not a conclusion.**

> **Status: finished.** The harness, the golden set (80 cases), a separate validation set (35 cases), the baseline, two pre-registered experiments (both measured, neither adopted) and tracing are done. The service is not deployed; this repository is about the measurement.

---

## Results

80 cases (60 dev + 20 held-out). Each row is the mean of 3 runs, with the min–max spread in brackets, plus control runs of the previous configuration (the model is noisy even at low temperature).

| Configuration | verdict | error type | fix correct | failed calls | p50 latency |
|---|---|---|---|---|---|
| Node prompt ported, `qwen/qwen3.8-27b` on Groq, T=0.2 (3 runs) | 0.742 (0.725–0.762) | 0.691 (0.691–0.691) | 0.448 (0.418–0.473), a lower bound (judge audit below) | 1 of 240 (cut off at `max_tokens`) | 0.55 s |
| Same prompt, `deepseek/deepseek-v4.1-flash` via OpenRouter (DeepInfra fp8), reasoning off, T=0.2 (3 runs) | 0.879 (0.850–0.900) | 0.776 (0.764–0.782) | 0.582 (0.564–0.600), a lower bound (judge audit below) | 1 of 240 (schema violation) | 3.1 s, about $0.009 per run |
| Same, DeepSeek, `temperature` 0 instead of 0.2 (3 runs) | 0.862 (0.850–0.875) | 0.752 (0.745–0.764) | 0.564 (0.545–0.582), a lower bound (judge audit below) | 0 of 240 | 3.1 s, about $0.008 per run |
| DeepSeek, T=0.2, the separator rule rewritten to depend on the locale (prompt `v2_locale`, not adopted, 3 runs) | 0.892 (0.888–0.900) | 0.776 (0.764–0.782) | not judged | 1 of 240 (invalid JSON) | 3.7 s, about $0.009 per run |

The first two rows use the identical prompt and cases; only the model and the provider differ. The best configuration is the DeepSeek one at T=0.2 with the original prompt: neither experiment beat it by more than the run-to-run spread (details under "What did not work" and in [EXPERIMENTS.md](EXPERIMENTS.md)). Wrong in every run (systematic errors): 15 of 80 cases for Qwen, 6 of 80 for DeepSeek; the 3 cases of the locale category (g075, g077, g078) are wrong for both models. Cases that change verdict or error type between runs (noise): 18 for Qwen, 9 for DeepSeek. For DeepSeek the systematic part is at most 7.5 percentage points of verdict error, so on this set a prompt change can barely be told apart from noise by verdict; the headroom is in error type and in fix correctness.

### Validation set (35 cases the prompt was never tuned on)

Written after the golden set was frozen, aimed at the weak spots found there (locale, a working formula next to a user complaint, valid but unusual syntax, ordinary errors with a twist). Labels were reviewed by hand before freezing: all debatable cases, a sample of eight, one tried in Excel 2013; two locale cases were rewritten to name the locale. Verdict accuracy, mean of 3 runs, the same prompt, `temperature` 0.2:

| Group (cases) | `qwen/qwen3.8-27b` (Groq) | `deepseek/deepseek-v4.1-flash` (DeepInfra fp8) |
|---|---|---|
| all (35) | 0.648 (0.629–0.686) | 0.781 (0.771–0.800) |
| locale (12) | 0.861 | 0.861 |
| hidden_cause (8) | 0.458 | 0.500 |
| valid_unusual (8) | 0.417 | 0.875 |
| hidden_cause: cause named in the explanation (8) | 0.792 | 0.917 |
| hidden_cause: an accepted action recommended (8) | 0.375 | 0.458 |
| generic (7) | 0.762 | 0.857 |
| stability | 0.829 | 0.943 |

The gap between the models holds on new data (13 points against 14 on the golden set, and the ranges do not overlap). Both models do worse than on the golden set (DeepSeek 0.879 → 0.781), so the golden numbers are the optimistic ones. The gap is almost entirely in `valid_unusual`: Qwen calls valid but unusual formulas broken. `locale` and `hidden_cause` are low for both models, and the same cases fail in both (v010, v013, v017, v021, v034), which points at the task and the prompt rules rather than the model. One case is 12.5 points inside a group of 8, so group numbers are directional only. For `hidden_cause` the verdict understates the product: the formula is valid and the real cause sits elsewhere, so models often name the cause but still answer `ok`. A judge from another model family reads the explanation and scores two things separately: is the cause named, and is an accepted action recommended. (The rubric was written after a first, combined version had been seen, which was stricter than its name; both are kept in the database.) The cause is named in 0.917 (DeepSeek) and 0.792 (Qwen) of the answers, but an accepted action is recommended in only 0.458 and 0.375. The models mostly understand what is wrong; the weak part is telling the user what to do, for example repeating the workaround the user already found, or rewriting a valid formula.


Held-out scores are higher than dev for both models (by 8–10 points), so the absolute held-out number is not comparable to dev, only changes between configurations on it are.

### Before the golden set existed

The Node version had a 20-case script with lenient expectations (lists of acceptable values, "not checked" entries), and the prompt had been adjusted against those same 20 cases. On the current model, three runs gave 17/20, 16/20, 16/20 (in run 3 one call failed with HTTP 502). Cases #1, #2 and #10 failed in every run; #18 flipped between runs. This is context, not a baseline: the cases were seen during prompt tuning, so they stay in the golden set only as *dev*.

---

## Architecture

```
POST /analyze  {formula, context, dialect}
  ↓ input check (formula 3–2000 chars, context ≤ 2000)
  ↓ prompt from prompts/<version>.txt (under git, sha256 recorded with every run)
  ↓ OpenAI-compatible chat call (Groq today; any provider by env)
  ↓ JSON recovery (same as the Node original) → Pydantic model
answer {verdict, error_type, whats_broken, why, fixed_formula, what_changed, ...}
```

Failures are classified, not just raised: `rate_limited` (HTTP 429), `timeout`, `api_error`, `invalid_json`, `truncated` (cut off at `max_tokens`), `schema_violation`. The HTTP status tells the infrastructure ones apart (429 / 504 / 502), and the evaluation stores all of them separately so that a provider rate limit is never counted as a wrong answer. Infrastructure failures are retried and can be re-run (`--resume`); format failures are the model's answer and are kept as they are, so the format-failure rate is not hidden by re-rolling.

Evaluation lives in Postgres (Docker):

| Table | One row is |
|---|---|
| `golden_cases` | a case of the golden set (`data/golden_v1.jsonl`) or of the validation set (`data/validation_v1.jsonl`); the files are the source of truth |
| `runs` | one pass over the cases: model id, temperature, prompt version and sha256, golden sha256 |
| `run_results` | one case in one run: the answer, status, latency, tokens, retries, scoring, the judge's reason |

Comparing two configurations is plain SQL over `run_results`.

### What was deliberately not ported

Quotas, authentication, Stripe, Supabase, SEO pages, legal pages. They are product plumbing, not the LLM problem.

---

## Method

### Rules fixed before the first run

- The golden dataset and the metrics come **before** any code and any tuning. Success criteria are written down before a run.
- One comparison of two LLM runs is not a conclusion. At least 3 runs of a configuration, plus control runs of the old configuration. A change counts only if it exceeds the spread of the control runs.
- No rules or dictionaries written against golden failures: the gain would be inflated. New changes are checked on the **held-out** slice, which tuning never sees.
- A fix to a general rule in a prompt often cures one case and breaks others. It is measured, not assumed.
- A second set (validation, 35 cases) was written after the golden set was frozen and is never used for tuning; it checks whether a result holds on data nobody tuned against.
- Experiments are pre-registered in [EXPERIMENTS.md](EXPERIMENTS.md): the change, the hypothesis, the adoption criteria and the order of runs (first check that the change works on the cheapest set that has the target, only then run the guards).

### Golden dataset

80 cases, frozen as `data/golden_v1.jsonl` (read-only; every run stores the file's sha256).

| Category | Cases |
|---|---|
| `#N/A` / `#REF!` / `#VALUE!` | 6 each |
| `#NAME?` / `#DIV/0!` / circular | 5 each |
| `#SPILL!` | 4 |
| syntax | 7 |
| logic | 6 |
| correct formulas (false-positive check, 5 of them look broken but are valid) | 15 |
| not a formula | 5 |
| locale and language (`;` separators, Cyrillic / Japanese / Chinese sheet names, missing `=`, full-width `＝`) | 10 |

48 Excel / 32 Google Sheets; 20 held-out, spread across categories; 8 cases flagged `debatable`.

Each case has an expected verdict, an expected error type, alternative acceptable error types (where the diagnosis depends on data the model cannot see), and a fix check: `any_of` (24 cases: a list of reference fixes), `judge` (31: written criteria), `none` (25).

**How it was labelled.** Generated and labelled by a stronger model (Claude Opus) in chat, from a written spec with quotas and a realism requirement (messy formulas, vague user context, user's wrong guesses about the cause). The model under test is Qwen, a different family. Then checked by a human: all `debatable` cases (9 at the time) plus 8 seeded-random ones, 17 in total. Three cases whose behaviour is a fact about the spreadsheet application were tried in a real one (two in Google Sheets; one could not be confirmed in Excel). Four cases changed as a result (see below).

### Metrics

- **verdict**: the answer's verdict equals the expected one.
- **error type**: among expected-broken cases, equals the expected type or one of its alternatives.
- **fix correct**: `any_of` cases first match a reference after normalization (whitespace, case, `FALSE` ≡ `0`; argument separators are *not* normalized, for the locale cases the separator is the fix). A non-match is not "wrong": it goes to the judge. `judge` cases always go to the judge.
- **failed calls** and **inconsistent answers** (for example `ok` together with an error type) are reported separately from wrong answers.
- **stability**: the share of cases with the same verdict and error type in every run.
- Every number is shown with its denominator, in two views: *answered* (among real answers) and *overall* (a failed call counts as not correct).

### The judge

Fixes that string matching cannot decide are judged by `openai/gpt-oss-120b`, a **different model family** from the one under test (a judge from the same family is blind in the same places). It sees the product, the formula, the context, the expected diagnosis and either the reference fixes or the written criteria, and the proposed formula only, not the model's explanation. Its one-sentence reason is stored with every verdict so that a sample can be read by hand.

**Audit (13 verdicts read by hand, 4 from Qwen and 9 from DeepSeek).** 10 were clearly right; none was a wrong "correct". 1 was stricter than the written criteria (it rejected `INDEX(DATEVALUE(...),0)`, which the criteria themselves allow). 2 cannot be decided by design: the model returned the formula unchanged and gave the remedy in prose ("move the formula outside A:F"), which the criteria accept but the judge, which sees only the formula, cannot credit. This happens in 1–3 of 55 answers per run (2–5%), for both models, so `fix correct` is a lower bound by roughly 1–3 cases per run and the comparison between configurations is not affected. A judge that also reads the explanation, but only when the formula is unchanged, is the fix if this rate ever changes.

---

## What did not work (so far)

- **The baseline model was retired mid-project.** Groq shut down `qwen/qwen3-32b` (deprecation announced 17 June 2026); its suggested replacement `qwen3.6-27b` was already missing from the model list when checked, and `qwen3.8-27b` was available. The "before" numbers had to be recomputed on `qwen/qwen3.8-27b`; the old test results are history only. The exact model id and date are recorded with every run.
- **A free-tier output-tokens-per-minute cap** (1,000 tokens/min on the on-demand tier for that model) rejects a request that asks for `max_tokens=1024` before it is generated. Found on the first Node run; it shapes how fast a golden run can go (a pause between cases, retries that honour `Retry-After`).
- **Exact string matching for fixes was too strict.** On the first smoke run a correct fix (`IF(A2:A="", "", VLOOKUP(...))`) was marked wrong because the reference wrote the empty result as an omitted argument. Patching the normalizer for that case would have been writing rules against a golden failure. The principle was changed instead, for all 24 `any_of` cases at once: a non-match goes to the judge. This was decided before looking at full-run metrics.
- **Four labels were wrong, ambiguous or unverifiable** and were fixed *before* freezing the set: a fix check that required the user's original range, which no model can see (g046); a fix check that accepted only one of several real causes of "the formula shows as text" (g079); a `syntax` label for what Google Sheets reports as an invalid-regex error, so `#VALUE!` became the expected type (g044); and a `#SPILL!` case (volatile-size `SEQUENCE`) that could not be confirmed in Excel (g032). The replacement written first for g032 duplicated an existing mechanism (listing the `#SPILL!` cases showed it); it was replaced by a different documented cause, merged cells in the spill range.
- **The model sometimes thinks out loud inside the JSON.** On one case the model opened a valid JSON object and then argued with itself inside the `why` string ("Wait, if I change the formula to ...") until it hit `max_tokens=1024`; the closing brace never came and the answer was lost. That is a format failure with a specific cause, so it has its own status (`truncated`, from the provider's `finish_reason`), separate from garbage output and from wrong answers. Candidate experiments: structured output / JSON mode, a larger token budget, an instruction against reasoning in the fields. Each is measured like any other change.
- **Routing through an aggregator adds noise that looks like model noise.** The first five test requests to the same model id were served by four different providers (the model has about 30 endpoints with fp4, fp8 and unspecified quantization). The provider that answered is now stored with every result, and the runs are pinned to one fp8 endpoint with fallbacks disabled (the model vendor's own endpoint was excluded by the account's data policy: it may train on paid-model prompts).
- **`temperature` 0 did not give the stability that was asked for, and it fixed the errors in place.** Criteria set before the run: success = stability ≥ 0.95 with the mean verdict ≥ 0.850; failure = stability < 0.93 or the mean verdict < 0.850. Result: stability 0.938 (was 0.886 at 0.2), mean verdict 0.862 (was 0.879): in between, so not counted as a success. The unstable cases fell from 9 to 5, but the cases wrong in every run rose from 6 to 10: at 0.2 the model sometimes got them right by chance, at 0 it is wrong every time. Verdict and fix correctness did not improve beyond the run-to-run spread, so the default stays at 0.2.
- **Rewriting one prompt rule for locales gained about one point and was not adopted.** The old rule said the model must accept both `,` and `;` as argument separators, which clashes with a user who says their Russian Excel rejects a formula written with commas. The rule was replaced by one that ties separators and the decimal comma to the locale; criteria were fixed before the runs (the change must fix the locale cases, including `g077` and `v010`, and cost at most two regressions elsewhere). Result: `v010` went from wrong in every run to right in every run, and two more locale cases improved; but `g077`, the case that motivated the change, stayed wrong, because the model now says the arguments are separated by `;` when the formula plainly uses commas: a reading error that a rule cannot fix. Two cases got worse (`g015`, `v019`), one of them plausibly because the model became more lenient about locale-looking input. Mean verdict: 0.892 against 0.879 on the golden set, unchanged on the validation set: inside the run-to-run spread. Swapping the model moved the verdict by 14 points; this rule moved it by about one. Not adopted; the Qwen confirmation run was skipped by the pre-agreed order (check the target first, run the guards only if it passes).
- *(more here as the experiments run: what a prompt change helped, and what it broke elsewhere)*

---

## Tracing

Optional, off by default. With a running [Phoenix](https://github.com/Arize-ai/phoenix) (any version that accepts OTLP over HTTP) set `PHOENIX_COLLECTOR_ENDPOINT=http://127.0.0.1:6006` in `.env`:

```powershell
uv run uvicorn api:app --app-dir src --port 8000          # the API traces every request
uv run python src/run_golden.py --label x --trace          # a golden run traces every case
```

One trace per request: an `analyze` span (formula, context, verdict, status) with an `llm` span inside (model, provider, token counts, finish reason, cost, the exact messages). Traces land in the Phoenix project `formulafix` (change with `PHOENIX_PROJECT_NAME`), next to other projects. A golden run is one Phoenix session (`run-<id>`), and `formulafix.case_id` finds one case across runs. Model-format failures (`invalid_json`, `truncated`, `schema_violation`) are ERROR spans. Without the endpoint, or without `--trace`, nothing is sent and the eval is unchanged.

![One trace in Phoenix: analyze span with the llm span inside](docs/img/phoenix-trace.png)

## Caveats

- 80 cases: one case is 1.25 percentage points. No confidence intervals; differences are judged against the spread of repeated runs.
- The golden set is labelled by an LLM and checked by a human only in part. Shared blind spots between the labeller and the judge (both are LLMs) cannot be excluded.
- The judge is a second opinion, not the truth. The hand audit covered only 13 verdicts: enough to see that it errs on the strict side, not enough to put an error rate on it.
- The validation set has 35 cases in four groups of 7–12; one case is 8–12 points inside a group, so group numbers are directional. Its labels were reviewed by hand, but one case (v012) stays debatable and v035 was verified only in Excel 2013.
- The `hidden_cause` rubric (cause named / action accepted) was written after a first combined version had been seen; both versions are stored. The judge reads the explanation only for this group.
- `fix correct` is a lower bound (see the judge audit).
- Groq models marked "preview" can be removed at short notice, so results are tied to a model id and a date.

---

## Run it

Needs: Python 3.14, [uv](https://docs.astral.sh/uv/), Docker.

```
cp .env.example .env                 # set POSTGRES_PASSWORD and LLM_API_KEY
uv sync
docker compose up -d
uv run python src/db_init.py
uv run python src/load_golden.py     # validates the golden set, then loads it

uv run uvicorn api:app --app-dir src --port 8000      # http://localhost:8000/docs

uv run python src/run_golden.py --label baseline --repeat 3
uv run python src/score.py  --label baseline
uv run python src/judge.py  --label baseline
uv run python src/report.py --label baseline

# validation set and the hidden_cause rubric
uv run python src/load_golden.py --set validation
uv run python src/run_golden.py --label baseline-val --split validation --repeat 3
uv run python src/score.py  --label baseline-val
uv run python src/judge.py  --label baseline-val
uv run python src/judge_cause.py --label baseline-val --rubric v2
```

The judge uses `JUDGE_BASE_URL` / `JUDGE_API_KEY` / `JUDGE_MODEL` when set, otherwise the endpoint of `LLM_BASE_URL` / `LLM_API_KEY`, so run it without `ENV_FILE` if the model under test lives on another provider. `ENV_FILE=.env.openrouter` selects a provider profile.

On Windows with Docker, connect to `127.0.0.1`, not `localhost` (the code does by default): `localhost` is tried as IPv6 first, the published port is IPv4 only, and a single script start took 130 s instead of 0.35 s.

`tools/check_prompt_parity.py` verifies that the Python prompt is word for word the Node one.

---

## Roadmap

- [x] Golden dataset v1 (80 cases), frozen
- [x] Postgres schema, loader with validation, prompt under git, parity check against the Node prompt
- [x] FastAPI service, run storage, scoring, report, judge
- [x] Baseline: 3 runs + control runs, scored and judged, judge sample audited by hand
- [x] Validation set (35 cases), never used for tuning, per-group report, `hidden_cause` rubric
- [x] Improvement experiments, one change at a time, 3 runs each (E1 temperature 0, E2 locale rule; neither adopted)
- [x] Tracing (OpenTelemetry spans viewed in Phoenix, opt-in)
- [x] Russian and Japanese README
- [ ] Not planned: CI regression tests and deployment (the product is shelved, there is no production to protect)
