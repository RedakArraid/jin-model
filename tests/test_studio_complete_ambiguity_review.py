from copy import deepcopy

import fitz
import pytest

from jin_runtime.studio_store import StudioStore, StudioConflictError
from scripts import studio_complete_ambiguity_review as review


def field(label, value, path=None, **kwargs):
    return dict(label=label, extracted_value=value, status='AMBIGUOUS',
                path=path or 'order.test', **kwargs)


def word(text, x=100, y=100, page=1, confidence=100, group=(0, 0)):
    return dict(text=text, page=page, bbox=[x,y,x+20,y+10], confidence=confidence,
                source='native', group=group)


def test_zero_is_not_lost_and_invalid_numeric_source_is_not_promoted():
    f=field('LINE_ITEM_QUANTITY',0)
    assert review.value(f)=='0'
    before=deepcopy(f)
    assert not review.set_source(f,'not a number',[word('garbage')],'test')
    assert f==before
    assert review.set_source(f,'0,00',[word('0,00')],'test')
    assert f['status']=='CONFIRMED'


def test_exact_words_do_not_cross_pages_or_ocr_lines_or_large_gaps():
    words=[word('124',page=1),word('RUE',x=125,page=2)]
    assert not review.matches(words,'124 RUE')
    assert not review.matches([word('124'),word('RUE',x=125,group=(1,1))],'124 RUE')
    assert not review.matches([word('124'),word('RUE',x=200)],'124 RUE')
    assert not review.matches([word('124',confidence=84)],'124')
    assert review.matches([word('124'),word('RUE',x=125)],'124 RUE')


@pytest.mark.parametrize('label,text,allowed',[
    ('TOTAL_GROSS','Total HT',False),('TOTAL_GROSS','TOTAL TTC',True),
    ('TOTAL_AMOUNT_DUE','TOTAL TTC',False),('TOTAL_AMOUNT_DUE','Net a payer',True),
    ('TOTAL_NET','Total HT',True),('TOTAL_SUBTOTAL','Total HT',False),
    ('TOTAL_SUBTOTAL','Total lignes HT',True),('TOTAL_VAT','Code NAF',False),
])
def test_total_label_controls_semantics(label,text,allowed):
    assert review.total_context_accepts(label,text)==allowed


def test_numeric_candidate_rejects_decimal_shift_and_identifier_substring():
    with fitz.open() as pdf:
        pdf.new_page()
        c=dict(text='1.234',evidence={'page':1,'bbox':[100,100,150,110]})
        assert not review.semantic_context_accepts(pdf,[],field('LINE_ITEM_QUANTITY','1234'),c)
        c['text']='50403505600044'
        assert not review.semantic_context_accepts(pdf,[],field('BUYER_SIREN','504035056'),c)


def test_supplier_role_needs_explicit_heading_not_just_matching_city():
    with fitz.open() as pdf:
        pdf.new_page()
        c=dict(text='DRANCY',evidence={'page':1,'bbox':[100,200,180,210]})
        f=field('SUPPLIER_CITY','DRANCY')
        assert not review.semantic_context_accepts(pdf,[],f,c)
        assert review.semantic_context_accepts(pdf,[word('FOURNISSEUR',x=100,y=150)],f,c)


def test_table_number_heading_recognizes_bare_number_symbol():
    import re
    assert re.fullmatch(review.HEADER_PATTERNS['LINE_ITEM_NUMBER'],'n°')


def test_force_is_explicit_and_does_not_reconfirm_existing_human_value_by_default():
    f=field('ORDER_NUMBER','ABC',corrected_value='DEF')
    f['status']='CORRECTED'
    before=deepcopy(f)
    assert not review.set_source(f,'ABC',[word('ABC')],'test')
    assert f==before


def test_visual_missing_rows_are_added_once_and_clipped_text_stays_ambiguous():
    fields=[]
    events=[]
    review.apply_visual_case('jdoc-06ac692e0660457794c35ceb7c2965d5',fields,events)
    refs=[f for f in fields if f['label']=='LINE_ITEM_REFERENCE']
    assert len(refs)==4
    descriptions=[f for f in fields if f['label']=='LINE_ITEM_DESCRIPTION']
    assert all(f['status']=='AMBIGUOUS' for f in descriptions)
    count=len(fields)
    review.apply_visual_case('jdoc-06ac692e0660457794c35ceb7c2965d5',fields,events)
    assert len(fields)==count


