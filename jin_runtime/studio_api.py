"""Authenticated HTTP API for JIN Studio annotation workflows."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from jin_runtime.studio_contract import FIELD_CATALOG, StudioContractError
from jin_runtime.studio_store import (
    StudioConflictError,
    StudioNotFoundError,
    StudioStore,
)


def _configured_token() -> str:
    return str(
        os.getenv("JIN_STUDIO_TOKEN")
        or os.getenv("JIN_FEEDBACK_TOKEN")
        or ""
    ).strip()


def _authorize(request: Request) -> None:
    expected = _configured_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="JIN Studio is disabled until JIN_STUDIO_TOKEN is configured",
        )
    header = str(request.headers.get("authorization") or "").strip()
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(
        supplied.strip(), expected
    ):
        raise HTTPException(status_code=401, detail="Invalid JIN Studio credentials")


def _json_object(raw: str, *, field: str) -> dict[str, Any]:
    if not str(raw or "").strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{field} must be a JSON object")
    return value


async def _request_json_object(request: Request, *, field: str) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{field} must be a JSON object")
    return value


def create_studio_router(store: StudioStore | None = None) -> APIRouter:
    repository = store or StudioStore()
    router = APIRouter(prefix="/studio", tags=["jin-studio"])

    @router.get("/status")
    def studio_status(request: Request) -> dict[str, Any]:
        _authorize(request)
        return {
            "enabled": True,
            "annotationSchemaVersion": 2,
            **repository.status(),
        }

    @router.get("/field-catalog")
    def field_catalog(request: Request) -> dict[str, Any]:
        _authorize(request)
        return {"fields": list(FIELD_CATALOG)}

    @router.post("/documents", status_code=201)
    async def create_document(
        request: Request,
        file: UploadFile = File(...),
        extraction_json: str = Form("{}"),
        source_json: str = Form("{}"),
    ) -> dict[str, Any]:
        _authorize(request)
        try:
            document = repository.create_document(
                filename=file.filename or "document.pdf",
                pdf_bytes=await file.read(),
                extraction=_json_object(extraction_json, field="extraction_json"),
                source=_json_object(source_json, field="source_json"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return document

    @router.get("/documents")
    def list_documents(
        request: Request,
        status: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        _authorize(request)
        documents = repository.list_documents(status=status, limit=limit)
        summaries = [
            {key: value for key, value in document.items() if key != "extraction"}
            for document in documents
        ]
        return {"items": summaries, "count": len(summaries)}

    @router.get("/documents/{document_id}")
    def get_document(document_id: str, request: Request) -> dict[str, Any]:
        _authorize(request)
        try:
            return repository.get_document(document_id)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc

    @router.get("/documents/{document_id}/pdf")
    def get_document_pdf(document_id: str, request: Request) -> FileResponse:
        _authorize(request)
        try:
            document = repository.get_document(document_id)
            path = repository.pdf_path(document_id)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=document["filename"],
            content_disposition_type="inline",
        )

    @router.put("/documents/{document_id}/annotation", status_code=201)
    async def save_annotation(document_id: str, request: Request) -> dict[str, Any]:
        _authorize(request)
        payload = await _request_json_object(request, field="Annotation")
        try:
            return repository.save_annotation(document_id, payload)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        except StudioContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/documents/{document_id}/annotations")
    def list_annotations(document_id: str, request: Request) -> dict[str, Any]:
        _authorize(request)
        try:
            items = repository.list_annotations(document_id)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        return {"items": items, "count": len(items)}

    @router.get("/datasets")
    def list_datasets(request: Request) -> dict[str, Any]:
        _authorize(request)
        items = repository.list_datasets()
        return {"items": items, "count": len(items)}

    @router.post("/datasets", status_code=201)
    async def create_dataset(request: Request) -> dict[str, Any]:
        _authorize(request)
        try:
            payload = await _request_json_object(request, field="Dataset request")
            return repository.create_dataset(
                name=str(payload.get("name") or ""),
                document_ids=list(payload.get("documentIds") or []),
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="A selected document was not found") from exc
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/training-runs")
    def list_training_runs(request: Request) -> dict[str, Any]:
        _authorize(request)
        items = repository.list_training_runs()
        return {"items": items, "count": len(items)}

    @router.post("/training-runs", status_code=201)
    async def prepare_training_run(request: Request) -> dict[str, Any]:
        _authorize(request)
        try:
            payload = await _request_json_object(request, field="Training request")
            return repository.prepare_training_run(
                dataset_id=str(payload.get("datasetId") or ""),
                model_family=str(payload.get("modelFamily") or "layoutlmv3"),
                base_model=str(
                    payload.get("baseModel") or "microsoft/layoutlmv3-base"
                ),
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router


__all__ = ["create_studio_router"]
