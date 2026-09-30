from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, Response

from jin_runtime import __version__
from jin_runtime.document_router import CorpusDocumentRouter
from jin_runtime.learning import StatisticalAddressLearner
from jin_runtime.pdf_cpu_router import CpuPdfRouter
from jin_runtime.weak_field_router import WeakFieldRouter

app = FastAPI(
    title="JIN Model Standalone Learning API",
    version=__version__,
    description=(
        "Standalone CPU API for JIN document routing, PDF routing, "
        "field/zone/cell suggestions and optional statistical feedback learning."
    ),
)

learner = StatisticalAddressLearner()
document_router = CorpusDocumentRouter()
pdf_router = CpuPdfRouter()
field_router = WeakFieldRouter()


@app.on_event("startup")
def startup_learning() -> None:
    if learner.feedback_path.exists() and not learner.status(compact=True)["trained"]:
        learner.train()


@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "runtime_layer_version": __version__,
        "mode": "standalone",
        "models": {
            "statistical_address_memory": learner.status(compact=True),
            "corpus_document_router": document_router.status(),
            "cpu_pdf_router": pdf_router.status(),
            "weak_field_router": field_router.status(),
        },
        "ready": bool(pdf_router.loaded and field_router.loaded),
    }


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


async def _pdf_bytes(file: UploadFile) -> bytes:
    data = await file.read()
    if not data.startswith(b"%PDF-"):
        raise ValueError("Input is not a PDF binary")
    return data


@app.post("/learning/pdf-route")
async def pdf_document_route(file: UploadFile = File(...)) -> Response:
    if not pdf_router.loaded:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "CPU PDF router model is not installed",
                "router": pdf_router.status(),
            },
        )
    try:
        prediction = pdf_router.predict_bytes(await _pdf_bytes(file))
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(content={"prediction": prediction, "router": pdf_router.status()})


@app.post("/learning/field-route")
async def field_document_route(file: UploadFile = File(...)) -> Response:
    if not field_router.loaded:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "CPU weak field router model is not installed",
                "router": field_router.status(),
            },
        )
    try:
        prediction = field_router.predict_bytes(await _pdf_bytes(file))
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(content={"prediction": prediction, "router": field_router.status()})


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)) -> Response:
    try:
        data = await _pdf_bytes(file)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    payload: dict[str, Any] = {
        "runtime_layer_version": __version__,
        "mode": "standalone",
    }

    if pdf_router.loaded:
        try:
            payload["document_pdf_router"] = pdf_router.predict_bytes(data)
        except (ValueError, RuntimeError) as exc:
            payload["document_pdf_router_error"] = str(exc)
    else:
        payload["document_pdf_router"] = None

    if field_router.loaded:
        try:
            payload["weak_field_suggestions"] = field_router.predict_bytes(data)
        except (ValueError, RuntimeError) as exc:
            payload["weak_field_suggestions_error"] = str(exc)
    else:
        payload["weak_field_suggestions"] = None

    return JSONResponse(content=payload)


@app.post("/feedback")
def record_feedback(payload: dict[str, Any]) -> Response:
    try:
        status = learner.record_feedback(payload, retrain=True)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "detail": str(exc)},
        )
    return JSONResponse(content={"accepted": True, "learning": status})
