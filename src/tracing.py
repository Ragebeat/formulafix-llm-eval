"""Optional tracing to Phoenix (OpenTelemetry spans with OpenInference attribute names).

Off unless PHOENIX_COLLECTOR_ENDPOINT is set (e.g. http://127.0.0.1:6006) AND init_tracing()
is called: the API does it at startup, run_golden.py only with --trace. When it is off, every
helper below is a cheap no-op, so the evaluation numbers and the CI run are not affected.

One trace per analyze() call:  analyze (CHAIN)  ->  llm (LLM)
Traces go into the Phoenix project PHOENIX_PROJECT_NAME (default "formulafix"), next to
other projects in the same Phoenix. Golden runs set session.id = "run-<id>", so one run's
cases are grouped in the Sessions view; formulafix.case_id finds a case across runs.
"""

import json
import os
import sys
from contextlib import contextmanager
from urllib.parse import urlparse

import config  # noqa: F401  (loads .env)

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL = True
except ImportError:  # tracing is optional: the service must start without the packages
    _OTEL = False

_provider = None


class _NoSpan:
    def set_attribute(self, *a, **k) -> None: ...
    def set_status(self, *a, **k) -> None: ...


def enabled() -> bool:
    return _provider is not None


def init_tracing(project: str | None = None) -> bool:
    """Start exporting spans to Phoenix. Returns False (and says why) when it cannot."""
    global _provider
    if _provider is not None:
        return True
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()
    if not endpoint:
        print("tracing: PHOENIX_COLLECTOR_ENDPOINT is not set, tracing stays off", file=sys.stderr)
        return False
    if not _OTEL:
        print("tracing: run `uv add opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`",
              file=sys.stderr)
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    name = project or os.environ.get("PHOENIX_PROJECT_NAME", "formulafix")
    provider = TracerProvider(
        resource=Resource.create({"openinference.project.name": name, "service.name": "formulafix"})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
    trace.set_tracer_provider(provider)
    _provider = provider
    print(f"tracing: on, project {name!r}, {url}", file=sys.stderr)
    return True


def shutdown() -> None:
    """Flush what is still queued; call before the process exits."""
    if _provider is not None:
        _provider.shutdown()


def _clean(attrs: dict | None) -> dict:
    return {k: v for k, v in (attrs or {}).items() if v is not None}


@contextmanager
def span(name: str, attributes: dict | None = None):
    if _provider is None:
        yield _NoSpan()
        return
    with trace.get_tracer("formulafix").start_as_current_span(
        name, attributes=_clean(attributes)
    ) as s:
        yield s


def _set(sp, attrs: dict) -> None:
    for k, v in _clean(attrs).items():
        sp.set_attribute(k, v)


def _fail(sp, text: str) -> None:
    if _OTEL and _provider is not None:
        sp.set_status(Status(StatusCode.ERROR, text[:200]))


def root_attrs(formula: str, context: str, dialect: str, extra: dict | None) -> dict:
    return {
        "openinference.span.kind": "CHAIN",
        "input.value": json.dumps({"formula": formula, "context": context, "dialect": dialect},
                                  ensure_ascii=False),
        "input.mime_type": "application/json",
        "formulafix.dialect": dialect,
        **(extra or {}),
    }


def root_done(sp, out) -> None:
    """Final answer on the root span. A model-format failure is an ERROR span: easy to filter."""
    r = out.result
    _set(sp, {
        "formulafix.status": out.status,
        "formulafix.verdict": r.verdict if r else None,
        "formulafix.error_type": r.error_type if r else None,
        "formulafix.confidence": r.confidence if r else None,
        "formulafix.attempts": out.attempts,
        "formulafix.latency_ms": out.latency_ms,
        "formulafix.issues": "; ".join(out.issues) if out.issues else None,
        "output.value": json.dumps(r.model_dump(), ensure_ascii=False) if r else out.raw_text,
        "output.mime_type": "application/json" if r else "text/plain",
    })
    if out.status != "ok":
        _fail(sp, f"{out.status}: {out.error_detail or ''}")


def llm_attrs(cfg, system: str, user: str) -> dict:
    return {
        "openinference.span.kind": "LLM",
        "llm.model_name": cfg.model,
        "llm.provider": urlparse(cfg.base_url).hostname,
        "llm.invocation_parameters": json.dumps(
            {"temperature": cfg.temperature, "max_tokens": cfg.max_tokens}),
        "llm.input_messages.0.message.role": "system",
        "llm.input_messages.0.message.content": system,
        "llm.input_messages.1.message.role": "user",
        "llm.input_messages.1.message.content": user,
        "input.value": user,
    }


def llm_done(sp, reply) -> None:
    _set(sp, {
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.content": reply.text,
        "output.value": reply.text,
        "llm.token_count.prompt": reply.input_tokens,
        "llm.token_count.completion": reply.output_tokens,
        "llm.token_count.completion_details.reasoning": reply.reasoning_tokens,
        "llm.cost.total": reply.cost_usd,
        "formulafix.finish_reason": reply.finish_reason,
        "formulafix.served_by": reply.served_by,
        "formulafix.latency_ms": reply.latency_ms,
        "formulafix.attempts": reply.attempts,
    })


def llm_error(sp, e) -> None:
    _set(sp, {"formulafix.status": e.status, "formulafix.attempts": e.attempts,
              "formulafix.latency_ms": e.latency_ms})
    _fail(sp, f"{e.status}: {e.detail}")
