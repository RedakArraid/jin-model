import base64
import hashlib

import pytest

from scripts.transfer_studio_documents import MCPClient, NoRedirects, document_ids, transfer_document


class Peer:
    def __init__(self, *, completed=False):
        self.calls = []
        self.data = b"%PDF-test"
        self.completed = completed

    def call(self, name, **args):
        self.calls.append((name, args))
        if name == "export_document_bundle":
            return {"pdf_size": len(self.data), "document": {"pdfSha256": hashlib.sha256(self.data).hexdigest()}, "annotations": []}
        if name == "begin_document_import":
            return {"state": "COMPLETE" if self.completed else "ACTIVE", "received_bytes": 0, "transfer_id": "test", "chunk_bytes": 262144}
        if name == "read_document_pdf_chunk":
            return {"data_base64": base64.b64encode(self.data).decode(), "pdf_sha256": hashlib.sha256(self.data).hexdigest(), "sha256": hashlib.sha256(self.data).hexdigest(), "pdf_size": len(self.data), "offset": 0, "next_offset": len(self.data)}
        if name == "upload_document_pdf_chunk":
            return {"received_bytes": len(self.data), "transfer_id": "test"}
        if name == "finish_document_import":
            return {"state": "COMPLETE"}
        if name == "list_documents":
            return {"items": [{"documentId": f"doc-{args['offset']}"}], "has_more": args["offset"] < 2, "next_offset": args["offset"] + 1}
        raise AssertionError(name)


def test_dry_run_never_calls_destination():
    source, target = Peer(), Peer()
    assert transfer_document(source, target, "doc", execute=False, trusted=False)["state"] == "DRY_RUN"
    assert target.calls == []


def test_client_uses_chunks_and_explicit_confirmation():
    source, target = Peer(), Peer()
    assert transfer_document(source, target, "doc", execute=True, trusted=True)["state"] == "COMPLETE"
    assert target.calls[0][1]["confirm_trusted_source"] is True
    assert target.calls[-1] == ("finish_document_import", {"transfer_id": "test", "confirm_import": True})


def test_client_skips_completed_transfer():
    source, target = Peer(), Peer(completed=True)
    assert transfer_document(source, target, "doc", execute=True, trusted=False)["state"] == "ALREADY_TRANSFERRED"
    assert len(target.calls) == 1


def test_client_rejects_corrupt_source_chunk():
    source, target = Peer(), Peer()
    original = source.call

    def corrupt(name, **args):
        result = original(name, **args)
        if name == "read_document_pdf_chunk":
            result["sha256"] = "0" * 64
        return result

    source.call = corrupt
    with pytest.raises(ValueError, match="inconsistent"):
        transfer_document(source, target, "doc", execute=True, trusted=False)
    assert [name for name, args in target.calls] == ["begin_document_import"]


def test_client_lists_all_pages():
    assert document_ids(Peer(), None) == ["doc-0", "doc-1", "doc-2"]
    assert document_ids(Peer(), ["doc-a", "doc-a"]) == ["doc-a"]


@pytest.mark.parametrize("url", ["file:///private", "https://user:secret@example.org/mcp", "https://example.org/mcp?token=secret", "http://vm.example.org/mcp"])
def test_client_rejects_unsafe_urls_before_connecting(url):
    with pytest.raises(ValueError):
        MCPClient(url, "token")


def test_client_never_forwards_token_on_redirect():
    with pytest.raises(ValueError, match="redirect refused"):
        NoRedirects().redirect_request(None, None, 307, "redirect", {}, "https://other.example.org/mcp")
