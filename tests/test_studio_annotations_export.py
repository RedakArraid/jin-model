import base64
from copy import deepcopy
import json

import pytest

from scripts.export_studio_annotations import (
    ExportSource, digest, encode, export_annotations, file_digest, safe_file, verify_export,
)
from scripts import transfer_studio_documents as transfer


class Source:
    def __init__(self, count=2):
        self.data = b"%PDF-1.4\n" + b"x" * 270000
        self.bundles = {}
        self.calls = []
        for index in range(count):
            document_id = f"jdoc-{index:032x}"
            history = []
            for revision, status in [(1, "DRAFT"), (2, "HUMAN_VALIDATED")]:
                annotation = {
                    "document_id": document_id, "document_sha256": digest(self.data),
                    "revision": revision, "status": status, "reviewed_by": "test-reviewer",
                    "annotation_digest": f"test-digest-{revision}",
                    "field_annotations": [{"path": "order.customer_order_number.value", "status": "CORRECTED",
                        "extracted_value": "ancienne", "corrected_value": "CMD-42",
                        "evidence": {"page": 1, "bbox": [1.0, 2.5, 3.0, 4.0]}}],
                }
                history.append({"created_at": f"2026-11-19T00:00:0{revision}Z", "annotation": annotation})
            bundle = {"schema": "jin-studio-document-transfer/v1", "pdf_size": len(self.data),
                      "document": {"documentId": document_id, "pdfSha256": digest(self.data),
                                   "filename": "Commande annotée.pdf", "status": "HUMAN_VALIDATED"},
                      "annotations": history}
            bundle["bundle_sha256"] = digest(encode(bundle))
            self.bundles[document_id] = bundle

    def call(self, name, **args):
        self.calls.append((name, args))
        if name == "list_documents":
            ids = list(self.bundles)[args.get("offset", 0):args.get("offset", 0) + args.get("limit", 100)]
            return {"items": [{"documentId": item} for item in ids], "has_more": False, "next_offset": len(ids)}
        bundle = self.bundles[args["document_id"]]
        if name == "export_document_bundle":
            return deepcopy(bundle)
        if name == "read_document_pdf_chunk":
            offset = args["offset"]
            data = self.data[offset:offset + args["max_bytes"]]
            return {"data_base64": base64.b64encode(data).decode(), "sha256": digest(data),
                    "pdf_sha256": digest(self.data), "pdf_size": len(self.data),
                    "offset": offset, "next_offset": offset + len(data)}
        raise AssertionError(name)


def snapshot(tmp_path):
    source = Source()
    output = tmp_path / "studio_annotations"
    export_annotations(source, output, list(source.bundles))
    return source, output


def test_complete_export_preserves_all_revisions_fields_and_pdf(tmp_path):
    source, output = snapshot(tmp_path)
    rows = verify_export(output)
    assert len(rows) == 2
    assert len(list((output / "annotations").rglob("*.json"))) == 4
    assert json.loads((output / "export_summary.json").read_text())["annotation_revisions"] == 4
    for document_id, row in rows.items():
        assert (output / row["pdf_file"]).read_bytes() == source.data
        original = source.bundles[document_id]
        restored = json.loads((output / row["document_file"]).read_text(encoding="utf-8"))
        assert encode(original) == encode(restored)
        assert row["field_status_counts"] == {"CORRECTED": 1}
        assert row["status"] == "HUMAN_VALIDATED"
        assert row["latest_annotation_file"].endswith("r000002.json")
        for exported, old in zip(row["annotation_revisions"], original["annotations"]):
            payload = json.loads((output / exported["annotation_file"]).read_text())
            assert payload == old["annotation"]
            assert type(payload["field_annotations"][0]["evidence"]["bbox"][0]) is float
    assert all(name in {"export_document_bundle", "read_document_pdf_chunk"} for name, _ in source.calls)


