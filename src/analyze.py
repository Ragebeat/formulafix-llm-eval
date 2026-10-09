"""The core: formula in, diagnosis out. Used by the API and by the eval runner.

analyze() never raises for model or provider trouble. It returns an Outcome
whose status says what happened, so that infrastructure failures (rate limit,
timeout) and format failures (broken JSON) are counted separately from wrong
answers:

  ok | rate_limited | timeout | api_error | invalid_json | truncated | schema_violation
"""

import json
import re
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

import prompts
import tracing
from llm import LLMConfig, LLMError, complete
from schema import AnalyzeResult


@dataclass
class Outcome:
    status: str
    result: AnalyzeResult | None = None
    raw_text: str | None = None
    error_detail: str | None = None
    latency_ms: int = 0
    attempts: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    issues: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    served_by: str | None = None
    cost_usd: float | None = None


def extract_json(text: str) -> dict:
    """Same recovery as the Node extractJson(): drop fences, take the text from the
    first "{" to the last "}". Kept identical on purpose (parity); how often it
    fails is a measured number, and a candidate for improvement in its own right."""
    cleaned = re.sub(r"```json", "", text, flags=re.IGNORECASE).replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in LLM response")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data


async def _analyze(
    client: httpx.AsyncClient,
    cfg: LLMConfig,
    template: str,
    formula: str,
    context: str,
    dialect: str,
    *,
    max_retries: int = 0,
) -> Outcome:
    system = prompts.system_prompt(template, dialect)
    user = prompts.user_message(formula, context)
    with tracing.span("llm", tracing.llm_attrs(cfg, system, user)) as llm_span:
        try:
            reply = await complete(client, cfg, system, user, max_retries=max_retries)
        except LLMError as e:
            tracing.llm_error(llm_span, e)
            return Outcome(
                status=e.status, error_detail=e.detail, latency_ms=e.latency_ms, attempts=e.attempts
            )
        tracing.llm_done(llm_span, reply)

    out = Outcome(
        status="ok",
        raw_text=reply.text,
        latency_ms=reply.latency_ms,
        attempts=reply.attempts,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        reasoning_tokens=reply.reasoning_tokens,
        finish_reason=reply.finish_reason,
        served_by=reply.served_by,
        cost_usd=reply.cost_usd,
    )
    try:
        data = extract_json(reply.text)
    except ValueError as e:  # includes json.JSONDecodeError
        if reply.finish_reason == "length":
            # the model ran into max_tokens before closing the JSON: a different
            # problem from garbage output, counted separately
            out.status = "truncated"
            out.error_detail = f"cut off at max_tokens ({reply.output_tokens} output tokens): {e}"[:300]
        else:
            out.status, out.error_detail = "invalid_json", str(e)[:300]
        return out
    try:
        out.result = AnalyzeResult.model_validate(data)
    except ValidationError as e:
        out.status = "schema_violation"
        out.error_detail = "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()
        )[:300]
        return out
    out.issues = out.result.consistency_issues()
    return out


async def analyze(
    client: httpx.AsyncClient,
    cfg: LLMConfig,
    template: str,
    formula: str,
    context: str,
    dialect: str,
    *,
    max_retries: int = 0,
    trace_attrs: dict | None = None,
) -> Outcome:
    """One analysis. With tracing on, it is one trace: analyze -> llm (see tracing.py)."""
    with tracing.span("analyze", tracing.root_attrs(formula, context, dialect, trace_attrs)) as root:
        out = await _analyze(client, cfg, template, formula, context, dialect, max_retries=max_retries)
        tracing.root_done(root, out)
        return out
