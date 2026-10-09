"""FastAPI service. Port of the Node POST /api/analyze, without quotas, auth and Stripe.

  uv run uvicorn api:app --app-dir src --port 8000
  http://localhost:8000/docs   <- Swagger page, generated from the Pydantic models

Differences from the Node original, on purpose:
- failures are told apart in the HTTP status (429 rate limit, 504 timeout, 502
  anything else) and in "error"; the Node version answered 502 for all of them
- invalid input is 400 with the field named, as in Node, via a handler below
"""

import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

import config  # noqa: F401  (loads .env)
import prompts
import tracing
from analyze import analyze
from llm import LLMConfig
from schema import AnalyzeResult

PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "v1_node_parity")
ERROR_STATUS = {"rate_limited": 429, "timeout": 504}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cfg = LLMConfig.from_env()
    app.state.template, app.state.prompt_sha = prompts.load_template(PROMPT_VERSION)
    tracing.init_tracing()  # no-op unless PHOENIX_COLLECTOR_ENDPOINT is set
    async with httpx.AsyncClient() as client:
        app.state.client = client
        yield
    tracing.shutdown()


app = FastAPI(title="FormulaFix", lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    formula: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
    context: str = ""
    dialect: Literal["excel", "sheets"] = "excel"

    @field_validator("context", mode="before")
    @classmethod
    def _clean_context(cls, v: object) -> object:
        if v is None:
            return ""
        return v.strip()[:2000] if isinstance(v, str) else v  # Node: trim, then slice(0, 2000)


class Meta(BaseModel):
    model: str
    prompt_version: str
    latency_ms: int
    attempts: int


class AnalyzeResponse(AnalyzeResult):
    model_config = ConfigDict(populate_by_name=True)
    meta: Meta = Field(alias="_meta")


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        {"field": ".".join(str(x) for x in e["loc"][1:]) or "body", "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=400, content={"error": "invalid_request", "fields": fields})


@app.get("/health")
async def health(request: Request) -> dict:
    return {
        "status": "ok",
        "model": request.app.state.cfg.model,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": request.app.state.prompt_sha,
    }


@app.post("/analyze", response_model=AnalyzeResponse, response_model_by_alias=True)
async def analyze_endpoint(req: AnalyzeRequest, request: Request):
    st = request.app.state
    out = await analyze(
        st.client, st.cfg, st.template, req.formula, req.context, req.dialect,
        trace_attrs={"formulafix.source": "api", "formulafix.prompt": PROMPT_VERSION},
    )
    if out.status != "ok" or out.result is None:
        return JSONResponse(
            status_code=ERROR_STATUS.get(out.status, 502),
            content={"error": out.status, "message": "Something went wrong. Please try again."},
        )
    meta = Meta(
        model=st.cfg.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=out.latency_ms,
        attempts=out.attempts,
    )
    return AnalyzeResponse(**{**out.result.model_dump(), "_meta": meta})
