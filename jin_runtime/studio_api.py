"""Authenticated HTTP API for JIN Studio annotation workflows."""
from __future__ import annotations

import hmac
import json
import os
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from jin_runtime.studio_contract import FIELD_CATALOG, StudioContractError
from jin_runtime.studio_security import (
    ROLE_PERMISSIONS,
    StudioPrincipal,
    StudioSecurityError,
    password_min_length,
    service_principal,
    session_duration_hours,
)
from jin_runtime.studio_service_auth import (
    configured_service_token,
    rotate_service_token,
    service_token_status,
)
from jin_runtime.studio_region_text import StudioRegionError, extract_pdf_region_text
from jin_runtime.studio_store import StudioConflictError, StudioNotFoundError, StudioStore


def _configured_service_token() -> str:
    return configured_service_token()


def _cookie_name() -> str:
    return str(os.getenv("JIN_STUDIO_SESSION_COOKIE") or "jin_studio_session").strip()


def _cookie_secure(request: Request) -> bool:
    configured = str(os.getenv("JIN_STUDIO_COOKIE_SECURE") or "auto").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    forwarded = str(request.headers.get("x-forwarded-proto") or "").lower()
    return request.url.scheme == "https" or forwarded == "https"


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0]
    if forwarded.strip():
        return forwarded.strip()[:128]
    return str(request.client.host if request.client else "")[:128]


