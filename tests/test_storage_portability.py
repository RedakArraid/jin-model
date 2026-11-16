from __future__ import annotations

import json
import shutil
from pathlib import Path
import hashlib
import sys
import types

import pytest

from jin_runtime.studio_paths import relative_studio_path, resolve_studio_path
from jin_runtime.studio_store import StudioStore
from jin_runtime.studio_training_worker import validated_command
from jin_runtime.training_paths import dataset_file, materialize_page_paths


def reviewed_annotation():
    return {"status": "HUMAN_VALIDATED", "reviewed_by": "test-reviewer", "field_annotations": [{
        "path": "order.purchase_order.number", "label": "ORDER_NUMBER", "status": "CONFIRMED",
        "extracted_value": "PO-123", "evidence": {"page": 1, "bbox": [100, 100, 250, 130]},
    }]}


@pytest.mark.parametrize("legacy", [False, True])
def test_dataset_and_queued_training_survive_storage_relocation(tmp_path, monkeypatch, legacy):
    monkeypatch.setenv("JIN_STUDIO_MIN_TRAINING_DOCUMENTS", "2")
    original = tmp_path / "old-host" / "studio"
    store = StudioStore(original)
    ids = []
    for i in range(2):
        doc = store.create_document(filename=f"order-{i}.pdf", pdf_bytes=b"%PDF-1.4\n" + str(i).encode(),
                                    extraction={"order": {"purchase_order": {"number": "PO-123"}}})
        store.save_annotation(doc["documentId"], reviewed_annotation())
        ids.append(doc["documentId"])
    dataset = store.create_dataset(name="portable", document_ids=ids)
    run = store.prepare_training_run(dataset_id=dataset["datasetId"])
    with store._connect() as db:
        stored = db.execute("SELECT payload_json FROM studio_training_runs").fetchone()[0]
        assert str(original) not in stored
        assert "training/jrun-" in stored
    manifest = Path(dataset["manifestPath"])
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    assert all(not Path(row["pdf"]).is_absolute() for row in rows)
    if legacy:
        for row in rows:
            row["pdf"] = "C:\\old-host\\studio\\documents\\" + row["document_id"] + "\\source.pdf"
            row["annotation"] = "C:/old-host/studio/datasets/" + dataset["datasetId"] + "/annotations/" + row["document_id"] + ".json"
        manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with store._connect() as db:
            db.execute("UPDATE studio_datasets SET payload_json=?", (json.dumps(dataset),))
            db.execute("UPDATE studio_training_runs SET payload_json=?", (json.dumps(run),))
    moved = tmp_path / "external-disk" / "studio"
    shutil.move(str(original), moved)
    restored = StudioStore(moved)
    exported = restored.list_datasets()[0]
    manifest = Path(exported["manifestPath"])
    assert manifest.is_file() and moved in manifest.parents
    for row in [json.loads(line) for line in manifest.read_text().splitlines()]:
        assert dataset_file(manifest, row["pdf"]).is_file()
        assert dataset_file(manifest, row["annotation"]).is_file()
    queued = restored.claim_training_run("relocated-worker")
    assert queued["runId"] == run["runId"]
    assert queued["outputDirectory"].startswith(str(moved))
    for step in queued["steps"]:
        validated_command(step["command"], store=restored, output_directory=queued["outputDirectory"])
    assert restored.get_document(ids[0])["latestAnnotation"]["status"] == "HUMAN_VALIDATED"
    restored.finish_training_run(run["runId"], success=True, result={
        "artifactDirectory": str(Path(queued["outputDirectory"]) / "artifact"),
        "benchmarkPath": queued["benchmarkPath"],
    })
    with restored._connect() as db:
        stored = db.execute("SELECT payload_json FROM studio_training_runs").fetchone()[0]
        assert str(moved) not in stored
        assert json.loads(stored)["result"]["artifactDirectory"].startswith("training/")
    relocated_again = tmp_path / "another-disk" / "studio"
    shutil.move(str(moved), relocated_again)
    finished = StudioStore(relocated_again).list_training_runs()[0]
    assert finished["result"]["artifactDirectory"].startswith(str(relocated_again))


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "C:/private/secret", "training/../secret", "//server/secrets"])
def test_portable_studio_paths_reject_unmanaged_or_escaping_values(tmp_path, bad):
    with pytest.raises(ValueError):
        resolve_studio_path(tmp_path, bad)


def test_page_images_and_legacy_windows_paths_are_portable(tmp_path):
    manifest = tmp_path / "layout" / "train.jsonl"
    expected = manifest.parent / "images" / "jdoc-1_p001.png"
    for value in ("images/jdoc-1_p001.png", "C:/old/layout/images/jdoc-1_p001.png"):
        assert materialize_page_paths({"image_path": value}, manifest)["image_path"] == str(expected)
    assert relative_studio_path("C:\\old\\studio\\training\\jrun-1\\layout") == "training/jrun-1/layout"


def test_prepared_layout_files_are_relative_and_remain_readable_after_move(tmp_path, monkeypatch):
    from PIL import Image
    # Rendering is outside this unit test and pdf2image belongs to the optional
    # trainer image, not the API test dependencies.
    renderer = types.ModuleType("pdf2image")
    renderer.convert_from_path = lambda *a, **k: [Image.new("RGB", (20, 20))]
    monkeypatch.setitem(sys.modules, "pdf2image", renderer)
    from training.vision import prepare_layout_dataset as prepare
    source = tmp_path / "source"
    source.mkdir()
    pdf = source / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4\ntest\n")
    annotation = source / "annotation.json"
    annotation.write_text(json.dumps({"pages": []}))
    manifest = source / "manifest.jsonl"
    manifest.write_text(json.dumps({"document_id": "doc-1", "split": "train", "pdf": "order.pdf",
                                    "annotation": "annotation.json", "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}) + "\n")
    output = source / "layout"
    monkeypatch.setattr(prepare, "convert_from_path", lambda *a, **k: [Image.new("RGB", (20, 20))])
    monkeypatch.setattr(prepare, "ocr_page", lambda *a: (["test"], [[0, 0, 10, 10]], ["O"]))
    monkeypatch.setattr(sys, "argv", ["prepare", "--manifest", str(manifest), "--output", str(output)])
    assert prepare.main() == 0
    row = json.loads((output / "train.jsonl").read_text())
    assert row["image_path"] == "images/doc-1_p001.png"
    assert row["source_pdf"] == "../order.pdf"
    destination = tmp_path / "relocated"
    shutil.move(str(source), destination)
    assert dataset_file(destination / "layout/train.jsonl", row["image_path"]).is_file()
    assert dataset_file(destination / "layout/train.jsonl", row["source_pdf"]).is_file()
