from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jin_runtime.studio_contract import (
    FIELD_CATALOG,
    StudioContractError,
    normalize_studio_annotation,
    training_annotation_issues,
)
from jin_runtime.studio_api import create_studio_router
from jin_runtime.studio_security import hash_password, verify_password
from jin_runtime.studio_store import StudioConflictError, StudioStore
from jin_runtime.studio_training_worker import validated_command


PDF = b"%PDF-1.4\n% JIN Studio test\n%%EOF\n"


def test_store_retries_transient_shared_database_open_errors(tmp_path, monkeypatch):
    store = StudioStore(tmp_path / "studio")
    real_connect = sqlite3.connect
    attempts = 0

    def flaky_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("jin_runtime.studio_store.sqlite3.connect", flaky_connect)
    monkeypatch.setattr("jin_runtime.studio_store.time.sleep", lambda _seconds: None)

    with store._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    assert attempts == 3


def test_store_creates_a_shared_schema_initialization_lock(tmp_path):
    store = StudioStore(tmp_path / "studio")

    assert store.schema_lock_path.is_file()
    assert store.status()["annotationMasks"] == 0


def test_document_pdf_paths_are_relative_and_survive_a_foreign_absolute_path(tmp_path):
    store = StudioStore(tmp_path / "studio")
    document = store.create_document(filename="commande.pdf", pdf_bytes=PDF)
    document_id = document["documentId"]

    with store._connect() as connection:
        stored = connection.execute(
            "SELECT pdf_path FROM studio_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()["pdf_path"]
        assert stored == f"documents/{document_id}/source.pdf"
        connection.execute(
            "UPDATE studio_documents SET pdf_path=? WHERE document_id=?",
            (r"C:\foreign-host\studio\documents\legacy\source.pdf", document_id),
        )

    assert store.pdf_path(document_id) == (
        store.documents_dir / document_id / "source.pdf"
    ).resolve()


def test_every_annotatable_field_has_precise_selection_help():
    assert FIELD_CATALOG
    assert all(item.get("help", "").startswith("Encadrez") for item in FIELD_CATALOG)
    assert all(item.get("example", "").startswith("Dans ") for item in FIELD_CATALOG)
    assert all("encadrez" in item["example"].lower() for item in FIELD_CATALOG)
    assert all(item.get("defaultPath", "") for item in FIELD_CATALOG)
    assert len({item["label"] for item in FIELD_CATALOG}) == len(FIELD_CATALOG)
    assert len({item["defaultPath"] for item in FIELD_CATALOG}) == len(FIELD_CATALOG)

    by_label = {item["label"]: item for item in FIELD_CATALOG}
    document_help = by_label["DOCUMENT_TYPE"]["help"]
    assert "BON DE COMMANDE" in document_help
    assert "toute la page" in document_help
    assert "jamais la description complète" in by_label["LINE_ITEM_REFERENCE"]["help"]
    assert "8738717111" in by_label["LINE_ITEM_REFERENCE"]["example"]
    assert "ISAAC NEWTON" in by_label["SHIP_TO_STREET_NAME"]["example"]
    assert "PORT" in by_label["ADDITIONAL_CHARGE_LABEL"]["example"]
    assert "client qui passe la commande" in by_label["BUYER_CODE"]["help"]
    assert by_label["ORDER_DATE"]["defaultPath"] == "order.order_date.value"
    assert by_label["BUYER_STREET_NAME"]["defaultPath"] == (
        "order.addresses.buyer.selected.components.street_name"
    )
    assert by_label["SHIP_TO_HOUSE_NUMBER"]["defaultPath"] == (
        "order.addresses.ship_to.selected.components.house_number"
    )
    assert not any(label.startswith("ADDRESS_") for label in by_label)


def test_training_safety_rejects_unscoped_or_derived_address_ground_truth():
    fields = [
        {
            "path": "order.addresses[0].components.house_number",
            "label": "ADDRESS_HOUSE_NUMBER",
            "status": "CONFIRMED",
            "extracted_value": "12",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [10, 10, 20, 20]},
        },
        {
            "path": "order.addresses[0].verification.matched_components.city",
            "label": "SHIP_TO_CITY",
            "status": "CONFIRMED",
            "extracted_value": "PARIS",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [30, 10, 60, 20]},
        },
    ]

    issues = training_annotation_issues(fields)

    assert {item["code"] for item in issues} == {
        "UNSCOPED_ADDRESS_LABEL",
        "DERIVED_METADATA_PATH",
    }


