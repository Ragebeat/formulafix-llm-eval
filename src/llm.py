"""Chat client for any OpenAI-compatible endpoint (Groq, OpenRouter, ...).

One call. No retries unless the caller asks for them: the API endpoint behaves
like the Node original (a failure is a failure), the eval runner asks for
retries so that a rate limit does not become a "wrong answer".

A failure is classified, not just raised, because the causes need different
handling and must not be mixed in the metrics:
  rate_limited : HTTP 429 (provider limits, not the model's fault)
  timeout      : no answer within timeout_s
  api_error    : any other transport or HTTP failure
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx

import config  # noqa: F401  (loads .env into the environment)

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def _json_env(name: str) -> dict:
    """A JSON object from an environment variable (provider options), or {}."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{name} is not valid JSON: {e}")
    if not isinstance(value, dict):
        raise SystemExit(f"{name} must be a JSON object")
    return value


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout_s: float = 30.0  # the Node version used AbortSignal.timeout(30000)
    extra_body: dict = field(default_factory=dict)     # provider options merged into the request
    extra_headers: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        key = os.environ.get("LLM_API_KEY")
        if not key:
            raise SystemExit("Set LLM_API_KEY in .env")
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            api_key=key,
            model=os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b"),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "1024")),
            timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "30")),
            extra_body=_json_env("LLM_EXTRA_BODY"),
            extra_headers=_json_env("LLM_EXTRA_HEADERS"),
        )


@dataclass
class LLMReply:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None  # thinking models spend output tokens on reasoning
    latency_ms: int               # the successful attempt only, waits between retries excluded
    attempts: int
    finish_reason: str | None = None  # "stop", or "length" when cut off by max_tokens
    served_by: str | None = None      # which upstream provider answered (OpenRouter reports it)
    cost_usd: float | None = None     # cost of this call, when the provider reports it

class LLMError(Exception):
    def __init__(self, status: str, detail: str, attempts: int, latency_ms: int):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.attempts = attempts
        self.latency_ms = latency_ms


async def complete(
    client: httpx.AsyncClient,
    cfg: LLMConfig,
    system: str,
    user: str,
    *,
    max_retries: int = 0,
    extra_body: dict | None = None,
) -> LLMReply:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **cfg.extra_body,
        **(extra_body or {}),
    }
    headers = {"authorization": f"Bearer {cfg.api_key}", **cfg.extra_headers}

    attempt = 0
    while True:
        attempt += 1
        started = time.perf_counter()
        retryable = False
        wait = 20.0
        try:
            r = await client.post(url, json=body, headers=headers, timeout=cfg.timeout_s)
        except httpx.TimeoutException:
            status, detail, retryable = "timeout", f"no answer in {cfg.timeout_s:g}s", True
        except httpx.HTTPError as e:
            status, detail, retryable = "api_error", f"{type(e).__name__}: {e}"[:300], True
        else:
            ms = int((time.perf_counter() - started) * 1000)
            if r.status_code == 200:
                data = r.json()
                choices = data.get("choices") or [{}]
                text = (choices[0].get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                details = usage.get("completion_tokens_details") or {}
                return LLMReply(
                    text=text,
                    model=data.get("model") or cfg.model,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    reasoning_tokens=details.get("reasoning_tokens"),
                    latency_ms=ms,
                    attempts=attempt,
                    finish_reason=choices[0].get("finish_reason"),
                    served_by=data.get("provider") if isinstance(data.get("provider"), str) else None,
                    cost_usd=usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None,
                )
            status = "rate_limited" if r.status_code == 429 else "api_error"
            detail = f"HTTP {r.status_code}: {r.text[:300]}"
            retryable = r.status_code in RETRYABLE_HTTP
            if r.status_code == 429 and "per day" in r.text.lower():
                # tokens/requests per DAY: waiting will not help, do not burn time on retries
                retryable = False
                detail = "DAILY LIMIT: " + detail
            retry_after = r.headers.get("retry-after")
            if retry_after:
                try:
                    wait = min(float(retry_after) + 1.0, 90.0)
                except ValueError:
                    pass
        ms = int((time.perf_counter() - started) * 1000)
        if not retryable or attempt > max_retries:
            raise LLMError(status, detail, attempt, ms)
        await asyncio.sleep(wait)