def test_stale_digest_cannot_overwrite_a_newer_annotation(tmp_path):
    store=StudioStore(tmp_path/'studio')
    doc=store.create_document(filename='commande.pdf',pdf_bytes=b'%PDF-1.4\n%%EOF')
    payload={'status':'DRAFT','reviewed_by':'test','field_annotations':[
        field('ORDER_NUMBER','ABC','order.customer_order_number.value')]}
    first=store.save_annotation(doc['documentId'],payload)
    second=store.save_annotation(doc['documentId'],payload,expected_digest=first['annotation_digest'])
    assert second['revision']==first['revision']+1
    # A content change must invalidate the saved review snapshot.
    payload['field_annotations'][0]['corrected_value']='DEF'
    third=store.save_annotation(doc['documentId'],payload)
    with pytest.raises(StudioConflictError):
        store.save_annotation(doc['documentId'],payload,expected_digest=first['annotation_digest'])
    assert store.get_document(doc['documentId'])['latestAnnotation']['revision']==third['revision']


def test_same_content_new_revision_also_invalidates_campaign(tmp_path):
    store=StudioStore(tmp_path/'studio')
    doc=store.create_document(filename='commande.pdf',pdf_bytes=b'%PDF-1.4\n%%EOF')
    payload={'status':'DRAFT','reviewed_by':'test','field_annotations':[
        field('ORDER_NUMBER','ABC','order.customer_order_number.value')]}
    first=store.save_annotation(doc['documentId'],payload)
    store.save_annotation(doc['documentId'],payload)
    with pytest.raises(StudioConflictError):
        store.save_annotation(doc['documentId'],payload,expected_revision=first['revision'])


def test_box_never_combines_pages():
    with pytest.raises(AssertionError):
        review.box([word('a'),word('b',page=2)])


def test_audit_preflight_rejects_changed_or_failed_plans():
    plan={'document_id':'test','field_annotations':[]}
    good={'plan_digest':review.plan_digest(plan),'invalid_geometry':[],
          'increased_training_issues':{},'native_evidence_disagreements':[]}
    review.assert_audited(plan,good)
    with pytest.raises(ValueError):
        review.assert_audited({**plan,'extra':True},good)
    with pytest.raises(ValueError):
        review.assert_audited(plan,{**good,'invalid_geometry':['bad field']})


def test_native_numeric_cells_never_accept_rounding_as_exact_quantity(monkeypatch):
    with fitz.open() as pdf:
        pdf.new_page()
        f=field('LINE_ITEM_QUANTITY','263.101','order.line_items[0].quantity')
        ref=field('LINE_ITEM_REFERENCE','1234567890','order.line_items[0].references.article_number')
        table={'page':1,'top':100,'headers':[]}
        candidate={'text':'263,10','evidence':{'page':1,'bbox':[700,200,750,212]}}
        reference={'text':'1234567890','evidence':{'page':1,'bbox':[80,200,150,212]}}
        monkeypatch.setattr(review,'table_headers',lambda _: [table])
        monkeypatch.setattr(review,'lookup_keys',lambda f:{f['label']})
        monkeypatch.setattr(review,'indexed_occurrences',lambda *args:{'LINE_ITEM_REFERENCE':[reference],'LINE_ITEM_QUANTITY':[candidate]})
        monkeypatch.setattr(review,'table_column',lambda t,e:'LINE_ITEM_REFERENCE' if e['bbox'][0]<200 else 'LINE_ITEM_QUANTITY')
        review.review_native_tables([ref,f],pdf,[])
        assert f['status']=='AMBIGUOUS'


def test_review_audit_uses_displayed_coordinates_on_rotated_pdf():
    from scripts.studio_ambiguity_review_report import complete_words_in_box
    with fitz.open() as pdf:
        page=pdf.new_page(width=500,height=700)
        page.insert_text((100,100),'ABC123')
        original=page.get_text('words')[0]
        page.set_rotation(90)
        displayed=fitz.Rect(original[:4])*page.rotation_matrix
        evidence={'page':1,'bbox':[displayed.x0/page.rect.width*1000,displayed.y0/page.rect.height*1000,
                                  displayed.x1/page.rect.width*1000,displayed.y1/page.rect.height*1000]}
        assert complete_words_in_box(pdf,evidence)=='ABC123'


def test_multi_order_source_is_not_promoted_to_a_single_training_order():
    from scripts.studio_review_native_cases import review_case, MULTI_ORDER
    f=field('ORDER_NUMBER','63198','order.customer_order_number.value')
    f['status']='CONFIRMED'
    events=[]
    review_case(MULTI_ORDER,[f],[],events)
    assert f['status']=='AMBIGUOUS'
    assert 'Scinder' in f['comment']
    assert events[0]['kind']=='multi_order_pdf'
