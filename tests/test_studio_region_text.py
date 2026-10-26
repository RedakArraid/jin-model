from __future__ import annotations

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jin_runtime.studio_api import create_studio_router
from jin_runtime.studio_region_text import (
    StudioRegionError,
    extract_pdf_region_text,
    normalize_region_bbox,
)
from jin_runtime.studio_store import StudioStore


def _pdf_with_text() -> bytes:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 90), "BON DE COMMANDE PO-123", fontsize=14)
    payload = document.tobytes()
    document.close()
    return payload


def test_region_text_uses_normalized_coordinates_without_mutating_them(tmp_path):
    path = tmp_path / "commande.pdf"
    path.write_bytes(_pdf_with_text())
    bbox = [0, 0, 700, 250]

    result = extract_pdf_region_text(path, page_number=1, bbox=bbox, allow_ocr=False)

    assert "PO-123" in result["text"]
    assert result["source"] == "native_pdf"
    assert result["bbox"] == [0.0, 0.0, 700.0, 250.0]
    assert bbox == [0, 0, 700, 250]


@pytest.mark.parametrize(
    "bbox",
    ([0, 0, 1001, 100], [10, 10, 10, 20], [1, 2, 3]),
)
def test_region_bbox_rejects_invalid_coordinates(bbox):
    with pytest.raises(StudioRegionError):
        normalize_region_bbox(bbox)


def test_studio_region_endpoint_returns_selected_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_STUDIO_TOKEN", "studio-secret")
    store = StudioStore(tmp_path / "studio")
    document = store.create_document(
        filename="commande.pdf",
        pdf_bytes=_pdf_with_text(),
        extraction={},
    )
    app = FastAPI()
    app.include_router(create_studio_router(store))
    client = TestClient(app)

    response = client.post(
        f"/studio/documents/{document['documentId']}/region-text",
        headers={"Authorization": "Bearer studio-secret"},
        json={"page": 1, "bbox": [0, 0, 700, 250]},
    )

    assert response.status_code == 200
    assert "PO-123" in response.json()["text"]
