"""Authenticated Streamable HTTP MCP facade for JIN Studio."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac
import os
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

from jin_runtime import __version__
from jin_runtime.studio_contract import FIELD_CATALOG
from jin_runtime.studio_service_auth import configured_service_token
from jin_runtime.studio_store import StudioStore


MCP_ACTOR = "service:jin-studio-mcp"


def _enabled(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or ("1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _csv_setting(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in str(os.getenv(name) or default).split(",") if item.strip()]


def _mcp_token() -> str:
    return str(os.getenv("JIN_STUDIO_MCP_TOKEN") or "").strip() or configured_service_token()


class BearerTokenMiddleware:
    """Small static-token boundary for private/server-to-server MCP deployments."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        expected = _mcp_token()
        header = Headers(scope=scope).get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if not expected:
            response = JSONResponse(
                {"error": "JIN Studio MCP machine token is not configured"},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        if not (
            scheme.lower() == "bearer"
            and supplied
            and hmac.compare_digest(supplied.strip(), expected)
        ):
            response = JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _audit(
    store: StudioStore,
    action: str,
    target_type: str,
    target_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    store.record_audit(
        actor_user_id=MCP_ACTOR,
        actor_username="mcp-service",
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        client_ip="mcp",
    )


def _merged_fields(
    current: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    replace_all: bool,
) -> list[dict[str, Any]]:
    if replace_all:
        return list(incoming)
    merged = [dict(field) for field in current]
    positions = {str(field.get("path") or ""): index for index, field in enumerate(merged)}
    for field in incoming:
        if not isinstance(field, dict):
            raise ValueError("Every field annotation must be an object")
        path = str(field.get("path") or "").strip()
        if not path:
            raise ValueError("Every field annotation requires a canonical path")
        if path in positions:
            merged[positions[path]] = dict(field)
        else:
            positions[path] = len(merged)
            merged.append(dict(field))
    return merged


def create_mcp_server(store: StudioStore | None = None) -> MCPServer:
    repository = store or StudioStore()
    server = MCPServer(
        "jin-studio",
        title="JIN Studio",
        version=__version__,
        description="Review document annotations and queue versioned training datasets.",
        instructions=(
            "Save machine-produced changes as drafts. Human validation is a distinct "
            "explicit step. Validated annotations do not affect the runtime until a "
            "dataset is created, a candidate is trained, evaluated and promoted."
        ),
    )

    @server.tool()
    def get_workflow_status() -> dict[str, Any]:
        """Return Studio counts and the explicit annotation-to-model workflow."""
        return {
            "studio": repository.status(),
            "workflow": [
                "save_annotation_draft",
                "human_validate_annotation",
                "create_versioned_dataset",
                "queue_candidate_training",
                "evaluate_candidate",
                "promote_candidate_manually",
            ],
            "automatic_runtime_update": False,
            "minimum_training_documents": int(
                os.getenv("JIN_STUDIO_MIN_TRAINING_DOCUMENTS") or "20"
            ),
        }

    @server.tool()
    def list_field_catalog() -> dict[str, Any]:
        """List every canonical annotation label accepted by JIN Studio."""
        return {"fields": list(FIELD_CATALOG), "count": len(FIELD_CATALOG)}

    @server.tool()
    def list_documents(
        status: str = "",
        limit: int = 100,
        campaign_id: str = "",
    ) -> dict[str, Any]:
        """List Studio documents, optionally filtered by state and campaign."""
        documents = repository.list_documents(status=status, limit=limit)
        if campaign_id:
            documents = [
                item
                for item in documents
                if str((item.get("source") or {}).get("campaignId") or "") == campaign_id
            ]
        summaries = [
            {key: value for key, value in item.items() if key != "extraction"}
            for item in documents
        ]
        return {"items": summaries, "count": len(summaries)}

    @server.tool()
    def get_document(document_id: str, include_extraction: bool = False) -> dict[str, Any]:
        """Read a document and its latest annotation; extraction is opt-in because it may be large."""
        document = repository.get_document(document_id)
        if not include_extraction:
            document.pop("extraction", None)
        return document

    @server.tool()
    def save_annotation_draft(
        document_id: str,
        field_annotations: list[dict[str, Any]],
        note: str = "",
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Merge fields by canonical path and save an immutable DRAFT revision."""
        current_document = repository.get_document(document_id)
        latest = current_document.get("latestAnnotation") or {}
        fields = _merged_fields(
            list(latest.get("field_annotations") or []),
            field_annotations,
            replace_all=replace_all,
        )
        annotation = repository.save_annotation(
            document_id,
            {
                "status": "DRAFT",
                "reviewed_by": "mcp-service",
                "note": note or str(latest.get("note") or ""),
                "field_annotations": fields,
            },
        )
        _audit(
            repository,
            "annotation.save_draft",
            "document",
            document_id,
            {"revision": annotation["revision"], "fieldCount": len(fields)},
        )
        return annotation

    @server.tool()
    def validate_annotation(
        document_id: str,
        field_annotations: list[dict[str, Any]],
        human_reviewer: str,
        confirmed_human_review: bool,
        note: str = "",
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Validate ground truth only when remote validation was explicitly enabled by an administrator."""
        if not _enabled("JIN_STUDIO_MCP_ALLOW_VALIDATION"):
            raise ValueError(
                "Remote MCP validation is disabled; validate in JIN Studio or set "
                "JIN_STUDIO_MCP_ALLOW_VALIDATION=1 after a security review."
            )
        reviewer = str(human_reviewer or "").strip()
        if not confirmed_human_review or len(reviewer) < 3:
            raise ValueError("A named human reviewer and explicit attestation are required")
        current_document = repository.get_document(document_id)
        latest = current_document.get("latestAnnotation") or {}
        fields = _merged_fields(
            list(latest.get("field_annotations") or []),
            field_annotations,
            replace_all=replace_all,
        )
        annotation = repository.save_annotation(
            document_id,
            {
                "status": "HUMAN_VALIDATED",
                "reviewed_by": reviewer,
                "note": note or str(latest.get("note") or ""),
                "field_annotations": fields,
            },
        )
        _audit(
            repository,
            "annotation.validate_remote",
            "document",
            document_id,
            {
                "revision": annotation["revision"],
                "humanReviewer": reviewer,
                "fieldCount": len(fields),
            },
        )
        return annotation

    @server.tool()
    def list_datasets() -> dict[str, Any]:
        """List immutable versioned datasets created from human-validated documents."""
        items = repository.list_datasets()
        return {"items": items, "count": len(items)}

    @server.tool()
    def create_versioned_dataset(name: str, document_ids: list[str]) -> dict[str, Any]:
        """Create a dataset; every selected document must already be HUMAN_VALIDATED."""
        dataset = repository.create_dataset(name=name, document_ids=document_ids)
        _audit(repository, "dataset.create", "dataset", dataset["datasetId"])
        return dataset

    @server.tool()
    def list_training_runs(limit: int = 100) -> dict[str, Any]:
        """List queued, running, completed and failed candidate training runs."""
        items = repository.list_training_runs()[: max(1, min(1000, int(limit)))]
        return {"items": items, "count": len(items)}

    @server.tool()
    def queue_candidate_training(
        dataset_id: str,
        confirm_queue: bool,
        model_family: str = "layoutlmv3",
        base_model: str = "microsoft/layoutlmv3-base",
    ) -> dict[str, Any]:
        """Queue compute for a READY dataset; confirm_queue must be true."""
        if not confirm_queue:
            raise ValueError("Set confirm_queue=true to start a training job")
        run = repository.prepare_training_run(
            dataset_id=dataset_id,
            model_family=model_family,
            base_model=base_model,
        )
        _audit(repository, "training.queue", "training_run", run["runId"])
        return run

    return server


def create_http_app(server: MCPServer | None = None) -> Starlette:
    active_server = server or create_mcp_server()
    security = TransportSecuritySettings(
        allowed_hosts=_csv_setting(
            "JIN_STUDIO_MCP_ALLOWED_HOSTS",
            "mcp,mcp:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*",
        ),
        allowed_origins=_csv_setting("JIN_STUDIO_MCP_ALLOWED_ORIGINS"),
    )
    mcp_app = active_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )

    async def health(_request: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "jin-studio-mcp",
                "tokenConfigured": bool(_mcp_token()),
            }
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with active_server.session_manager.run():
            yield

    return Starlette(
        routes=[Route("/health", health), Mount("/", app=mcp_app)],
        middleware=[Middleware(BearerTokenMiddleware)],
        lifespan=lifespan,
    )


mcp = create_mcp_server()
app = create_http_app(mcp)


def main() -> None:
    uvicorn.run(
        app,
        host=str(os.getenv("JIN_STUDIO_MCP_HOST") or "0.0.0.0"),
        port=int(os.getenv("JIN_STUDIO_MCP_PORT") or "8001"),
        log_level=str(os.getenv("JIN_STUDIO_MCP_LOG_LEVEL") or "info").lower(),
    )


if __name__ == "__main__":
    main()


__all__ = ["app", "create_http_app", "create_mcp_server", "mcp"]
