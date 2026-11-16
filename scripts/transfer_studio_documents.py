"""Copy PDFs and reviewed annotation history between two JIN Studio MCP servers.

Dry-run by default. Tokens come only from JIN_SOURCE_MCP_TOKEN and
JIN_TARGET_MCP_TOKEN. No local staging of customer documents or credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import base64
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("MCP redirect refused: use the final /mcp URL (tokens must not be forwarded)")


class MCPClient:
    def __init__(self, url: str, token: str, *, allow_http: bool = False):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use an HTTP(S) MCP URL without credentials, query or fragment")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not allow_http:
            raise ValueError("Remote MCP requires HTTPS; --allow-http is an explicit private-network exception")
        if not token.strip():
            raise ValueError("Set JIN_SOURCE_MCP_TOKEN and JIN_TARGET_MCP_TOKEN in the environment")
        self.url = url
        self.opener = build_opener(NoRedirects())
        self.headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        initialized = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "jin-studio-transfer", "version": "1"}})
        self.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]

    def rpc(self, method: str, params: dict) -> dict:
        request = Request(self.url, headers=self.headers, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode())
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError(f"MCP HTTP {exc.code}; check the URL, token and server configuration") from None
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("MCP response exceeds 32 MiB")
        payload = json.loads(raw)
        if "error" in payload:
            raise RuntimeError(f"MCP protocol error: {payload['error'].get('message', 'unknown')}")
        return payload["result"]

    def call(self, name: str, **arguments) -> dict:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            message = " ".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
            raise RuntimeError(f"{name}: {message[:1000]}")
        return result["structuredContent"]


def document_ids(source, selected: list[str] | None) -> list[str]:
    if selected:
        return list(dict.fromkeys(selected))
    ids, offset = [], 0
    while True:
        page = source.call("list_documents", limit=100, offset=offset)
        ids.extend(item["documentId"] for item in page["items"])
        if not page["has_more"]:
            return list(dict.fromkeys(ids))
        if page["next_offset"] <= offset:
            raise ValueError("MCP pagination did not advance")
        offset = page["next_offset"]


def transfer_document(source, target, document_id: str, *, execute: bool, trusted: bool) -> dict:
    bundle = source.call("export_document_bundle", document_id=document_id)
    summary = {"document_id": document_id, "pdf_bytes": bundle["pdf_size"], "annotation_revisions": len(bundle["annotations"])}
    if not execute:
        return summary | {"state": "DRY_RUN"}
    session = target.call("begin_document_import", document_export=bundle, confirm_trusted_source=trusted)
    if session["state"] == "COMPLETE":
        return summary | {"state": "ALREADY_TRANSFERRED"}
    while session["received_bytes"] < bundle["pdf_size"]:
        offset = session["received_bytes"]
        chunk = source.call("read_document_pdf_chunk", document_id=document_id, offset=offset, max_bytes=session["chunk_bytes"])
        data = base64.b64decode(chunk["data_base64"], validate=True)
        if (not data or chunk["pdf_sha256"] != bundle["document"]["pdfSha256"]
                or chunk["pdf_size"] != bundle["pdf_size"] or chunk["offset"] != offset
                or chunk["next_offset"] != offset + len(data)
                or hashlib.sha256(data).hexdigest() != chunk["sha256"]):
            raise ValueError("Source PDF chunk is inconsistent with its manifest")
        session = target.call("upload_document_pdf_chunk", transfer_id=session["transfer_id"], offset=offset, data_base64=chunk["data_base64"], sha256=chunk["sha256"])
        if session["received_bytes"] != offset + len(data):
            raise ValueError("Destination did not acknowledge the expected PDF bytes")
    completed = target.call("finish_document_import", transfer_id=session["transfer_id"], confirm_import=True)
    return summary | {"state": completed["state"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--target-url", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document-id", action="append")
    parser.add_argument("--execute", action="store_true", help="Actually import; otherwise inspect source only")
    parser.add_argument("--confirm-trusted-source", action="store_true", help="Attest that existing human validations come from a trusted source")
    parser.add_argument("--allow-http", action="store_true", help="Explicitly allow unencrypted remote HTTP on a private network")
    args = parser.parse_args()
    if args.source_url.rstrip("/") == args.target_url.rstrip("/"):
        parser.error("Source and destination must be different Studio instances")
    try:
        source = MCPClient(args.source_url, os.getenv("JIN_SOURCE_MCP_TOKEN", ""), allow_http=args.allow_http)
        target = MCPClient(args.target_url, os.getenv("JIN_TARGET_MCP_TOKEN", ""), allow_http=args.allow_http) if args.execute else None
        ids = document_ids(source, args.document_id)
        failures = 0
        for document_id in ids:
            try:
                result = transfer_document(source, target, document_id, execute=args.execute, trusted=args.confirm_trusted_source)
                print(json.dumps(result, ensure_ascii=True), flush=True)
            except Exception as exc:
                failures += 1
                print(json.dumps({"document_id": document_id, "state": "FAILED", "error": str(exc)}, ensure_ascii=True), flush=True)
        print(json.dumps({"documents": len(ids), "failed": failures, "execute": args.execute}))
        return 1 if failures else 0
    except Exception as exc:
        print(f"Transfer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