def test_human_validation_requires_role_specific_source_address_fields():
    payload = {
        "status": "HUMAN_VALIDATED",
        "reviewed_by": "admin",
        "field_annotations": [
            {
                "path": "order.addresses[0].components.house_number",
                "label": "ADDRESS_HOUSE_NUMBER",
                "status": "CONFIRMED",
                "extracted_value": "12",
                "corrected_value": None,
                "evidence": {"page": 1, "bbox": [10, 10, 20, 20]},
            }
        ],
    }

    with pytest.raises(StudioContractError, match="label is not supported"):
        normalize_studio_annotation(
            payload,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_training_safety_rejects_overlapping_composite_and_atomic_street():
    fields = [
        {
            "path": "order.addresses.ship_to.selected.components.street",
            "label": "SHIP_TO_STREET",
            "status": "CORRECTED",
            "extracted_value": "",
            "corrected_value": "1ERE AVENUE",
            "evidence": {"page": 1, "bbox": [100, 100, 250, 130]},
        },
        {
            "path": "order.addresses.ship_to.selected.components.street_type",
            "label": "SHIP_TO_STREET_TYPE",
            "status": "CORRECTED",
            "extracted_value": "",
            "corrected_value": "AVENUE",
            "evidence": {"page": 1, "bbox": [180, 100, 250, 130]},
        },
    ]

    issues = training_annotation_issues(fields)

    assert any(item["code"] == "OVERLAPPING_COMPOSITE_STREET" for item in issues)


def test_training_safety_requires_a_source_region_for_reviewed_values():
    fields = [
        {
            "path": "order.addresses.ship_to.selected.components.country",
            "label": "SHIP_TO_COUNTRY",
            "status": "CONFIRMED",
            "extracted_value": "France",
            "corrected_value": None,
        }
    ]

    issues = training_annotation_issues(fields)

    assert [item["code"] for item in issues] == ["MISSING_SOURCE_REGION"]


def test_training_safety_rejects_one_region_reused_by_two_order_lines():
    fields = [
        {
            "path": "order.line_items[0].references.article_number",
            "label": "LINE_ITEM_REFERENCE",
            "status": "CONFIRMED",
            "extracted_value": "8712345678",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [10, 10, 80, 20]},
        },
        {
            "path": "order.line_items[1].references.article_number",
            "label": "LINE_ITEM_REFERENCE",
            "status": "CONFIRMED",
            "extracted_value": "8712345678",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [10, 10, 80, 20]},
        },
    ]

    issues = training_annotation_issues(fields)

    assert any(item["code"] == "DUPLICATE_SOURCE_REGION" for item in issues)


def test_training_safety_recognizes_a_derived_path_at_the_root():
    fields = [
        {
            "path": "quality.output_audit.issues[0].details.city",
            "label": "SHIP_TO_CITY",
            "status": "CONFIRMED",
            "extracted_value": "PARIS",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [10, 10, 80, 20]},
        }
    ]

    issues = training_annotation_issues(fields)

    assert any(item["code"] == "DERIVED_METADATA_PATH" for item in issues)


def test_training_safety_rejects_a_party_name_that_absorbs_its_street():
    fields = [
        {
            "path": "order.parties.ship_to.name",
            "label": "SHIP_TO_NAME",
            "status": "CORRECTED",
            "extracted_value": "",
            "corrected_value": "REXEL-CENTRE LOGISTIQUE 1ERE AVENUE",
            "evidence": {"page": 1, "bbox": [10, 10, 200, 30]},
        },
        {
            "path": "order.addresses.ship_to.selected.components.street",
            "label": "SHIP_TO_STREET",
            "status": "CORRECTED",
            "extracted_value": "",
            "corrected_value": "1ERE AVENUE",
            "evidence": {"page": 1, "bbox": [120, 25, 200, 40]},
        },
    ]

    issues = training_annotation_issues(fields)

    assert any(item["code"] == "PARTY_NAME_CONTAINS_ADDRESS" for item in issues)


