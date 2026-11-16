from __future__ import annotations

import base64
import copy
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from jin_runtime.studio_mcp import create_http_app, create_mcp_server
from jin_runtime.studio_store import StudioConflictError, StudioStore
from jin_runtime.studio_transfer import CHUNK_BYTES, StudioDocumentTransfer, bundle_digest


PDF = b"%PDF-1.4\n" + b"sample PDF bytes\n" * 20000 + b"%%EOF\n"


@pytest.fixture
def transfer_pair(tmp_path):
    source = StudioStore(tmp_path / "source")
    target = StudioStore(tmp_path / "target")
    document = source.create_document(
        filename="REXEL_commande.pdf", pdf_bytes=PDF,
        extraction={"order": {"purchase_order": {"number": "PO-123"}}},
        source={"campaignId": "reviewed-campaign", "origin": "manual"},
    )
    source.save_annotation(document["documentId"], {
        "status": "DRAFT", "reviewed_by": "reviewer.test",
        "field_annotations": [{
            "path": "order.purchase_order.number", "label": "ORDER_NUMBER",
            "status": "CONFIRMED", "extracted_value": "PO-123", "corrected_value": None,
            "evidence": {"page": 1, "bbox": [100, 100, 250, 130]},
        }],
    })
    return StudioDocumentTransfer(source), StudioDocumentTransfer(target), document["documentId"]


def _upload_all(source, target, document_id, session):
    offset = session["received_bytes"]
    while offset < session["pdf_size"]:
        chunk = source.read_chunk(document_id, offset)
        session = target.upload(session["transfer_id"], offset, chunk["data_base64"], chunk["sha256"])
        offset = session["received_bytes"]
    return session


def test_roundtrip_preserves_pdf_extraction_history_and_review(transfer_pair, monkeypatch):
    source, target, document_id = transfer_pair
    payload = source.store.get_document(document_id)["latestAnnotation"]
    payload["status"] = "HUMAN_VALIDATED"
    source.store.save_annotation(document_id, payload)
    bundle = source.export(document_id)
    monkeypatch.delenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", raising=False)
    with pytest.raises(ValueError, match="ALLOW_VALIDATION"):
        target.begin(bundle, confirm_trusted_source=True)
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", "1")
    with pytest.raises(ValueError, match="confirm_trusted_source"):
        target.begin(bundle)
    session = target.begin(bundle, confirm_trusted_source=True)
    assert target.store.list_documents() == []
    session = _upload_all(source, target, document_id, session)
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", "0")
    with pytest.raises(ValueError, match="ALLOW_VALIDATION"):
        target.finish(session["transfer_id"], confirm_import=True)
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", "1")
    assert target.finish(session["transfer_id"], confirm_import=True)["state"] == "COMPLETE"
    assert target.store.pdf_path(document_id).read_bytes() == PDF
    assert target.store.get_document(document_id) == source.store.get_document(document_id)
    assert target.store.list_annotations(document_id) == source.store.list_annotations(document_id)
    assert target.export(document_id) == bundle  # includes original revision timestamps
    with target.store._connect() as connection:
        path = connection.execute("SELECT pdf_path FROM studio_documents").fetchone()[0]
    assert path == f"documents/{document_id}/source.pdf"
    assert target.store.list_audit_events()[0]["action"] == "document_transfer.complete"


def test_resume_retries_and_completed_transfer_do_not_overwrite(transfer_pair):
    source, target, document_id = transfer_pair
    bundle = source.export(document_id)
    session = target.begin(bundle)
    chunk = source.read_chunk(document_id, 0, 100)
    received = target.upload(session["transfer_id"], 0, chunk["data_base64"], chunk["sha256"])
    target = StudioDocumentTransfer(StudioStore(target.store.root))  # process restart
    assert target.begin(bundle) == received
    assert target.upload(session["transfer_id"], 0, chunk["data_base64"], chunk["sha256"]) == received
    with pytest.raises(ValueError, match="incomplete"):
        target.finish(session["transfer_id"], confirm_import=True)
    _upload_all(source, target, document_id, received)
    with pytest.raises(ValueError, match="confirm_import"):
        target.finish(session["transfer_id"])
    completed = target.finish(session["transfer_id"], confirm_import=True)
    payload = target.store.get_document(document_id)["latestAnnotation"]
    payload["note"] = "Local correction after transfer"
    target.store.save_annotation(document_id, payload)
    assert target.begin(bundle) == completed
    assert target.finish(session["transfer_id"], confirm_import=True) == completed
    assert target.store.get_document(document_id)["latestAnnotation"]["revision"] == 2
    with pytest.raises(ValueError, match="cannot be cancelled"):
        target.cancel(session["transfer_id"])
    source.store.save_annotation(document_id, payload)
    with pytest.raises(StudioConflictError, match="never overwrite"):
        target.begin(source.export(document_id))


