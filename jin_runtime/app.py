from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from jin_runtime import __version__
from jin_runtime.learning import StatisticalAddressLearner
from jin_runtime.output_quality import audit_and_repair_output
from jin_runtime.document_router import CorpusDocumentRouter
from jin_runtime.pdf_cpu_router import CpuPdfRouter
from uda.api import app as core_app

app = FastAPI(
    title="JIN Model Runtime",
    version=__version__,
    description="Compatibility runtime adding statistical learning on top of the packaged JIN core engine.",
)
learner = StatisticalAddressLearner()
document_router = CorpusDocumentRouter()
pdf_router = CpuPdfRouter()


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


@app.post("/learning/document-route")
def document_route(payload: dict[str, Any]) -> dict[str, Any]:
    prediction = document_router.predict(
        filename=str(payload.get("filename") or ""),
        subject=str(payload.get("subject") or ""),
        sender=str(payload.get("sender") or ""),
        text=str(payload.get("text") or payload.get("text_preview") or ""),
    )
    return {"prediction": prediction, "router": document_router.status()}


async def _predict_uploaded_pdf(request: Request) -> dict[str, Any] | None:
    if not pdf_router.loaded or "multipart/form-data" not in request.headers.get("content-type", ""):
        return None
    try:
        form = await request.form()
        uploaded = form.get("file")
        if uploaded is None or not hasattr(uploaded, "read"):
            return None
        data = await uploaded.read()
        if not data.startswith(b"%PDF-"):
            return None
        return pdf_router.predict_bytes(data)
    except Exception:
        return None


@app.post("/learning/pdf-route")
async def pdf_document_route(file: UploadFile = File(...)) -> Response:
    if not pdf_router.loaded:
        return JSONResponse(
            status_code=503,
            content={"detail": "CPU PDF router model is not mounted", "router": pdf_router.status()},
        )
    data = await file.read()
    try:
        prediction = pdf_router.predict_bytes(data)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "router": pdf_router.status()})
    return JSONResponse(content={"prediction": prediction, "router": pdf_router.status()})


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
            "corpus_document_router": document_router.status(),
            "cpu_pdf_router": pdf_router.status(),
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
        payload = audit_and_repair_output(payload, repair=True)
        payload = learner.enrich(payload)
        route = document_router.predict_payload(payload)
        if route:
            payload["document_statistical_router"] = route
        pdf_route = await _predict_uploaded_pdf(request)
        if pdf_route:
            payload["document_pdf_router"] = pdf_route
        payload.setdefault("runtime_layer_version", __version__)
    return JSONResponse(status_code=response.status_code, content=payload)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_other_core_routes(request: Request, path: str) -> Response:
    return _response_from_core(await _call_core(request, "/" + path))
