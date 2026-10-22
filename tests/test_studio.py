from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jin_runtime.studio_contract import StudioContractError, normalize_studio_annotation
from jin_runtime.studio_api import create_studio_router
from jin_runtime.studio_store import StudioStore
from jin_runtime.studio_training_worker import validated_command


PDF = b"%PDF-1.4\n% JIN Studio test\n%%EOF\n"


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
    queued = store.prepare_training_run(dataset_id=dataset["datasetId"])
    assert queued["status"] == "QUEUED"
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
    finished = store.finish_training_run(running["runId"], success=True)
    assert finished["status"] == "SUCCEEDED"
    assert "currentStep" not in finished


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
