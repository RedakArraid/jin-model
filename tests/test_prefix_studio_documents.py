from pathlib import Path
import json
import sqlite3

from scripts.prefix_studio_documents_with_client import (
    apply_rename_plan,
    build_rename_plan,
    normalize_client_prefix,
    prefixed_filename,
    resolve_client_prefix,
)


def test_client_prefix_prefers_curated_campaign_directory():
    prefix, source = resolve_client_prefix(
        {"sourceRelativePath": "REXEL/order.pdf"},
        {"order": {"parties": {"buyer": {"name": "Wrong OCR"}}}},
    )
    assert (prefix, source) == ("REXEL", "sourceRelativePath")
    assert normalize_client_prefix("Rexel France") == "REXEL"


def test_client_prefix_falls_back_to_extracted_buyer_for_unclassified_documents():
    prefix, source = resolve_client_prefix(
        {"sourceRelativePath": "00_IDENTITE_A_VERIFIER/order.pdf"},
        {"order": {"parties": {"buyer": {"legal_name": "IZI by EDF"}}}},
    )
    assert (prefix, source) == (
        "IZI_BY_EDF",
        "extraction.order.parties.buyer",
    )


def test_prefixed_filename_is_idempotent_and_keeps_pdf_extension():
    assert prefixed_filename("order 123.PDF", "REXEL") == "REXEL_order_123.PDF"
    assert prefixed_filename("REXEL_order_123.PDF", "REXEL") == "REXEL_order_123.PDF"
    assert len(prefixed_filename("x" * 300 + ".pdf", "CLIENT")) <= 180
    assert prefixed_filename("x" * 300 + ".pdf", "CLIENT").endswith(".pdf")


def test_database_plan_and_apply_preserve_document_identity(tmp_path: Path):
    database = tmp_path / "studio.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE studio_documents (
                document_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                source_json TEXT NOT NULL,
                extraction_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO studio_documents VALUES (?, ?, ?, ?, ?)",
            (
                "jdoc-1",
                "order.pdf",
                json.dumps({"sourceRelativePath": "REXEL/order.pdf"}),
                "{}",
                "2026-01-01T00:00:00Z",
            ),
        )

    changes, unresolved = build_rename_plan(database)
    assert unresolved == []
    assert changes[0]["newFilename"] == "REXEL_order.pdf"
    apply_rename_plan(database, changes)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT document_id, filename, source_json FROM studio_documents"
        ).fetchone()
    assert row[0] == "jdoc-1"
    assert row[1] == "REXEL_order.pdf"
    source = json.loads(row[2])
    assert source["originalStudioFilename"] == "order.pdf"
    assert source["clientPrefix"] == "REXEL"