def _authenticate(request: Request, repository: StudioStore) -> StudioPrincipal:
    session_token = str(request.cookies.get(_cookie_name()) or "").strip()
    if session_token:
        principal = repository.resolve_session(session_token)
        if principal is not None:
            return principal

    expected = _configured_service_token()
    header = str(request.headers.get("authorization") or "").strip()
    scheme, _, supplied = header.partition(" ")
    if (
        expected
        and scheme.lower() == "bearer"
        and supplied
        and hmac.compare_digest(supplied.strip(), expected)
    ):
        return service_principal()

    raise HTTPException(
        status_code=401,
        detail="Authentification JIN Studio requise",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorize(
    request: Request,
    repository: StudioStore,
    permission: str,
    *,
    csrf: bool = False,
) -> StudioPrincipal:
    principal = _authenticate(request, repository)
    if not principal.allows(permission):
        raise HTTPException(status_code=403, detail="Droit insuffisant pour cette action")
    if csrf and principal.auth_type == "session":
        supplied = str(request.headers.get("x-csrf-token") or "").strip()
        if not supplied or not hmac.compare_digest(supplied, principal.csrf_token):
            raise HTTPException(status_code=403, detail="Protection CSRF invalide")
    return principal


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


def _audit(
    repository: StudioStore,
    request: Request,
    principal: StudioPrincipal,
    action: str,
    target_type: str,
    target_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    repository.record_audit(
        actor_user_id=principal.user_id,
        actor_username=principal.username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        client_ip=_client_ip(request),
    )


def create_studio_router(store: StudioStore | None = None) -> APIRouter:
    repository = store or StudioStore()
    router = APIRouter(prefix="/studio", tags=["jin-studio"])

    @router.get("/auth/config")
    def auth_config() -> dict[str, Any]:
        return {
            "passwordMinLength": password_min_length(),
            "roles": [
                {"code": role, "permissions": sorted(permissions)}
                for role, permissions in ROLE_PERMISSIONS.items()
            ],
            "hasUsers": repository.user_count() > 0,
            "serviceAccessConfigured": bool(_configured_service_token()),
            "mcpEndpoint": "/mcp",
        }

    @router.post("/auth/login")
    async def login(request: Request) -> JSONResponse:
        payload = await _request_json_object(request, field="Connexion")
        username = str(payload.get("username") or "").strip().lower()
        user = repository.authenticate_user(username, str(payload.get("password") or ""))
        if user is None:
            repository.record_audit(
                actor_username=username or "unknown",
                action="auth.login_failed",
                target_type="session",
                client_ip=_client_ip(request),
            )
            raise HTTPException(status_code=401, detail="Identifiants invalides")
        token, principal = repository.create_session(
            user["userId"],
            client_ip=_client_ip(request),
            user_agent=str(request.headers.get("user-agent") or ""),
        )
        _audit(repository, request, principal, "auth.login", "session")
        response = JSONResponse({"user": principal.public(include_csrf=True)})
        response.set_cookie(
            key=_cookie_name(),
            value=token,
            max_age=60 * 60 * session_duration_hours(),
            httponly=True,
            secure=_cookie_secure(request),
            samesite="strict",
            path="/",
        )
        return response

    @router.get("/auth/me")
    def current_user(request: Request) -> dict[str, Any]:
        principal = _authenticate(request, repository)
        if principal.auth_type != "session":
            raise HTTPException(status_code=403, detail="Une session utilisateur est requise")
        return {"user": principal.public(include_csrf=True)}

    @router.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        principal = _authorize(request, repository, "studio.read", csrf=True)
        if principal.auth_type == "session":
            _audit(repository, request, principal, "auth.logout", "session")
            repository.revoke_session(principal.session_hash)
        response = JSONResponse({"loggedOut": True})
        response.delete_cookie(_cookie_name(), path="/")
        return response

    @router.post("/auth/change-password")
    async def change_password(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "studio.read", csrf=True)
        if principal.auth_type != "session":
            raise HTTPException(status_code=403, detail="Une session utilisateur est requise")
        payload = await _request_json_object(request, field="Mot de passe")
        if not repository.verify_user_password(
            principal.user_id, str(payload.get("currentPassword") or "")
        ):
            raise HTTPException(status_code=422, detail="Mot de passe actuel incorrect")
        try:
            repository.set_user_password(
                principal.user_id, str(payload.get("newPassword") or "")
            )
        except StudioSecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(repository, request, principal, "user.password_change", "user", principal.user_id)
        return {"updated": True, "reauthenticationRequired": True}

    @router.get("/status")
    def studio_status(request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        return {"enabled": True, "annotationSchemaVersion": 2, **repository.status()}

    @router.get("/field-catalog")
    def field_catalog(request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        return {"fields": list(FIELD_CATALOG)}

    @router.post("/documents", status_code=201)
    async def create_document(
        request: Request,
        file: UploadFile = File(...),
        extraction_json: str = Form("{}"),
        source_json: str = Form("{}"),
    ) -> dict[str, Any]:
        principal = _authorize(request, repository, "documents.write", csrf=True)
        try:
            document = repository.create_document(
                filename=file.filename or "document.pdf",
                pdf_bytes=await file.read(),
                extraction=_json_object(extraction_json, field="extraction_json"),
                source=_json_object(source_json, field="source_json"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(
            repository,
            request,
            principal,
            "document.create",
            "document",
            document["documentId"],
            {"filename": document["filename"]},
        )
        return document

    @router.get("/documents")
    def list_documents(request: Request, status: str = "", limit: int = 200) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        documents = repository.list_documents(status=status, limit=limit)
        summaries = [
            {key: value for key, value in document.items() if key != "extraction"}
            for document in documents
        ]
        return {"items": summaries, "count": len(summaries)}

    @router.get("/documents/{document_id}")
    def get_document(document_id: str, request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        try:
            return repository.get_document(document_id)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc

    @router.get("/documents/{document_id}/pdf")
    def get_document_pdf(document_id: str, request: Request) -> FileResponse:
        _authorize(request, repository, "studio.read")
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

    @router.post("/documents/{document_id}/region-text")
    async def document_region_text(document_id: str, request: Request) -> dict[str, Any]:
        """Read the value inside a human-drawn box, with OCR as a scan fallback."""
        _authorize(request, repository, "annotations.draft", csrf=True)
        payload = await _request_json_object(request, field="Zone PDF")
        try:
            path = repository.pdf_path(document_id)
            return extract_pdf_region_text(
                path,
                page_number=payload.get("page"),
                bbox=payload.get("bbox"),
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        except StudioRegionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.put("/documents/{document_id}/annotation", status_code=201)
    async def save_annotation(document_id: str, request: Request) -> dict[str, Any]:
        payload = await _request_json_object(request, field="Annotation")
        validated = str(payload.get("status") or "DRAFT").strip().upper() == "HUMAN_VALIDATED"
        permission = "annotations.validate" if validated else "annotations.draft"
        principal = _authorize(request, repository, permission, csrf=True)
        payload["reviewed_by"] = principal.username
        try:
            annotation = repository.save_annotation(document_id, payload)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        except StudioContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(
            repository,
            request,
            principal,
            "annotation.validate" if validated else "annotation.save_draft",
            "document",
            document_id,
            {"revision": annotation["revision"], "digest": annotation["annotation_digest"]},
        )
        return annotation

    @router.get("/documents/{document_id}/annotations")
    def list_annotations(document_id: str, request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        try:
            items = repository.list_annotations(document_id)
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Studio document not found") from exc
        return {"items": items, "count": len(items)}

    @router.get("/datasets")
    def list_datasets(request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        items = repository.list_datasets()
        return {"items": items, "count": len(items)}

    @router.post("/datasets", status_code=201)
    async def create_dataset(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "datasets.write", csrf=True)
        try:
            payload = await _request_json_object(request, field="Dataset request")
            dataset = repository.create_dataset(
                name=str(payload.get("name") or ""),
                document_ids=list(payload.get("documentIds") or []),
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="A selected document was not found") from exc
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(repository, request, principal, "dataset.create", "dataset", dataset["datasetId"])
        return dataset

    @router.get("/training-runs")
    def list_training_runs(request: Request) -> dict[str, Any]:
        _authorize(request, repository, "studio.read")
        items = repository.list_training_runs()
        return {"items": items, "count": len(items)}

    @router.post("/training-runs", status_code=201)
    async def prepare_training_run(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "training.write", csrf=True)
        try:
            payload = await _request_json_object(request, field="Training request")
            run = repository.prepare_training_run(
                dataset_id=str(payload.get("datasetId") or ""),
                model_family=str(payload.get("modelFamily") or "layoutlmv3"),
                base_model=str(payload.get("baseModel") or "microsoft/layoutlmv3-base"),
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(repository, request, principal, "training.queue", "training_run", run["runId"])
        return run

    @router.get("/users")
    def list_users(request: Request) -> dict[str, Any]:
        _authorize(request, repository, "users.manage")
        items = repository.list_users()
        return {"items": items, "count": len(items)}

    @router.get("/admin/service-token")
    def get_service_token_status(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "users.manage")
        if principal.auth_type != "session":
            raise HTTPException(status_code=403, detail="Une session ADMIN est requise")
        return service_token_status()

    @router.post("/admin/service-token/rotate")
    async def rotate_studio_service_token(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "users.manage", csrf=True)
        if principal.auth_type != "session":
            raise HTTPException(status_code=403, detail="Une session ADMIN est requise")
        payload = await _request_json_object(request, field="Rotation du jeton")
        if payload.get("confirm") is not True:
            raise HTTPException(
                status_code=422,
                detail="La révocation de l'ancien jeton doit être confirmée",
            )
        try:
            token = rotate_service_token()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _audit(
            repository,
            request,
            principal,
            "service_token.rotate",
            "service_token",
            details={"source": "file"},
        )
        return {"token": token, **service_token_status()}

    @router.post("/users", status_code=201)
    async def create_user(request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "users.manage", csrf=True)
        payload = await _request_json_object(request, field="Utilisateur")
        try:
            user = repository.create_user(
                username=str(payload.get("username") or ""),
                display_name=str(payload.get("displayName") or ""),
                password=str(payload.get("password") or ""),
                roles=list(payload.get("roles") or []),
                created_by=principal.username,
            )
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (StudioSecurityError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(repository, request, principal, "user.create", "user", user["userId"], {"roles": user["roles"]})
        return user

    @router.patch("/users/{user_id}")
    async def update_user(user_id: str, request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "users.manage", csrf=True)
        payload = await _request_json_object(request, field="Utilisateur")
        if "active" in payload and not isinstance(payload["active"], bool):
            raise HTTPException(status_code=422, detail="active doit être un booléen")
        try:
            previous = repository.get_user(user_id)
            user = repository.update_user(
                user_id,
                display_name=payload.get("displayName") if "displayName" in payload else None,
                roles=list(payload.get("roles") or []) if "roles" in payload else None,
                active=payload.get("active") if "active" in payload else None,
            )
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable") from exc
        except StudioConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (StudioSecurityError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(
            repository,
            request,
            principal,
            "user.update",
            "user",
            user_id,
            {
                "before": {
                    "displayName": previous["displayName"],
                    "roles": previous["roles"],
                    "active": previous["active"],
                },
                "after": {
                    "displayName": user["displayName"],
                    "roles": user["roles"],
                    "active": user["active"],
                },
            },
        )
        return user

    @router.post("/users/{user_id}/password")
    async def reset_user_password(user_id: str, request: Request) -> dict[str, Any]:
        principal = _authorize(request, repository, "users.manage", csrf=True)
        payload = await _request_json_object(request, field="Mot de passe")
        try:
            repository.set_user_password(user_id, str(payload.get("password") or ""))
        except StudioNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable") from exc
        except StudioSecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _audit(repository, request, principal, "user.password_reset", "user", user_id)
        return {"updated": True}

    @router.get("/audit-events")
    def list_audit_events(request: Request, limit: int = 200) -> dict[str, Any]:
        _authorize(request, repository, "audit.read")
        items = repository.list_audit_events(limit=limit)
        return {"items": items, "count": len(items)}

    return router


__all__ = ["create_studio_router"]