def annotation(status: str = "DRAFT", reviewer: str = "") -> dict:
    return {
        "status": status,
        "reviewed_by": reviewer,
        "field_annotations": [
            {
                "path": "order.purchase_order.number",
                "label": "ORDER_NUMBER",
                "status": "CONFIRMED" if status == "HUMAN_VALIDATED" else "UNREVIEWED",
                "extracted_value": "PO-123",
                "corrected_value": None,
                "evidence": {"page": 1, "bbox": [100, 100, 250, 130]},
            }
        ],
    }


def test_validated_annotation_requires_every_field_to_be_reviewed():
    payload = annotation("HUMAN_VALIDATED", "admin")
    payload["field_annotations"][0]["status"] = "UNREVIEWED"

    with pytest.raises(StudioContractError, match="all fields must be reviewed"):
        normalize_studio_annotation(
            payload,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_confirmed_annotation_cannot_turn_an_empty_prediction_into_ground_truth():
    payload = annotation("HUMAN_VALIDATED", "admin")
    payload["field_annotations"][0]["extracted_value"] = ""

    with pytest.raises(StudioContractError, match="extracted_value is required"):
        normalize_studio_annotation(
            payload,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_annotation_rejects_duplicate_semantic_fields():
    payload = annotation()
    payload["field_annotations"].append(
        {
            "path": "order.customer_order_number.alternatives[1].value",
            "label": "ORDER_NUMBER",
            "status": "UNREVIEWED",
            "extracted_value": "PO-456",
            "corrected_value": None,
        }
    )

    with pytest.raises(StudioContractError, match="duplicates label ORDER_NUMBER"):
        normalize_studio_annotation(
            payload,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_annotation_allows_same_line_field_on_different_lines_only():
    first = {
        "path": "order.line_items[0].quantity",
        "label": "LINE_ITEM_QUANTITY",
        "status": "UNREVIEWED",
        "extracted_value": 1,
        "corrected_value": None,
    }
    second = {
        **first,
        "path": "order.line_items[1].quantity",
        "extracted_value": 2,
    }
    normalized = normalize_studio_annotation(
        {"status": "DRAFT", "field_annotations": [first, second]},
        document_id="jdoc-12345678",
        document_sha256="a" * 64,
        revision=1,
    )
    assert len(normalized["field_annotations"]) == 2

    duplicate = {
        **first,
        "path": "order.line_items[0].ordered_quantity",
    }
    with pytest.raises(
        StudioContractError,
        match=r"duplicates label LINE_ITEM_QUANTITY.*line_items\[0\]",
    ):
        normalize_studio_annotation(
            {"status": "DRAFT", "field_annotations": [first, duplicate]},
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_annotation_preserves_explicit_header_footer_compartments():
    payload = annotation()
    payload["field_annotations"][0]["compartment"] = "footer"

    normalized = normalize_studio_annotation(
        payload,
        document_id="jdoc-12345678",
        document_sha256="a" * 64,
        revision=1,
    )

    assert normalized["field_annotations"][0]["compartment"] == "FOOTER"


def test_annotation_rejects_line_compartment_for_regular_field():
    payload = annotation()
    payload["field_annotations"][0]["compartment"] = "LINE_ITEMS"

    with pytest.raises(StudioContractError, match="requires a line-item field"):
        normalize_studio_annotation(
            payload,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_annotation_forces_line_item_fields_into_line_compartment():
    field = {
        "path": "order.line_items[0].quantity",
        "label": "LINE_ITEM_QUANTITY",
        "compartment": "HEADER",
        "status": "UNREVIEWED",
        "extracted_value": 2,
        "corrected_value": None,
    }

    normalized = normalize_studio_annotation(
        {"status": "DRAFT", "field_annotations": [field]},
        document_id="jdoc-12345678",
        document_sha256="a" * 64,
        revision=1,
    )

    assert normalized["field_annotations"][0]["compartment"] == "LINE_ITEMS"


def test_line_item_annotations_require_visible_contiguous_indexes():
    payload = {
        "status": "DRAFT",
        "field_annotations": [
            {
                "path": "order.line_items[0].references.article_number",
                "label": "LINE_ITEM_REFERENCE",
                "status": "UNREVIEWED",
                "extracted_value": "7736507169",
                "corrected_value": None,
            },
            {
                "path": "order.line_items[1].quantity",
                "label": "LINE_ITEM_QUANTITY",
                "status": "UNREVIEWED",
                "extracted_value": 2,
                "corrected_value": None,
            },
        ],
    }

    normalized = normalize_studio_annotation(
        payload,
        document_id="jdoc-12345678",
        document_sha256="a" * 64,
        revision=1,
    )

    assert [field["path"] for field in normalized["field_annotations"]] == [
        "order.line_items[0].references.article_number",
        "order.line_items[1].quantity",
    ]


def test_annotation_normalization_always_places_line_items_last():
    payload = {
        "status": "DRAFT",
        "field_annotations": [
            {
                "path": "order.line_items[1].description",
                "label": "LINE_ITEM_DESCRIPTION",
                "status": "UNREVIEWED",
                "extracted_value": "BLOC GAZ",
                "corrected_value": None,
            },
            {
                "path": "order.customer_order_number.value",
                "label": "ORDER_NUMBER",
                "status": "UNREVIEWED",
                "extracted_value": "PO-42",
                "corrected_value": None,
            },
            {
                "path": "order.line_items[0].quantity",
                "label": "LINE_ITEM_QUANTITY",
                "status": "UNREVIEWED",
                "extracted_value": 2,
                "corrected_value": None,
            },
            {
                "path": "order.requested_delivery_date.value",
                "label": "DELIVERY_DATE",
                "status": "UNREVIEWED",
                "extracted_value": "2026-10-26",
                "corrected_value": None,
            },
        ],
    }

    normalized = normalize_studio_annotation(
        payload,
        document_id="jdoc-12345678",
        document_sha256="a" * 64,
        revision=1,
    )

    assert [field["label"] for field in normalized["field_annotations"]] == [
        "ORDER_NUMBER",
        "DELIVERY_DATE",
        "LINE_ITEM_QUANTITY",
        "LINE_ITEM_DESCRIPTION",
    ]


def test_line_item_annotations_reject_missing_or_unindexed_lines():
    missing_first_line = {
        "status": "DRAFT",
        "field_annotations": [
            {
                "path": "order.line_items[1].quantity",
                "label": "LINE_ITEM_QUANTITY",
                "status": "UNREVIEWED",
                "extracted_value": 2,
                "corrected_value": None,
            }
        ],
    }
    with pytest.raises(StudioContractError, match="contiguous from zero"):
        normalize_studio_annotation(
            missing_first_line,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )

    unindexed = {
        "status": "DRAFT",
        "field_annotations": [
            {
                "path": "manual.quantity",
                "label": "LINE_ITEM_QUANTITY",
                "status": "UNREVIEWED",
                "extracted_value": 2,
                "corrected_value": None,
            }
        ],
    }
    with pytest.raises(StudioContractError, match=r"line_items\[index\]"):
        normalize_studio_annotation(
            unindexed,
            document_id="jdoc-12345678",
            document_sha256="a" * 64,
            revision=1,
        )


def test_store_keeps_immutable_revisions_and_exports_reviewed_dataset(tmp_path):
    store = StudioStore(tmp_path / "studio")
    document = store.create_document(
        filename="commande.pdf",
        pdf_bytes=PDF,
        extraction={"order": {"purchase_order": {"number": "PO-123"}}},
        source={"application": "test"},
    )
    first = store.save_annotation(document["documentId"], annotation())
    second = store.save_annotation(
        document["documentId"], annotation("HUMAN_VALIDATED", "admin")
    )

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["annotation_digest"].startswith("sha256:")
    history = store.list_annotations(document["documentId"])
    assert [item["revision"] for item in history] == [2, 1]

    dataset = store.create_dataset(
        name="commandes-revues", document_ids=[document["documentId"]]
    )
    assert dataset["status"] == "DRAFT"
    assert dataset["regionCount"] == 1
    manifest = list(
        map(json.loads, open(dataset["manifestPath"], encoding="utf-8"))
    )
    assert manifest[0]["pdf_sha256"] == document["pdfSha256"]


def test_studio_api_is_authenticated_and_supports_annotation_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_TOKEN", "studio-secret")
    app = FastAPI()
    app.include_router(create_studio_router(StudioStore(tmp_path / "studio")))
    client = TestClient(app)

    config = client.get("/studio/auth/config")
    assert config.status_code == 200
    assert config.json()["serviceAccessConfigured"] is True
    assert config.json()["mcpEndpoint"] == "/mcp"
    assert client.get("/studio/status").status_code == 401
    headers = {"Authorization": "Bearer studio-secret"}
    created = client.post(
        "/studio/documents",
        headers=headers,
        files={"file": ("commande.pdf", PDF, "application/pdf")},
        data={
            "extraction_json": json.dumps(
                {"order": {"purchase_order": {"number": "PO-123"}}}
            ),
            "source_json": json.dumps({"application": "test"}),
        },
    )
    assert created.status_code == 201
    document_id = created.json()["documentId"]

    saved = client.put(
        f"/studio/documents/{document_id}/annotation",
        headers=headers,
        json=annotation("HUMAN_VALIDATED", "admin"),
    )
    assert saved.status_code == 201
    assert saved.json()["status"] == "HUMAN_VALIDATED"

    listed = client.get("/studio/documents", headers=headers).json()
    assert listed["count"] == 1
    assert "extraction" not in listed["items"][0]
    assert listed["items"][0]["status"] == "HUMAN_VALIDATED"

    pdf = client.get(f"/studio/documents/{document_id}/pdf", headers=headers)
    assert pdf.status_code == 200
    assert pdf.content == PDF

    invalid_dataset = client.post(
        "/studio/datasets", headers=headers, json=[document_id]
    )
    assert invalid_dataset.status_code == 400


def test_studio_api_accepts_the_generated_file_backed_machine_token(
    tmp_path, monkeypatch
):
    for name in (
        "JIN_STUDIO_SERVICE_TOKEN",
        "JIN_STUDIO_TOKEN",
        "JIN_FEEDBACK_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "shared" / "service-token"
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN_FILE", str(token_file))
    app = FastAPI()
    app.include_router(create_studio_router(StudioStore(tmp_path / "studio")))
    client = TestClient(app)

    assert client.get("/studio/status").status_code == 401
    token = token_file.read_text(encoding="utf-8").strip()
    response = client.get(
        "/studio/status", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_passwords_are_scrypt_hashed_and_never_stored_in_plain_text():
    encoded = hash_password("admin123")

    assert encoded.startswith("scrypt$")
    assert "admin123" not in encoded
    assert verify_password("admin123", encoded)
    assert not verify_password("wrong-password", encoded)


def test_human_login_uses_secure_session_roles_csrf_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_PASSWORD_MIN_LENGTH", "8")
    store = StudioStore(tmp_path / "studio")
    admin = store.create_user(
        username="admin.local",
        display_name="Administrateur local",
        password="admin123",
        roles=["ADMIN"],
        created_by="pytest",
    )
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)

    login = client.post(
        "/studio/auth/login",
        json={"username": "admin.local", "password": "admin123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["userId"] == admin["userId"]
    assert login.json()["user"]["permissions"] == ["*"]
    csrf = login.json()["user"]["csrfToken"]
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie

    assert client.get("/studio/auth/me").status_code == 200
    blocked = client.post(
        "/studio/users",
        json={
            "username": "annotation.user",
            "displayName": "Annotation User",
            "password": "password123",
            "roles": ["ANNOTATOR"],
        },
    )
    assert blocked.status_code == 403

    created = client.post(
        "/studio/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": "annotation.user",
            "displayName": "Annotation User",
            "password": "password123",
            "roles": ["ANNOTATOR"],
        },
    )
    assert created.status_code == 201
    assert created.json()["roles"] == ["ANNOTATOR"]
    audit = client.get("/studio/audit-events").json()["items"]
    assert {event["action"] for event in audit} >= {"auth.login", "user.create"}

    logout = client.post("/studio/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200
    assert client.get("/studio/auth/me").status_code == 401


def test_admin_can_rotate_file_backed_service_token_once_from_ui_api(
    tmp_path, monkeypatch
):
    for name in (
        "JIN_STUDIO_SERVICE_TOKEN",
        "JIN_STUDIO_TOKEN",
        "JIN_FEEDBACK_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    token_path = tmp_path / "service-token"
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN_FILE", str(token_path))
    store = StudioStore(tmp_path / "studio")
    store.create_user(
        username="token.admin",
        display_name="Token Admin",
        password="admin123",
        roles=["ADMIN"],
        created_by="pytest",
    )
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)
    login = client.post(
        "/studio/auth/login",
        json={"username": "token.admin", "password": "admin123"},
    ).json()["user"]

    status = client.get("/studio/admin/service-token")
    assert status.status_code == 200
    assert status.json() == {
        "configured": True,
        "source": "file",
        "rotatable": True,
        "mcpShared": True,
    }
    assert "token" not in status.json()
    previous_token = token_path.read_text(encoding="utf-8").strip()
    assert client.post(
        "/studio/admin/service-token/rotate", json={"confirm": True}
    ).status_code == 403
    refused = client.post(
        "/studio/admin/service-token/rotate",
        headers={"X-CSRF-Token": login["csrfToken"]},
        json={"confirm": False},
    )
    assert refused.status_code == 422

    rotated = client.post(
        "/studio/admin/service-token/rotate",
        headers={"X-CSRF-Token": login["csrfToken"]},
        json={"confirm": True},
    )
    assert rotated.status_code == 200
    response_payload = rotated.json()
    token = response_payload.pop("token")
    assert len(token) >= 32
    assert token != previous_token
    assert token_path.read_text(encoding="utf-8").strip() == token
    assert response_payload == {
        "configured": True,
        "source": "file",
        "rotatable": True,
        "mcpShared": True,
    }
    service_client = TestClient(app)
    assert service_client.get(
        "/studio/status", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200
    assert service_client.get(
        "/studio/status", headers={"Authorization": f"Bearer {previous_token}"}
    ).status_code == 401
    actions = {
        item["action"] for item in client.get("/studio/audit-events").json()["items"]
    }
    assert "service_token.rotate" in actions


def test_annotation_identity_and_validator_permission_come_from_session(tmp_path):
    store = StudioStore(tmp_path / "studio")
    annotator = store.create_user(
        username="annotator",
        display_name="Annotateur",
        password="password123",
        roles=["ANNOTATOR"],
        created_by="pytest",
    )
    validator = store.create_user(
        username="validator",
        display_name="Validateur",
        password="password123",
        roles=["VALIDATOR"],
        created_by="pytest",
    )
    document = store.create_document(filename="commande.pdf", pdf_bytes=PDF)
    app = FastAPI()
    app.include_router(create_studio_router(store))

    annotation_client = TestClient(app)
    login = annotation_client.post(
        "/studio/auth/login", json={"username": annotator["username"], "password": "password123"}
    ).json()["user"]
    draft_payload = annotation("DRAFT", "spoofed-user")
    draft = annotation_client.put(
        f"/studio/documents/{document['documentId']}/annotation",
        headers={"X-CSRF-Token": login["csrfToken"]},
        json=draft_payload,
    )
    assert draft.status_code == 201
    assert draft.json()["reviewed_by"] == "annotator"
    forbidden = annotation_client.put(
        f"/studio/documents/{document['documentId']}/annotation",
        headers={"X-CSRF-Token": login["csrfToken"]},
        json=annotation("HUMAN_VALIDATED", "spoofed-user"),
    )
    assert forbidden.status_code == 403

    validation_client = TestClient(app)
    validator_login = validation_client.post(
        "/studio/auth/login", json={"username": validator["username"], "password": "password123"}
    ).json()["user"]
    validated = validation_client.put(
        f"/studio/documents/{document['documentId']}/annotation",
        headers={"X-CSRF-Token": validator_login["csrfToken"]},
        json=annotation("HUMAN_VALIDATED", "spoofed-user"),
    )
    assert validated.status_code == 201
    assert validated.json()["reviewed_by"] == "validator"


def test_last_active_admin_cannot_be_disabled(tmp_path):
    store = StudioStore(tmp_path / "studio")
    admin = store.create_user(
        username="only.admin",
        display_name="Only Admin",
        password="password123",
        roles=["ADMIN"],
        created_by="pytest",
    )

    with pytest.raises(StudioConflictError, match="dernier administrateur"):
        store.update_user(admin["userId"], active=False)


def test_user_can_change_own_password_and_all_sessions_are_revoked(tmp_path):
    store = StudioStore(tmp_path / "studio")
    store.create_user(
        username="viewer.user",
        display_name="Viewer User",
        password="password123",
        roles=["VIEWER"],
        created_by="pytest",
    )
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)
    user = client.post(
        "/studio/auth/login",
        json={"username": "viewer.user", "password": "password123"},
    ).json()["user"]

    changed = client.post(
        "/studio/auth/change-password",
        headers={"X-CSRF-Token": user["csrfToken"]},
        json={"currentPassword": "password123", "newPassword": "new-password-456"},
    )
    assert changed.status_code == 200
    assert changed.json()["reauthenticationRequired"] is True
    assert client.get("/studio/auth/me").status_code == 401
    assert client.post(
        "/studio/auth/login",
        json={"username": "viewer.user", "password": "new-password-456"},
    ).status_code == 200


def test_training_run_has_a_persistent_queue_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_MIN_TRAINING_DOCUMENTS", "2")
    store = StudioStore(tmp_path / "studio")
    document_ids = []
    for index in range(2):
        document = store.create_document(
            filename=f"commande-{index}.pdf",
            pdf_bytes=PDF + str(index).encode(),
            extraction={"order": {"purchase_order": {"number": f"PO-{index}"}}},
        )
        store.save_annotation(
            document["documentId"], annotation("HUMAN_VALIDATED", "admin")
        )
        document_ids.append(document["documentId"])

    dataset = store.create_dataset(name="training-ready", document_ids=document_ids)
    assert dataset["status"] == "READY"
    assert dataset["splitCounts"] == {"train": 1, "validation": 1, "test": 0}
    manifest_rows = [
        json.loads(line)
        for line in Path(dataset["manifestPath"]).read_text(encoding="utf-8").splitlines()
    ]
    assert {row["split"] for row in manifest_rows} == {"train", "validation"}
    exported = json.loads(Path(manifest_rows[0]["annotation"]).read_text(encoding="utf-8"))
    region = exported["pages"][0]["regions"][0]
    assert region["baseline_present"] is True
    assert str(region["baseline_value"]).startswith("PO-")
    queued = store.prepare_training_run(dataset_id=dataset["datasetId"])
    assert queued["status"] == "QUEUED"
    assert [step["name"] for step in queued["steps"]] == [
        "prepare_layout_dataset",
        "fine_tune_layoutlmv3",
        "benchmark_holdout",
    ]
    for step in queued["steps"]:
        command = validated_command(
            step["command"],
            store=store,
            output_directory=queued["outputDirectory"],
        )
        assert Path(command[1]).name == Path(step["command"][1]).name

    running = store.claim_training_run("pytest-worker")
    assert running is not None
    assert running["runId"] == queued["runId"]
    assert running["status"] == "RUNNING"
    heartbeat = store.heartbeat_training_run(
        running["runId"],
        current_step="prepare_layout_dataset",
        completed_steps=[],
    )
    assert heartbeat["currentStep"] == "prepare_layout_dataset"
    finished = store.finish_training_run(
        running["runId"],
        success=True,
        result={"benchmark": {"decision": {"status": "REVIEW_REQUIRED"}}},
    )
    assert finished["status"] == "SUCCEEDED"
    assert "currentStep" not in finished
    assert finished["benchmark"]["decision"]["status"] == "REVIEW_REQUIRED"


def test_training_worker_rejects_commands_outside_its_allowlist(tmp_path):
    store = StudioStore(tmp_path / "studio")
    output = store.root / "training" / "jrun-test"

    with pytest.raises(ValueError, match="not allowed"):
        validated_command(
            ["python", "training/evil.py"],
            store=store,
            output_directory=str(output),
        )

    with pytest.raises(ValueError, match="outside the run output"):
        validated_command(
            [
                "python",
                "training/vision/train_layoutlmv3.py",
                "--data",
                str(output / "layout"),
                f"--output={tmp_path / 'escape'}",
            ],
            store=store,
            output_directory=str(output),
        )