@pytest.mark.parametrize("damage", ["digest", "document_id", "annotation_binding", "annotation_digest", "status", "size", "filename", "schema"])
def test_invalid_manifest_refused_before_writing(transfer_pair, damage):
    source, target, document_id = transfer_pair
    bundle = copy.deepcopy(source.export(document_id))
    if damage == "digest":
        bundle["document"]["filename"] = "changed.pdf"
    elif damage == "document_id":
        bundle["document"]["documentId"] = "../../outside"
    elif damage == "annotation_binding":
        bundle["annotations"][0]["annotation"]["document_sha256"] = "0" * 64
    elif damage == "annotation_digest":
        bundle["annotations"][0]["annotation"]["field_annotations"][0]["extracted_value"] = "modified"
    elif damage == "status":
        bundle["document"]["status"] = "HUMAN_VALIDATED"
    elif damage == "size":
        bundle["pdf_size"] = 101 * 1024 * 1024
    elif damage == "filename":
        bundle["document"]["filename"] = "../outside.pdf"
    elif damage == "schema":
        bundle["annotations"][0]["annotation"]["schema_version"] = 999
    if damage != "digest":
        bundle["bundle_sha256"] = bundle_digest(bundle)
    with pytest.raises(ValueError):
        target.begin(bundle)
    assert target.store.list_documents() == []
    assert list(target.staging.iterdir()) == []


def test_chunk_checks_and_full_file_integrity(transfer_pair):
    source, target, document_id = transfer_pair
    session = target.begin(source.export(document_id))
    tid = session["transfer_id"]
    chunk = source.read_chunk(document_id, 0, 100)
    with pytest.raises(ValueError, match="out of order"):
        target.upload(tid, 1, chunk["data_base64"], chunk["sha256"])
    with pytest.raises(ValueError, match="SHA-256"):
        target.upload(tid, 0, chunk["data_base64"], "0" * 64)
    with pytest.raises(ValueError, match="base64"):
        target.upload(tid, 0, "bad?", "0" * 64)
    with pytest.raises(ValueError, match="size"):
        target.upload(tid, 0, "A" * (2 * CHUNK_BYTES), "0" * 64)
    target.upload(tid, 0, chunk["data_base64"], chunk["sha256"])
    altered = b"%PDF-not original"
    with pytest.raises(StudioConflictError, match="Retry differs"):
        target.upload(tid, 0, base64.b64encode(altered).decode(), hashlib.sha256(altered).hexdigest())
    _upload_all(source, target, document_id, target.status(tid))
    with target._part(tid).open("r+b") as handle:
        handle.seek(10)
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        target.finish(tid, confirm_import=True)
    assert target.store.list_documents() == []
    assert not (target.store.documents_dir / document_id).exists()


def test_cancel_expiry_quota_and_paths(transfer_pair, monkeypatch):
    source, target, document_id = transfer_pair
    bundle = source.export(document_id)
    session = target.begin(bundle)
    _upload_all(source, target, document_id, session)
    with target.store._connect() as connection:
        connection.execute("UPDATE studio_document_transfers SET expires_at=?", (time.time() - 1,))
    assert target.status(session["transfer_id"])["state"] == "EXPIRED"
    with pytest.raises(ValueError, match="expired"):
        target.finish(session["transfer_id"], confirm_import=True)
    new = target.begin(bundle)
    assert new["transfer_id"] != session["transfer_id"]
    assert not target._part(session["transfer_id"]).exists()
    with pytest.raises(ValueError, match="Invalid transfer ID"):
        target.cancel("../outside")
    assert target.cancel(new["transfer_id"])["state"] == "CANCELLED"
    monkeypatch.setattr("jin_runtime.studio_transfer.MAX_ACTIVE_TRANSFERS", 0)
    with pytest.raises(ValueError, match="quota"):
        target.begin(bundle)


