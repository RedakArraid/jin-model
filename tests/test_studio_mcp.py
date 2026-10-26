from __future__ import annotations

from fastapi.testclient import TestClient

from jin_runtime.studio_mcp import create_http_app, create_mcp_server
from jin_runtime.studio_store import StudioStore


PDF = b"%PDF-1.4\n% JIN Studio MCP test\n%%EOF\n"


def _headers(token: str = "mcp-test-secret") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _rpc(client: TestClient, method: str, params: dict | None = None, call_id: int = 1):
    return client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": method,
            "params": params or {},
        },
    )


def test_mcp_http_boundary_requires_bearer_token(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_MCP_TOKEN", "mcp-test-secret")
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOWED_HOSTS", "testserver,testserver:*")
    app = create_http_app(create_mcp_server(StudioStore(tmp_path / "studio")))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        unauthorized = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"


def test_mcp_lists_documents_and_saves_audited_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_MCP_TOKEN", "mcp-test-secret")
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOWED_HOSTS", "testserver,testserver:*")
    store = StudioStore(tmp_path / "studio")
    document = store.create_document(
        filename="commande.pdf",
        pdf_bytes=PDF,
        extraction={"order": {"purchase_order": {"number": "PO-123"}}},
    )
    app = create_http_app(create_mcp_server(store))

    with TestClient(app) as client:
        initialized = _rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
        assert initialized.status_code == 200, initialized.text
        protocol = initialized.json()["result"]["protocolVersion"]
        rpc_headers = _headers() | {"MCP-Protocol-Version": protocol}

        tools = client.post(
            "/mcp",
            headers=rpc_headers,
            json={"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
        )
        tool_names = {item["name"] for item in tools.json()["result"]["tools"]}
        assert {
            "list_annotation_masks",
            "create_annotation_mask",
            "apply_annotation_mask",
        } <= tool_names

        listed = client.post(
            "/mcp",
            headers=rpc_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_documents", "arguments": {"limit": 10}},
            },
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["result"]["structuredContent"]["count"] == 1

        saved = client.post(
            "/mcp",
            headers=rpc_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "save_annotation_draft",
                    "arguments": {
                        "document_id": document["documentId"],
                        "field_annotations": [
                            {
                                "path": "order.purchase_order.number",
                                "label": "ORDER_NUMBER",
                                "status": "UNREVIEWED",
                                "extracted_value": "PO-123",
                                "corrected_value": None,
                                "evidence": {
                                    "page": 1,
                                    "bbox": [100, 100, 250, 130],
                                },
                            }
                        ],
                    },
                },
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["result"]["structuredContent"]["status"] == "DRAFT"

    assert store.get_document(document["documentId"])["latestAnnotation"]["revision"] == 1
    assert store.list_audit_events()[0]["action"] == "annotation.save_draft"


def test_remote_mcp_validation_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_MCP_TOKEN", "mcp-test-secret")
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOWED_HOSTS", "testserver,testserver:*")
    monkeypatch.delenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", raising=False)
    store = StudioStore(tmp_path / "studio")
    document = store.create_document(filename="commande.pdf", pdf_bytes=PDF)
    app = create_http_app(create_mcp_server(store))

    with TestClient(app) as client:
        initialized = _rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
        protocol = initialized.json()["result"]["protocolVersion"]
        result = client.post(
            "/mcp",
            headers=_headers() | {"MCP-Protocol-Version": protocol},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "validate_annotation",
                    "arguments": {
                        "document_id": document["documentId"],
                        "field_annotations": [],
                        "human_reviewer": "reviewer.test",
                        "confirmed_human_review": True,
                    },
                },
            },
        )

    assert result.status_code == 200
    assert result.json()["result"]["isError"] is True
    assert store.get_document(document["documentId"])["status"] == "DRAFT"
