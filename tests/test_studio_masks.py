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


def _section_shift_pdf(
    header_value: str,
    footer_value: str,
    *,
    header_shift: tuple[float, float] = (0, 0),
    footer_shift: tuple[float, float] = (0, 0),
) -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    header_x, header_y = header_shift
    footer_x, footer_y = footer_shift
    page.insert_text((50 + header_x, 55 + header_y), "BON COMMANDE", fontsize=10)
    page.insert_text((50 + header_x, 75 + header_y), "NUMERO CLIENT", fontsize=10)
    page.insert_text((50 + header_x, 95 + header_y), "REFERENCE FOURNISSEUR", fontsize=10)
    page.insert_text((220 + header_x, 75 + header_y), header_value, fontsize=10)
    page.insert_text((50 + footer_x, 690 + footer_y), "TOTAL GENERAL", fontsize=10)
    page.insert_text((50 + footer_x, 710 + footer_y), "CONDITIONS PAIEMENT", fontsize=10)
    page.insert_text((50 + footer_x, 730 + footer_y), "COORDONNEES BANCAIRES", fontsize=10)
    page.insert_text((220 + footer_x, 710 + footer_y), footer_value, fontsize=10)
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


def _line_field() -> dict:
    return {
        "path": "order.line_items[0].references.article_number",
        "label": "LINE_ITEM_REFERENCE",
        "status": "CONFIRMED",
        "extracted_value": "87168294760",
        "corrected_value": None,
        "evidence": {"page": 1, "bbox": [100, 200, 250, 225]},
        "comment": "",
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
    assert applied["alignment"]["status"] == "UNAVAILABLE"
    assert applied["alignment"]["translatedFieldCount"] == 0
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


def test_annotation_mask_preserves_a_manually_selected_footer_compartment(tmp_path):
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
    footer_field = {**_field(), "compartment": "FOOTER"}

    mask = store.create_annotation_mask(
        name="Champ manuel de pied",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[footer_field],
        created_by="annotator",
    )
    applied = store.apply_annotation_mask(
        document_id=target["documentId"],
        mask_id=mask["maskId"],
    )

    assert mask["fields"][0]["compartment"] == "FOOTER"
    assert applied["fields"][0]["compartment"] == "FOOTER"


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


def test_annotation_mask_excludes_line_items_and_keeps_header_footer(tmp_path):
    store = StudioStore(tmp_path / "studio")
    source = store.create_document(
        filename="source.pdf",
        pdf_bytes=_pdf("Commande PO-SOURCE\n87168294760 ARTICLE"),
        extraction={"document": {"type": "purchase_order"}},
    )

    mask = store.create_annotation_mask(
        name="En-tête et pied",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[_field(), _line_field()],
        created_by="annotator",
    )

    assert mask["schemaVersion"] == 3
    assert mask["scope"] == "HEADER_AND_FOOTER"
    assert mask["alignmentPolicy"] == "SOURCE_TARGET_TEXT_TRANSLATION_V1"
    assert mask["fieldCount"] == 1
    assert mask["omittedLineItemCount"] == 1
    assert all("line_items[" not in field["path"] for field in mask["fields"])


def test_annotation_mask_translates_header_and_footer_independently(tmp_path):
    store = StudioStore(tmp_path / "studio")
    source = store.create_document(
        filename="source.pdf",
        pdf_bytes=_section_shift_pdf("PO-SOURCE", "NET 100 EUR"),
        extraction={"document": {"type": "purchase_order"}},
    )
    target = store.create_document(
        filename="target.pdf",
        pdf_bytes=_section_shift_pdf(
            "PO-TARGET",
            "NET 200 EUR",
            header_shift=(36, 24),
            footer_shift=(-24, -16),
        ),
        extraction={"document": {"type": "purchase_order"}},
    )
    header_field = {
        **_field(),
        "compartment": "HEADER",
        "evidence": {"page": 1, "bbox": [350, 75, 650, 110]},
    }
    footer_field = {
        "path": "order.totals.total_net",
        "label": "TOTAL_NET",
        "compartment": "FOOTER",
        "status": "CONFIRMED",
        "extracted_value": 100,
        "corrected_value": None,
        "evidence": {"page": 1, "bbox": [350, 865, 650, 900]},
        "comment": "",
    }
    mask = store.create_annotation_mask(
        name="Masque avec recalage",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[header_field, footer_field],
        created_by="annotator",
    )

    applied = store.apply_annotation_mask(
        document_id=target["documentId"],
        mask_id=mask["maskId"],
    )

    assert applied["alignment"]["status"] == "TRANSLATED"
    assert applied["alignment"]["translatedFieldCount"] == 2
    alignments = {item["path"]: item for item in applied["alignment"]["fields"]}
    header_alignment = alignments[header_field["path"]]
    footer_alignment = alignments[footer_field["path"]]
    assert header_alignment["anchorCount"] >= 2
    assert header_alignment["dx"] == pytest.approx(60, abs=1)
    assert header_alignment["dy"] == pytest.approx(30, abs=1)
    assert footer_alignment["anchorCount"] >= 2
    assert footer_alignment["dx"] == pytest.approx(-40, abs=1)
    assert footer_alignment["dy"] == pytest.approx(-20, abs=1)
    fields = {item["path"]: item for item in applied["fields"]}
    assert "PO-TARGET" in fields[header_field["path"]]["extracted_value"]
    assert "NET 200 EUR" in fields[footer_field["path"]]["extracted_value"]


def test_applying_legacy_mask_also_excludes_line_items(tmp_path):
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
    mask = store.create_annotation_mask(
        name="Ancien masque",
        source_document_id=source["documentId"],
        document_type="purchase_order",
        field_annotations=[_field()],
        created_by="annotator",
    )
    with store._connect() as connection:
        row = connection.execute(
            "SELECT payload_json FROM studio_annotation_masks WHERE mask_id=?",
            (mask["maskId"],),
        ).fetchone()
        legacy = json.loads(row[0])
        legacy["schemaVersion"] = 1
        legacy["fields"].append(
            {
                "path": _line_field()["path"],
                "label": _line_field()["label"],
                "evidence": _line_field()["evidence"],
            }
        )
        connection.execute(
            "UPDATE studio_annotation_masks SET payload_json=? WHERE mask_id=?",
            (json.dumps(legacy), mask["maskId"]),
        )

    applied = store.apply_annotation_mask(
        document_id=target["documentId"],
        mask_id=mask["maskId"],
    )

    assert applied["appliedFieldCount"] == 1
    assert applied["excludedLineItemCount"] == 1
    assert all("line_items[" not in field["path"] for field in applied["fields"])


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
    with store._connect() as connection:
        connection.execute(
            "UPDATE studio_documents SET pdf_path=? WHERE document_id=?",
            (
                r"C:\foreign-host\jin-model\data\studio\documents\target\source.pdf",
                target["documentId"],
            ),
        )
    assert client.get(f"/studio/documents/{target['documentId']}/pdf").status_code == 200
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
