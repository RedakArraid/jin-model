"""Long table coverage, dual references and abbreviated financial summaries."""
import fitz
import pytest

from po_ocr.config import load_config
from po_ocr.extract import _is_line_table_stop, abbreviated_net_total
from po_ocr.models import LineItem, PageResult
from po_ocr.pdf_native import extract_native_page
from po_ocr.validate import validate
from uda.business import (
    _coherent_source_subset_rejection,
    _material_anchored_numeric_row_items_v61,
    _strict_text_totals_v48,
    enhance_purchase_order_with_native_structure,
)
from test_order_table_header_cells import order, page


def heading(y=260):
    return (y, [("Article", 10, 37), ("Designation", 126, 190),
                ("Quantite", 289, 337), ("U.C.", 348, 367),
                ("P.", 373, 384), ("unit.", 385, 417), ("U.A.", 425, 444),
                ("PU", 448, 460), ("Net", 464, 490),
                ("Montant", 502, 536), ("HT", 537, 550)])


def product(index, y):
    return [(y, [(f"C{23000 + index}", 10, 38), (str(7733701800 + index), 40, 95),
                 ("2,000", 315, 343), ("PIEC", 349, 368), ("10,00", 390, 418),
                 ("PIEC", 425, 444), ("10,00", 465, 493), ("20,00", 503, 537),
                 ("6", 555, 559)]),
            (y + 9, [(f"UNITE CLIMATE MODELE {index}", 40, 265)])]


@pytest.mark.parametrize("count,start,step", [(21, 350, 19), (35, 310, 13), (2, 90, 670)])
def test_numeric_table_keeps_every_row_without_height_or_lookback_limit(count, start, step):
    rows = [heading(min(260, start - 30))]
    for index in range(count):
        rows.extend(product(index, start + index * step))
    lines = _material_anchored_numeric_row_items_v61([page(rows)], {})
    assert len(lines) == count
    assert [line.material_number for line in lines] == [str(7733701800 + i) for i in range(count)]
    assert [line.customer_material_number for line in lines] == [f"C{23000 + i}" for i in range(count)]
    assert all(line.supplier_material_number == line.material_number for line in lines)
    assert all((line.quantity, line.uom, line.unit_price, line.line_total) == (2, "PCE", 10, 20) for line in lines)


def test_numeric_table_reads_first_item_immediately_below_header():
    lines = _material_anchored_numeric_row_items_v61([page([heading(), *product(0, 276)])], {})
    assert len(lines) == 1


@pytest.mark.parametrize("footer", ["MT H.T. 20,00", "Total HT 20,00", "Mode de reglement", "Adresse de livraison"])
def test_numeric_table_stops_at_summary_and_does_not_reuse_stale_header(footer):
    rows = [heading(), *product(0, 300), (340, [(footer, 10, 270)]), *product(1, 370)]
    lines = _material_anchored_numeric_row_items_v61([page(rows)], {})
    assert [line.material_number for line in lines] == ["7733701800"]


def test_numeric_table_requires_local_header_on_each_page():
    first = page([heading(), *product(0, 740)])
    notes = page(product(1, 300), 2)
    last = page([heading(), *product(2, 300)], 3)
    lines = _material_anchored_numeric_row_items_v61([first, notes, last], {})
    assert [(line.material_number, line.page) for line in lines] == [("7733701800", 1), ("7733701802", 3)]
    assert [line.line_number for line in lines] == ["1", "2"]


def test_numeric_reference_without_header_is_not_a_product():
    assert not _material_anchored_numeric_row_items_v61([page(product(0, 300))], {})


@pytest.mark.parametrize("leading", ["1", "ARTICLE", "31/12/26"])
def test_position_label_and_date_are_not_client_article_codes(leading):
    rows = product(0, 300)
    rows[0][1][0] = (leading, 10, 38)
    lines = _material_anchored_numeric_row_items_v61([page([heading(), *rows])], {})
    assert len(lines) == 1
    assert lines[0].customer_material_number is None


@pytest.mark.parametrize("label", ["MT H.T.", "MNT HT", "MT. H. T."])
def test_abbreviated_total_restores_reconciliation_and_detects_truncated_lines(label):
    po = order()
    po.pages = [page([(790, [("Mode de reglement", 10, 130), (label, 170, 220), ("74 513,08", 230, 285)])])]
    _strict_text_totals_v48(po, po.pages, {})
    assert po.totals.subtotal == po.totals.total_net == po.totals.total_before_tax == 74513.08
    po.lines = [LineItem(material_number="7733701800", description="UNITE CLIMATE",
                         quantity=2, unit_price=10, line_total=20)]
    validate(po, load_config(None).get("validation", {}))
    assert po.validation.checks["printed_total_presence"]["printed_label_present"] is True
    assert po.validation.checks["line_sum"]["valid"] is False
    assert po.validation.status != "PASS"


def test_abbreviated_total_missing_from_output_is_not_silently_validated():
    po = order()
    po.pages = [page([(790, [("MT H.T. 74513,08", 170, 285)])])]
    validate(po, {})
    assert po.validation.checks["printed_total_presence"]["valid"] is False


@pytest.mark.parametrize("text", ["MNT HT", "MT TVA 20,00", "Taux TVA 20,00", "MT HT\n20,00", "MT HT 12345", "FORMAT HT 20,00"])
def test_abbreviated_total_requires_explicit_net_label_and_decimal_amount(text):
    assert abbreviated_net_total(text) is None