def test_export_is_portable_and_read_only(tmp_path):
    original, output = snapshot(tmp_path)
    moved = tmp_path / "external_disk"
    output.rename(moved)
    source = ExportSource(moved)
    assert transfer.document_ids(source, None) == list(original.bundles)
    document_id = next(iter(original.bundles))
    assert source.call("export_document_bundle", document_id=document_id) == original.bundles[document_id]
    chunk = source.call("read_document_pdf_chunk", document_id=document_id, offset=262144, max_bytes=262144)
    assert base64.b64decode(chunk["data_base64"]) == original.data[262144:]
    assert chunk["eof"]
    with pytest.raises(ValueError, match="read-only"):
        source.call("save_annotation_draft", document_id=document_id)


@pytest.mark.parametrize("kind", ["manifest", "pdf", "bundle", "annotation"])
def test_corruption_blocks_snapshot_loading(tmp_path, kind):
    _, output = snapshot(tmp_path)
    row = next(iter(verify_export(output).values()))
    relative = {"manifest": "manifest.jsonl", "pdf": row["pdf_file"], "bundle": row["document_file"],
                "annotation": row["annotation_revisions"][0]["annotation_file"]}[kind]
    with (output / relative).open("ab") as stream:
        stream.write(b"corrupted")
    with pytest.raises(ValueError):
        ExportSource(output)


def test_partial_export_never_gets_completion_marker(tmp_path):
    source = Source()
    original = source.call

    def broken(name, **args):
        result = original(name, **args)
        if name == "read_document_pdf_chunk" and args["document_id"] == list(source.bundles)[-1]:
            result["sha256"] = "0" * 64
        return result

    source.call = broken
    output = tmp_path / "partial"
    with pytest.raises(ValueError, match="inconsistent"):
        export_annotations(source, output, list(source.bundles))
    assert not (output / "export_summary.json").exists()
    with pytest.raises(ValueError):
        ExportSource(output)


def test_existing_export_never_overwritten(tmp_path):
    source, output = snapshot(tmp_path)
    before = file_digest(output / "manifest.jsonl")
    with pytest.raises(FileExistsError):
        export_annotations(source, output, list(source.bundles))
    assert file_digest(output / "manifest.jsonl") == before


@pytest.mark.parametrize("relative", ["../outside.json", "/outside.json", "C:/outside.json", "..\\outside.json"])
def test_export_rejects_unsafe_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        safe_file(tmp_path, relative)


