from datetime import datetime, timezone

from uda.business import (
    _normalize_inline_po_number,
    _enhance_inline_transaction_header_v46,
    _extract_geometry_table_items_v46,
    _sanitize_summary_totals_v46,
    _stacked_column_native_table_items_v46,
)
from uda.models import UniversalTable, SourceRef
from po_ocr.models import PageResult, WordToken, PurchaseOrderResult, DocumentMeta
from po_ocr.config import load_config


def _meta():
    return DocumentMeta(
        id='x', filename='x.pdf', extension='pdf', file_size=1, sha256='x', page_count=1,
        is_pdf=True, is_native_pdf=True, has_text_layer=True,
        processing_timestamp=datetime.now(timezone.utc).isoformat(), model_version='4.6.0'
    )


def _page(rows, width=600.0, height=842.0):
    words=[]
    texts=[]
    for y, items in rows:
        texts.append(' '.join(t for t,_,_ in items))
        for t,x0,x1 in items:
            words.append(WordToken(text=t,page=1,bbox=(x0,y,x1,y+10),confidence=1.0,source='native_pdf'))
    return PageResult(page=1,width=width,height=height,source_type='native_pdf',text='\n'.join(texts),words=words,confidence=1.0)


def test_inline_po_number_grammar_supports_compact_and_spaced_ids():
    cfg=load_config(None)
    assert _normalize_inline_po_number('FBC27834 au 17/08/26',cfg)[0]=='FBC27834'
    assert _normalize_inline_po_number('193 444 / Nice',cfg)==('193444','Nice')
    assert _normalize_inline_po_number('ST C CSP 33443',cfg)[0]=='ST C CSP 33443'
    assert _normalize_inline_po_number('72 475 Adresse de livraison',cfg)==('72475',None)


def test_inline_header_extracts_number_date_supplier_code_and_buyer_contact():
    cfg=load_config(None)
    pg=_page([
        (160,[('COMMANDE',300,370),('FOURNISSEUR',375,460),(':',465,470),('193',475,495),('444',500,520),('/',523,527),('Nice',530,555)]),
        (300,[('Code',20,45),('fournisseur',48,100),(':',102,105),('20',108,118),('501',120,138),('Acheteur',190,230),(':',232,235),('THAON',238,275),('Gabriel',278,315),('-',318,322),('Tél',325,340),('04',345,355),('92',358,368),('15',371,381),('78',384,394),('15',397,407)]),
        (320,[('Date',20,45),('commande',48,95),(':',98,101),('06/08/2026',104,155)]),
    ])
    po=PurchaseOrderResult(document=_meta())
    po=_enhance_inline_transaction_header_v46(po,[pg],cfg)
    assert po.purchase_order.number.value=='193444'
    assert po.purchase_order.project_name.value=='Nice'
    assert po.purchase_order.order_date.value=='2026-08-06'
    assert po.supplier.code=='20501'
    assert po.buyer.contact.name=='THAON Gabriel'


def test_geometry_table_recovers_products_and_ecopart_with_correct_columns():
    cfg=load_config(None)
    pg=_page([
        (440,[('Référence',25,70),('Adressage',115,160),('Désignation',170,225),('Quantité',385,425),('PU',435,447),('net',450,463),('HT',465,477),('Montant',502,536),('HT',539,551)]),
        (488,[('7733701564',22,72),('Z43-08',116,145),('UNITE',169,195),('INTERIEURE',198,251),('MURALE',253,290),('BOSCH',293,324),('CLIMATE',326,365),('35,00',400,422),('104,00',474,501),('3',519,524),('640,00',526,553)]),
        (498,[('CL3000iUW26E',169,233),('R32',235,252),('2.6KW',254,281),('-',284,287),('BLANC',289,319)]),
        (521,[('ECOPART_1.75',22,87),('ECOPARTICIPATION',169,256),('1.75€',258,281),('35,00',400,422),('1,75',484,501),('61,25',531,553)]),
        (546,[('7733701997',22,72),('Z43-15',116,145),('UNITE',169,195),('INTERIEURE',198,251),('MURALE',253,290),('BOSCH',293,324),('CLIMATE',326,365),('35,00',400,422),('93,60',479,501),('3',519,524),('276,00',526,553)]),
        (556,[('CL3000iUW20E',169,233),('R32',235,252),('2.0KW',254,281),('-',284,287),('BLANC',289,319)]),
        (724,[('TOTAL',20,50),('HT',52,65),('10022,45',500,553)]),
    ])
    lines,charges=_extract_geometry_table_items_v46([pg],cfg,'EUR')
    assert len(lines)==2
    assert len(charges)==1
    assert lines[0].material_number=='7733701564'
    assert lines[0].supplier_material_number=='7733701564'
    assert lines[0].customer_material_number=='Z43-08'
    assert lines[0].quantity==35
    assert lines[0].unit_price==104
    assert lines[0].line_total==3640
    assert 'CL3000iUW26E' in lines[0].description
    assert charges[0].amount==61.25
    assert charges[0].parent_material_number=='7733701564'


def test_stacked_native_table_keeps_public_price_separate_from_net_price():
    t=UniversalTable(
        id='x',source=SourceRef(page=1),confidence=0.98,
        headers=['Référence','Désignation','P.P. HT','Quantité','Prix HT','Montant HT'],
        data=[
            ['Référence','Désignation','P.P. HT','Quantité','Prix HT','Montant HT'],
            ['8738206065\n8738203521\n000000001',
             'Commandé par le dépôt : NICE\nSONDE TEMPERATURE NTC10 1000MM\nSONDE EXTERIEURE\nPORT\n********\nHoraires de réception :',
             '47,00\n133,00\n20,00','1,00PCE\n1,00PCE\n1,00PCE','25,38\n71,82\n20,00','25,38\n71,82\n20,00']
        ]
    )
    lines=_stacked_column_native_table_items_v46(t,'EUR')
    assert len(lines)==3
    assert lines[0].description=='SONDE TEMPERATURE NTC10 1000MM'
    assert lines[0].gross_unit_price==47.0
    assert lines[0].unit_price==25.38
    assert lines[0].uom=='PCE'


def test_summary_type_guard_does_not_turn_weight_or_pack_count_into_money():
    cfg=load_config(None)
    pg=_page([
        (745,[('TOTAL',24,50),('POIDS',52,77),('DU',79,91),('BON',93,111),(':',113,116),('0,000',120,140),('Kg',142,153),('TOTAL',427,459),('HT',462,475),('DU',478,493),('BON',495,518),(':',520,524),('117,20',526,556),('€',559,565)]),
        (765,[('TOTAL',24,50),('CONDITIONNEMENT',52,131),(':',133,136),('3,000',153,173)]),
    ])
    po=PurchaseOrderResult(document=_meta())
    po.totals.grand_total=3.0
    po.totals.total_shipping=200020.0
    po=_sanitize_summary_totals_v46(po,[pg],cfg,'EUR')
    assert po.totals.total_net==117.2
    assert po.totals.total_weight==0.0
    assert po.totals.grand_total is None
    assert po.totals.total_shipping is None