def test_abbreviated_total_uses_its_own_amount_not_another_column():
    po = order()
    _strict_text_totals_v48(po, [page([(790, [("MT HT 100,00", 170, 285), ("120,00 EUR", 480, 550)])])], {})
    assert po.totals.total_net == 100
    assert _is_line_table_stop("VIREMENTS MT HT 100,00")
    assert not _is_line_table_stop("Article Designation MNT HT")


def source_lines(count=6):
    return [LineItem(material_number=f"C{23000+i}", description="UNITE CLIMATE",
                     quantity=2, unit_price=10, line_total=20, page=1,
                     bbox=(10, 300 + 20*i, 550, 318 + 20*i)) for i in range(count)]


def test_shape_scores_cannot_prefer_subset_of_same_coherent_source_rows():
    complete = source_lines()
    subset = [line.model_copy(deep=True) for line in complete[:3]]
    for index, line in enumerate(subset):
        line.material_number = str(7733701800 + index)
    rejection = _coherent_source_subset_rejection(subset, [("geometry", complete, [], 0)], {})
    assert rejection["reason"] == "partial_coverage_vs_coherent_source_rows"
    assert rejection["missing_source_rows"] == 3


def test_hypothesis_selection_rejects_numeric_fragment_even_without_printed_total(monkeypatch):
    import uda.business as business

    rows = [heading()]
    for index in range(21):
        rows.extend(product(index, 350 + index * 19))
    source = page(rows)
    full_parser = business._material_anchored_numeric_row_items_v61
    monkeypatch.setattr(business, "_material_anchored_numeric_row_items_v61",
                        lambda *args, **kwargs: full_parser(*args, **kwargs)[:10])
    po = order()
    po.pages = [source]
    enhanced = business.enhance_purchase_order_with_native_structure(po, [], [source], load_config(None))
    diagnostic = enhanced.validation.checks["line_parser_hypothesis"]
    assert len(enhanced.lines) == 21, diagnostic
    assert diagnostic["selected"] != "material_anchored_numeric"
    rejected = diagnostic["suppressed_hypotheses"]["material_anchored_numeric"]
    assert rejected["reason"] == "partial_coverage_vs_coherent_source_rows"
    assert rejected["missing_source_rows"] == 11


@pytest.mark.parametrize("reason", ["reconciled_total", "different_page", "different_values", "bad_extra_rows", "duplicates"])
def test_coverage_guard_does_not_reward_unrelated_or_incoherent_rows(reason):
    complete = source_lines()
    subset = [line.model_copy(deep=True) for line in complete[:3]]
    total_fit = {"valid": reason == "reconciled_total"}
    if reason == "different_page":
        for line in complete:
            line.page = 2
    elif reason == "different_values":
        for line in complete:
            line.quantity, line.line_total = 3, 30
    elif reason == "bad_extra_rows":
        for line in complete[3:]:
            line.line_total = 999
    elif reason == "duplicates":
        complete = complete[:3] * 2
    assert _coherent_source_subset_rejection(subset, [("geometry", complete, [], 0)], total_fit) is None


@pytest.mark.parametrize("delivery_note", ["LIVRAISON DEBUT JANVIER", "LIVRAISON DEBUT MARS"])
def test_generated_two_page_pdf_keeps_21_lines_and_reconciles_last_page_total(delivery_note):
    """Portable end-to-end core regression without committing customer PDFs."""
    document = fitz.open()
    try:
        first_rows = [heading(), (300, [(delivery_note, 40, 260)])]
        for index in range(21):
            first_rows.extend(product(index, 350 + 19 * index))
        last_rows = [heading(), (330, [("POUR TOUTE EXPEDITION, PRENDRE RENDEZ VOUS", 40, 330)]),
                     (350, [("LIVRAISON DE 7H A 12H ET DE 13H A 15H", 40, 330)]),
                     (785, [("MT H.T.", 170, 210), ("420,00", 230, 270)]),
                     (800, [("MT TVA", 170, 210), ("84,00", 230, 270)])]
        pages = []
        for number, rows in enumerate([first_rows, last_rows], 1):
            pdf_page = document.new_page(width=595, height=842)
            for y, cells in rows:
                for text, left, _right in cells:
                    pdf_page.insert_text((left, y), text, fontsize=7)
            text, words, confidence = extract_native_page(pdf_page, number)
            pages.append(PageResult(page=number, width=595, height=842, source_type="native_pdf",
                                    text=text, words=words, confidence=confidence))
        po = order()
        po.pages = pages
        cfg = load_config(None)
        enhanced = enhance_purchase_order_with_native_structure(po, [], pages, cfg)
        validate(enhanced, cfg.get("validation", {}))
        assert len(enhanced.lines) == 21
        assert [line.line_number for line in enhanced.lines] == [str(i) for i in range(1, 22)]
        assert [line.material_number for line in enhanced.lines] == [str(7733701800+i) for i in range(21)]
        assert [line.customer_material_number for line in enhanced.lines] == [f"C{23000+i}" for i in range(21)]
        assert all(line.page == 1 and line.description == f"UNITE CLIMATE MODELE {i}"
                   for i, line in enumerate(enhanced.lines))
        assert sum(line.line_total for line in enhanced.lines) == enhanced.totals.total_net == 420
        assert enhanced.validation.checks["line_sum"]["valid"] is True
        assert enhanced.validation.checks["printed_total_presence"]["valid"] is True
    finally:
        document.close()