def test_publish_rolls_back_on_annotation_insert_failure(transfer_pair, monkeypatch):
    source, target, document_id = transfer_pair
    session = target.begin(source.export(document_id))
    _upload_all(source, target, document_id, session)
    with target.store._connect() as connection:
        connection.execute("""CREATE TRIGGER fail_import BEFORE INSERT ON studio_annotations
                           BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
    with pytest.raises(Exception, match="simulated disk failure"):
        target.finish(session["transfer_id"], confirm_import=True)
    assert target.store.list_documents() == []
    assert not (target.store.documents_dir / document_id).exists()
    assert target.status(session["transfer_id"])["state"] == "ACTIVE"


def test_mcp_end_to_end_transfer_over_authenticated_http(transfer_pair, monkeypatch):
    source, target, document_id = transfer_pair
    monkeypatch.setenv("JIN_STUDIO_MCP_TOKEN", "transfer-test-token")
    monkeypatch.setenv("JIN_STUDIO_MCP_ALLOWED_HOSTS", "testserver,testserver:*")
    headers = {"Authorization": "Bearer transfer-test-token", "Accept": "application/json, text/event-stream"}

    def rpc(client, method, params):
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        return result

    def call(client, name, **arguments):
        return rpc(client, "tools/call", {"name": name, "arguments": arguments})["structuredContent"]

    with TestClient(create_http_app(create_mcp_server(source.store))) as src, TestClient(create_http_app(create_mcp_server(target.store))) as dst:
        for client in (src, dst):
            initialized = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "transfer-test", "version": "1"}})
            headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        bundle = call(src, "export_document_bundle", document_id=document_id)
        session = call(dst, "begin_document_import", document_export=bundle)
        while session["received_bytes"] < session["pdf_size"]:
            chunk = call(src, "read_document_pdf_chunk", document_id=document_id, offset=session["received_bytes"])
            session = call(dst, "upload_document_pdf_chunk", transfer_id=session["transfer_id"], **{k: chunk[k] for k in ("offset", "data_base64", "sha256")})
        assert call(dst, "get_document_import_status", transfer_id=session["transfer_id"])["received_bytes"] == len(PDF)
        assert call(dst, "finish_document_import", transfer_id=session["transfer_id"], confirm_import=True)["state"] == "COMPLETE"
        assert call(dst, "get_document", document_id=document_id)["latestAnnotation"] == source.store.get_document(document_id)["latestAnnotation"]
    assert target.store.pdf_path(document_id).read_bytes() == PDF


def test_document_pagination_is_stable(transfer_pair):
    source, _, document_id = transfer_pair
    second = source.store.create_document(filename="second.pdf", pdf_bytes=PDF)
    all_ids = {document_id, second["documentId"]}
    paged = [source.store.list_documents(limit=1, offset=i)[0]["documentId"] for i in range(2)]
    assert set(paged) == all_ids
    assert source.store.list_documents(limit=1, offset=2) == []


def test_document_without_annotations_and_existing_orphan_directory(transfer_pair):
    source, target, _ = transfer_pair
    document = source.store.create_document(filename="unannotated.pdf", pdf_bytes=PDF)
    document_id = document["documentId"]
    bundle = source.export(document_id)
    assert bundle["annotations"] == []
    session = target.begin(bundle)
    _upload_all(source, target, document_id, session)
    directory = target.store.documents_dir / document_id
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("Do not overwrite existing files")
    with pytest.raises(FileExistsError):
        target.finish(session["transfer_id"], confirm_import=True)
    assert sentinel.read_text() == "Do not overwrite existing files"
    assert target.store.list_documents() == []
    sentinel.unlink()
    directory.rmdir()
    target.finish(session["transfer_id"], confirm_import=True)
    assert target.store.get_document(document_id) == source.store.get_document(document_id)
