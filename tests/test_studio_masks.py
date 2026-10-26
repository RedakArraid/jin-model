from __future__ import annotations

import json

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jin_runtime.studio_api import create_studio_router
from jin_runtime.studio_masks import StudioMaskError
from jin_runtime.studio_store import StudioStore


def _pdf(text: str, *, pages: int = 1, landscape: bool = False) -> bytes:
    document = fitz.open()
    width, height = ((800, 600) if landscape else (600, 800))
    for index in range(pages):
        page = document.new_page(width=width, height=height)
        if index == 0:
            page.insert_text((60, 80), text, fontsize=12)
    payload = document.tobytes()
    document.close()
    return payload


def _field(value: str = "PO-SOURCE") -> dict:
    return {
        "path": "order.purchase_order.number",
        "label": "ORDER_NUMBER",
        "status": "CORRECTED",
        "extracted_value": "wrong",
        "corrected_value": value,
        "evidence": {"page": 1, "bbox": [80, 55, 700, 125]},
        "comment": "source value must not enter the mask",
    }


def test_annotation_mask_is_value_free_versioned_and_re_reads_target_pdf(tmp_path):
    store = StudioStore(tmp_path / "studio")
    source = store.create_document(
        filename="source.pdf",
        pdf_bytes=_pdf("Commande PO-SOURCE"),
        extraction={"document": {"type": "purchase_order"}},
    )
    target = store.create_document(
        filename="target.pdf",
        pdf_bytes=_pdf("Commande PO-TARGET"),
        extraction={"document": {"type": "purchase_order"}},
    )

    first = store.create_annotation_mask(
        name="Commande récurrente",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[_field()],
        created_by="annotator",
    )
    second = store.create_annotation_mask(
        name="commande récurrente",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[_field("PO-SECRET")],
        created_by="annotator",
    )

    assert first["version"] == 1
    assert second["version"] == 2
    assert first["fieldCount"] == 1
    serialized = json.dumps(first, ensure_ascii=False)
    assert "PO-SOURCE" not in serialized
    assert "wrong" not in serialized
    assert "source value must not enter the mask" not in serialized
    assert first["fields"] == [
        {
            "path": "order.purchase_order.number",
            "label": "ORDER_NUMBER",
            "evidence": {"page": 1, "bbox": [80.0, 55.0, 700.0, 125.0]},
        }
    ]
    assert store.list_annotation_masks()[0]["version"] == 2
    assert len(store.list_annotation_masks(latest_only=False)) == 2
    assert store.status()["annotationMasks"] == 2

    applied = store.apply_annotation_mask(
        document_id=target["documentId"],
        mask_id=first["maskId"],
    )

    assert applied["compatibility"] == "MATCH"
    assert applied["warnings"] == []
    assert applied["appliedFieldCount"] == 1
    assert "PO-TARGET" in applied["fields"][0]["extracted_value"]
    assert applied["fields"][0]["status"] == "UNREVIEWED"
    assert applied["fields"][0]["corrected_value"] is None
    assert target["documentId"] == applied["documentId"]
    assert store.get_document(target["documentId"])["latestAnnotation"] is None


def test_annotation_mask_reports_layout_and_document_type_mismatches(tmp_path):
    store = StudioStore(tmp_path / "studio")
    source = store.create_document(
        filename="source.pdf",
        pdf_bytes=_pdf("Commande PO-SOURCE"),
        extraction={"document": {"type": "purchase_order"}},
    )
    target = store.create_document(
        filename="target.pdf",
        pdf_bytes=_pdf("Facture INV-1", pages=2, landscape=True),
        extraction={"document": {"type": "invoice"}},
    )
    mask = store.create_annotation_mask(
        name="Commande portrait",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[_field()],
        created_by="annotator",
    )

    applied = store.apply_annotation_mask(
        document_id=target["documentId"],
        mask_id=mask["maskId"],
    )

    assert applied["compatibility"] == "REVIEW_REQUIRED"
    assert any("Type différent" in warning for warning in applied["warnings"])
    assert any("Nombre de pages différent" in warning for warning in applied["warnings"])
    assert any("Géométrie différente" in warning for warning in applied["warnings"])


def test_annotation_mask_requires_at_least_one_positioned_field(tmp_path):
    store = StudioStore(tmp_path / "studio")
    source = store.create_document(filename="source.pdf", pdf_bytes=_pdf("PO-SOURCE"))
    field = _field()
    field["evidence"] = None

    with pytest.raises(StudioMaskError, match="Aucun champ ne possède de cadre"):
        store.create_annotation_mask(
            name="Masque vide",
            source_document_id=source["documentId"],
            document_type="purchase_order",
            field_annotations=[field],
            created_by="annotator",
        )


def test_annotation_mask_api_is_authenticated_csrf_protected_and_audited(tmp_path):
    store = StudioStore(tmp_path / "studio")
    store.create_user(
        username="mask.annotator",
        display_name="Mask Annotator",
        password="password123",
        roles=["ANNOTATOR"],
        created_by="pytest",
    )
    source = store.create_document(
        filename="source.pdf",
        pdf_bytes=_pdf("Commande PO-SOURCE"),
        extraction={"document": {"type": "purchase_order"}},
    )
    target = store.create_document(
        filename="target.pdf",
        pdf_bytes=_pdf("Commande PO-TARGET"),
        extraction={"document": {"type": "purchase_order"}},
    )
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)
    user = client.post(
        "/studio/auth/login",
        json={"username": "mask.annotator", "password": "password123"},
    ).json()["user"]

    request = {
        "name": "Masque API",
        "sourceDocumentId": source["documentId"],
        "documentType": "purchase_order",
        "fieldAnnotations": [_field()],
    }
    assert client.post("/studio/annotation-masks", json=request).status_code == 403
    created = client.post(
        "/studio/annotation-masks",
        headers={"X-CSRF-Token": user["csrfToken"]},
        json=request,
    )
    assert created.status_code == 201, created.text
    mask = created.json()

    listed = client.get("/studio/annotation-masks")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["maskId"] == mask["maskId"]
    applied = client.post(
        f"/studio/documents/{target['documentId']}/annotation-masks/{mask['maskId']}/apply",
        headers={"X-CSRF-Token": user["csrfToken"]},
        json={},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["fields"][0]["status"] == "UNREVIEWED"

    actions = {event["action"] for event in store.list_audit_events()}
    assert "annotation_mask.create" in actions
    assert "annotation_mask.apply" in actions
