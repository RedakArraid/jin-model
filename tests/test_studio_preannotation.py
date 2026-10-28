from __future__ import annotations

import json

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jin_runtime.studio_api import create_studio_router
from jin_runtime.studio_preannotation import (
    MACHINE_PREANNOTATION_REVIEWER,
    generate_studio_preannotation,
    machine_draft_payload,
)
from jin_runtime.studio_store import StudioStore


def _order_pdf(path) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "BON DE COMMANDE")
    page.insert_text((72, 110), "Numero de commande PO-12345")
    page.insert_text((72, 148), "Date 28/10/2026")
    document.save(path)
    document.close()
    return path.read_bytes()


def _extraction() -> dict:
    return {
        "document": {"type": "purchase_order"},
        "order": {
            "customer_order_number": {"value": "PO-12345"},
            "order_date": {"value": "2026-10-28", "raw_value": "28/10/2026"},
        },
    }


def test_source_backed_preannotation_emits_normalized_studio_frames(tmp_path):
    pdf_path = tmp_path / "commande.pdf"
    _order_pdf(pdf_path)

    result = generate_studio_preannotation(pdf_path, _extraction())

    assert result["fieldCount"] >= 3
    assert result["framedFieldCount"] >= 3
    assert result["unframedFieldCount"] == (
        result["fieldCount"] - result["framedFieldCount"]
    )
    order_number = next(
        field for field in result["fields"] if field["label"] == "ORDER_NUMBER"
    )
    assert order_number["evidence"]["page"] == 1
    assert all(0 <= value <= 1000 for value in order_number["evidence"]["bbox"])

    payload = machine_draft_payload(result)
    assert payload["status"] == "DRAFT"
    assert payload["reviewed_by"] == MACHINE_PREANNOTATION_REVIEWER


def test_upload_saves_frames_and_reread_returns_unsaved_geometric_proposal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIN_STUDIO_TOKEN", "studio-secret")
    pdf_path = tmp_path / "commande.pdf"
    pdf_bytes = _order_pdf(pdf_path)
    store = StudioStore(tmp_path / "studio")
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)
    headers = {"Authorization": "Bearer studio-secret"}

    response = client.post(
        "/studio/documents",
        headers=headers,
        files={"file": ("commande.pdf", pdf_bytes, "application/pdf")},
        data={
            "extraction_json": json.dumps(_extraction()),
            "source_json": json.dumps({"application": "test"}),
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["preannotation"]["status"] == "READY"
    assert created["preannotation"]["framedFieldCount"] >= 3
    assert created["latestAnnotation"]["reviewed_by"] == MACHINE_PREANNOTATION_REVIEWER
    assert any(
        field.get("evidence", {}).get("bbox")
        for field in created["latestAnnotation"]["field_annotations"]
    )

    document_id = created["documentId"]
    proposal = client.post(
        f"/studio/documents/{document_id}/preannotation",
        headers=headers,
        json={"extraction": _extraction()},
    )
    assert proposal.status_code == 200
    assert proposal.json()["framedFieldCount"] >= 3

    revisions = client.get(
        f"/studio/documents/{document_id}/annotations", headers=headers
    ).json()
    assert revisions["count"] == 1
