import fitz

from scripts.studio_annotation_geometry_qa import unsafe_reason, geometry_health


def test_geometry_qa_rejects_non_party_name():
    pdf = fitz.open()
    pdf.new_page()
    field = {"label": "BUYER_NAME", "extracted_value": "TVA", "evidence": {"page": 1, "bbox": [10, 10, 30, 20]}}
    assert "nom de partie" in unsafe_reason(field, [field], pdf, {"unsafe": False})


def test_geometry_qa_accepts_clean_blank_pdf_structure():
    pdf = fitz.open()
    pdf.new_page()
    assert not geometry_health(pdf)["unsafe"]
