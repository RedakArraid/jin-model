from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from jin_runtime import __version__
from jin_runtime.learning import StatisticalAddressLearner
from uda.api import app as core_app

app = FastAPI(
    title="JIN Model Runtime",
    version=__version__,
    description="Compatibility runtime adding statistical learning on top of the packaged JIN core engine.",
)
learner = StatisticalAddressLearner()


def _forward_headers(request: Request) -> dict[str, str]:
    blocked = {"host", "content-length", "connection", "transfer-encoding"}
    return {k: v for k, v in request.headers.items() if k.lower() not in blocked}


async def _call_core(request: Request, path: str | None = None) -> httpx.Response:
    body = await request.body()
    transport = httpx.ASGITransport(app=core_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jin-core") as client:
        return await client.request(
            request.method,
            path or request.url.path,
            params=list(request.query_params.multi_items()),
            headers=_forward_headers(request),
            content=body,
            timeout=None,
        )


def _response_from_core(response: httpx.Response) -> Response:
    headers: dict[str, str] = {}
    content_type = response.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    return Response(content=response.content, status_code=response.status_code, headers=headers)


@app.on_event("startup")
def _startup_learning() -> None:
    if learner.feedback_path.exists() and not learner.status(compact=True)["trained"]:
        learner.train()


@app.get("/learning/status")
def learning_status() -> dict[str, Any]:
    return learner.status()


@app.post("/feedback")
def record_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        status = learner.record_feedback(payload, retrain=True)
        return {"accepted": True, "learning": status}
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=422, content={"accepted": False, "detail": str(exc)})


@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request) -> Response:
    response = await _call_core(request, "/health")
    if response.status_code >= 400 or "application/json" not in response.headers.get("content-type", ""):
        return _response_from_core(response)
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _response_from_core(response)
    if isinstance(payload, dict):
        payload["runtime_layer"] = {
            "version": __version__,
            "statistical_learning": learner.status(compact=True),
        }
    return JSONResponse(status_code=response.status_code, content=payload)


@app.api_route("/extract", methods=["POST"])
async def extract(request: Request) -> Response:
    response = await _call_core(request, "/extract")
    if response.status_code >= 400 or "application/json" not in response.headers.get("content-type", ""):
        return _response_from_core(response)
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return _response_from_core(response)
    if isinstance(payload, dict):
        payload = learner.enrich(payload)
        payload.setdefault("runtime_layer_version", __version__)
    return JSONResponse(status_code=response.status_code, content=payload)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_other_core_routes(request: Request, path: str) -> Response:
    return _response_from_core(await _call_core(request, "/" + path))
