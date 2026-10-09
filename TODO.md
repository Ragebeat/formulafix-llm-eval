# TODO (FormulaFix)

Project is finished (see README). Nothing open except optional items.

## Done
- hidden_cause metric: rubric v1 (cause_ok) and v2 (cause_named, action_ok), both stored; v2 written after seeing v1.
- E1 (T=0) and E2 (v2_locale): measured, not adopted. See EXPERIMENTS.md.
- Qwen validation runs 16, 19, 20 complete.
- Tracing (Phoenix), README (EN/RU/JA).

## Optional
- Set data/validation_v1.jsonl read-only: Set-ItemProperty data\validation_v1.jsonl -Name IsReadOnly -Value $true
- Orphan 16-case run under label deepseek-v2locale (not deleted): always use --run 25 26 27 for E2 golden.
- Judge falls back to LLM_BASE_URL/LLM_API_KEY if no JUDGE_*: run without ENV_FILE or set JUDGE_BASE_URL/JUDGE_API_KEY.
- Untested ideas: judge that also reads the explanation when the formula is unchanged; JSON mode / larger max_tokens against `truncated`.