def test_export_detects_changed_bundle_before_any_destination_write(tmp_path):
    _, output = snapshot(tmp_path)
    source = ExportSource(output)
    document_id = next(iter(source.rows))
    (output / source.rows[document_id]["document_file"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="changed"):
        source.call("export_document_bundle", document_id=document_id)


def test_empty_and_duplicate_selections_refused(tmp_path):
    source = Source()
    document_id = next(iter(source.bundles))
    for ids in ([], [document_id, document_id]):
        with pytest.raises(ValueError, match="unique"):
            export_annotations(source, tmp_path / "export", ids)


def test_unannotated_document_still_exported(tmp_path):
    source = Source(1)
    bundle = next(iter(source.bundles.values()))
    bundle["annotations"] = []
    bundle["document"]["status"] = "DRAFT"
    bundle["bundle_sha256"] = digest(encode({k: v for k, v in bundle.items() if k != "bundle_sha256"}))
    export_annotations(source, tmp_path / "export", list(source.bundles))
    row = next(iter(verify_export(tmp_path / "export").values()))
    assert row["latest_annotation_file"] is None
    assert row["field_count"] == 0


def test_execute_requires_backup_before_connecting(monkeypatch):
    monkeypatch.setattr("sys.argv", ["transfer", "--source-url", "http://localhost/mcp",
                                   "--target-url", "https://target.example/mcp", "--all", "--execute"])
    calls = []
    monkeypatch.setattr(transfer, "MCPClient", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit):
        transfer.main()
    assert calls == []


def test_failed_export_does_not_connect_to_target(tmp_path, monkeypatch):
    source = Source()
    source.bundles[next(iter(source.bundles))]["bundle_sha256"] = "broken"
    calls = []

    def client(url, *args, **kwargs):
        calls.append(url)
        assert url == "http://localhost/mcp"
        return source

    monkeypatch.setattr(transfer, "MCPClient", client)
    monkeypatch.setattr("sys.argv", ["transfer", "--source-url", "http://localhost/mcp",
                                   "--target-url", "https://target.example/mcp", "--all", "--execute",
                                   "--export-dir", str(tmp_path / "failed")])
    assert transfer.main() == 1
    assert calls == ["http://localhost/mcp"]


def test_readback_detects_remote_changes_without_writing():
    source, target = Source(1), Source(1)
    document_id = next(iter(source.bundles))
    transfer.verify_remote_document(source, target, document_id)
    target.bundles[document_id]["annotations"][-1]["annotation"]["reviewed_by"] = "changed"
    with pytest.raises(ValueError, match="differs"):
        transfer.verify_remote_document(source, target, document_id)
    assert all(name == "export_document_bundle" for name, _ in target.calls)


def test_reusing_backup_requires_all_selected_documents(tmp_path):
    source, output = snapshot(tmp_path)
    with pytest.raises(ValueError, match="every selected"):
        transfer.prepare_export_source(source, ["jdoc-" + "f" * 32], output)


def test_snapshot_dry_run_needs_no_token_or_connection(tmp_path, monkeypatch, capsys):
    _, output = snapshot(tmp_path)
    monkeypatch.setattr("sys.argv", ["transfer", "--from-export", str(output),
                                   "--target-url", "https://target.example/mcp", "--all"])
    def forbidden(*args, **kwargs):
        pytest.fail("Dry-run must not connect to any MCP server")
    monkeypatch.setattr(transfer, "MCPClient", forbidden)
    assert transfer.main() == 0
    assert capsys.readouterr().out.count('"state": "DRY_RUN"') == 2
    assert not list(output.glob("transfer-*.jsonl"))


def test_snapshot_execute_verifies_before_target_and_reads_back(tmp_path, monkeypatch):
    source, output = snapshot(tmp_path)
    events = []
    class Target:
        def call(self, name, **args):
            events.append(name)
            if name == "begin_document_import":
                assert args["confirm_trusted_source"] is True
                return {"state": "COMPLETE"}
            return source.call(name, **args)
    def client(*args, **kwargs):
        assert len(verify_export(output)) == 2
        events.append("connect")
        return Target()
    monkeypatch.setattr(transfer, "MCPClient", client)
    monkeypatch.setattr("sys.argv", ["transfer", "--from-export", str(output), "--target-url",
                                   "https://target.example/mcp", "--all", "--execute", "--confirm-trusted-source"])
    assert transfer.main() == 0
    assert events == ["connect", "begin_document_import", "export_document_bundle",
                      "begin_document_import", "export_document_bundle"]
    results = [json.loads(line) for line in next(output.glob("transfer-*.jsonl")).read_text().splitlines()]
    assert len(results) == 2
    assert all(row["state"] == "ALREADY_TRANSFERRED" and row["verified"] for row in results)


def test_corrupt_unselected_document_blocks_whole_snapshot(tmp_path, monkeypatch):
    source, output = snapshot(tmp_path)
    rows = verify_export(output)
    ids = list(source.bundles)
    (output / rows[ids[1]]["pdf_file"]).write_bytes(b"corrupt")
    def forbidden(*args, **kwargs):
        pytest.fail("Destination must not be contacted with an incomplete backup")
    monkeypatch.setattr(transfer, "MCPClient", forbidden)
    monkeypatch.setattr("sys.argv", ["transfer", "--from-export", str(output), "--target-url",
                                   "https://target.example/mcp", "--document-id", ids[0], "--execute"])
    assert transfer.main() == 1
