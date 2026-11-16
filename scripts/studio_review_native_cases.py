"""Source-backed decisions for the two multipage layouts visually inspected.

These are annotation corrections, not hard-coded production extraction rules.
Every value is located again in the immutable PDF before being changed.
"""
from decimal import Decimal
import re

SOROFI='jdoc-32770cf7221a4260a81b4d7425141031'
SISCA='jdoc-c830a032ec0d41e4b37e98f2a72eed4e'
MULTI_ORDER='jdoc-1a2cb2cc40e54efc8cd780583ee1d0d7'


def review_case(document_id,fields,words,events):
    from scripts import studio_complete_ambiguity_review as r
    if document_id==MULTI_ORDER:
        for f in fields:
            if f['label'] in {'ORDER_NUMBER','ORDER_DATE','DELIVERY_DATE'} or r.LINE.search(f['path']):
                f['status']='AMBIGUOUS'
                f['comment']='Le PDF contient plusieurs commandes indépendantes (PR 2 1A4 63443, PR 2 1A4 63003 et une troisième page). Scinder le document avant de valider : les lignes et dates actuelles mélangent plusieurs commandes.'
        events.append({'kind':'multi_order_pdf','reason':'Plusieurs numéros de commande et dates, avec pagination remise à 1. Scission nécessaire avant apprentissage.'})
        return
    if document_id not in {SOROFI,SISCA}:
        return
    by_label={f['label']:f for f in fields if not f['label'].startswith(('LINE_ITEM_','ADDITIONAL_CHARGE_'))}

    def locate(label,printed,page,region,canonical=None):
        candidates=r.matches(r.selected(words,page,*region),printed)
        # Ignore punctuation-only variants of the same occurrence.
        unique={tuple(tuple(w['bbox']) for w in c if r.normalized(w['text'])):c for c in candidates}
        if len(unique)!=1:
            events.append({'kind':'visual_source_not_unique','label':label,'printed':printed,'count':len(unique)})
            return
        f=by_label.get(label)
        if f is None and label.endswith('_STREET'):
            role=label[:-7].lower()
            f={'label':label,'path':f'order.addresses.{role}.selected.components.street','status':'UNREVIEWED','extracted_value':None}
            fields.append(f)
            by_label[label]=f
        if f:
            r.set_source(f,canonical if canonical is not None else printed,next(iter(unique.values())),
                         'Lecture visuelle recoupée avec le texte natif dans le bloc métier et la page indiqués.',force=True)

    if document_id==SOROFI:
        # Independent legal-footer issuer, separate from the explicit billing box.
        for label,printed in {
            'BUYER_NAME':'S.A.S. SOROFI','BUYER_VAT_NUMBER':'FR 13390461440',
            'BUYER_STREET':"48 quai Commandant L'herminier",'BUYER_CS':'CS 40032',
            'BUYER_POSTAL_CODE':'42335','BUYER_CITY':'Roanne',
        }.items():
            locate(label,printed,1,(0,1000,960,1000))
        # Two printed SIRENs (RCS / Siren); use the explicit Siren occurrence.
        locate('BUYER_SIREN','390 461 440',1,(565,660,960,1000),'390461440')
        locate('BUYER_CITY','Roanne',1,(640,750,973,995))
        locate('ORDER_NUMBER','2309888 / D05',1,(140,450,260,285))
        locate('ORDER_DATE','05/11/2025',1,(20,145,260,285),'2025-11-05')
        for role,region,values in [
            ('SUPPLIER',(470,950,165,235),{'NAME':'BOSCH - ELM LEBLANC PRODUITS FINIS','STREET':'124 - 126 RUE STALINGRAD','POSTAL_CODE':'93711','CITY':'DRANCY','COUNTRY':'FRANCE'}),
            ('BILL_TO',(465,930,40,125),{'NAME':'SOROFI SAS','STREET':'48 QUAI DU CDT LHERMINIER','CS':'CS 40032','POSTAL_CODE':'42335','CITY':'ROANNE','CONTACT_PHONE':'04 77 44 08 62'}),
            ('SHIP_TO',(30,460,50,170),{'NAME':'Entrepot SOROFI Coteau','STREET':'Rue Antoine Pinay','INDUSTRIAL_ZONE':'Z.I. Des Guerins','POSTAL_CODE':'42120','CITY':'LE COTEAU','COUNTRY_CODE':'FR','CONTACT_PHONE':'04 77 62 84 70'}),
        ]:
            for suffix,printed in values.items():
                locate(role+'_'+suffix,printed,1,region)
        locate('TOTAL_NET','3 078,63',2,(850,970,840,865),'3078.63')
        if 'TOTAL_VAT' in by_label:
            r.absent(by_label['TOTAL_VAT'],'Le document porte un total HT et une éco-taxe, pas un montant de TVA. La valeur concaténée précédente ne correspond à aucune cellule.')
        # Buyer phone has no independent footer occurrence. Do not reuse the
        # billing phone automatically, and retain the question for the reviewer.
        target_rows=10
        expected_fees=Decimal('46.08')
    else:
        footer=(0,1000,930,970)
        for label,printed in {
            'BUYER_NAME':'SAS SISCA','BUYER_STREET':'144,Route de Toulouse',
            'BUYER_POSTAL_CODE':'65600','BUYER_CITY':'SEMEAC','BUYER_COUNTRY':'FRANCE',
            'BUYER_CONTACT_PHONE':'05.62.33.84.00','BUYER_VAT_NUMBER':'FR0K352747018',
            'BUYER_SIRET':'35274701800013',
        }.items():
            locate(label,printed,2,footer)
        locate('ORDER_DATE','29/10/2025',1,(45,140,305,325),'2025-10-29')
        for role,region,values in [
            ('SUPPLIER',(520,900,75,170),{'NAME':'ELM LEBLANC (BOSCH)','STREET':'124,126 rue de Stalingrad','POSTAL_CODE':'93711','CITY':'Drancy','COUNTRY':'FRANCE','CONTACT_PHONE':'08 20 00 30 00','CONTACT_FAX':'02.98.79.47.57'}),
            ('BILL_TO',(565,920,190,252),{'NAME':'SISCA SAS','STREET':'144 Route de Toulouse','POSTAL_CODE':'65600','CITY':'SEMEAC','COUNTRY':'FRANCE'}),
            ('SHIP_TO',(50,455,99,171),{'NAME':'PLF St GEOURS de MAREMNE','STREET':"180 rue du Marais d'Orx",'INDUSTRIAL_ZONE':'Zone Atlantisud','POSTAL_CODE':'40230','CITY':'SAINT GEOURS DE MAREMNE'}),
        ]:
            for suffix,printed in values.items():
                locate(role+'_'+suffix,printed,1,region)
        locate('TOTAL_GROSS','5864,58',2,(705,817,860,885),'5864.58')
        locate('TOTAL_AMOUNT_DUE','5864,58',2,(815,950,860,885),'5864.58')
        for label in ('BUYER_COUNTRY_CODE','SUPPLIER_COUNTRY_CODE','BILL_TO_COUNTRY_CODE','SHIP_TO_COUNTRY','SHIP_TO_COUNTRY_CODE','BUYER_SIREN'):
            if label in by_label:
                r.absent(by_label[label],'Pas de valeur autonome imprimée pour ce champ. Le pays écrit en toutes lettres ou le SIRET sont annotés séparément ; ne pas créer une preuve pour un code dérivé.')
        for f in list(fields):
            if f['label'] in {'BILL_TO_VAT_NUMBER','BILL_TO_SIREN','BILL_TO_SIRET'}:
                fields.remove(f)
                events.append({'kind':'removed_role_duplicate','field':f,'reason':'Identifiants légaux du pied de page annotés comme acheteur, pas comme occurrence indépendante dans le bloc facturation.'})
        target_rows=9
        expected_fees=Decimal('16.03')

    # Remove only atomic components whose complete street now has a source.
    for role in ('BUYER','SUPPLIER','SHIP_TO','BILL_TO'):
        street=by_label.get(role+'_STREET')
        if street and street['status'] in r.ACTIVE:
            for f in list(fields):
                if f['label'] in {role+'_'+part for part in r.ATOMIC}:
                    fields.remove(f)
                    events.append({'kind':'removed_street_component','field':f,'replacement_path':street['path']})

    # Row references and numeric cells are read independently, never zipped on
    # model order unless every reference agrees with the complete source table.
    sorofi=document_id==SOROFI
    anchors=[w for w in words if 25<w['bbox'][0]<(160 if sorofi else 180) and
             (340 if sorofi else 525)<w['bbox'][1]<940 and re.fullmatch(r'\d{10}',w['text'])]
    anchors.sort(key=lambda w:(w['page'],w['bbox'][1]))
    refs=sorted([f for f in fields if f['label']=='LINE_ITEM_REFERENCE'],key=lambda f:int(r.LINE.search(f['path']).group(1)))
    if len(anchors)!=target_rows or len(refs)!=target_rows or any(r.normalized(r.value(f))!=r.normalized(w['text']) for f,w in zip(refs,anchors)):
        events.append({'kind':'table_alignment_review','reason':'Reference count/order mismatch on inspected layout'})
        return
    for i,a in enumerate(anchors):
        y,p=a['bbox'][1],a['page']
        boundaries=(550,640,830,890,895,975) if sorofi else (750,805,810,878,885,950)
        q0,q1,p0,p1,t0,t1=boundaries
        cells={'LINE_ITEM_REFERENCE':[a],
               'LINE_ITEM_QUANTITY':r.selected(words,p,q0,q1,y-2,y+7),
               'LINE_ITEM_UNIT_PRICE':r.selected(words,p,p0,p1,y-2,y+7),
               'LINE_ITEM_TOTAL':r.selected(words,p,t0,t1,y-2,y+7)}
        # Unit is printed next to the quantity for SOROFI only.
        if sorofi:
            cells['LINE_ITEM_UNIT']=[w for w in cells['LINE_ITEM_QUANTITY'] if w['text']=='EA']
            cells['LINE_ITEM_QUANTITY']=[w for w in cells['LINE_ITEM_QUANTITY'] if w['text']!='EA']
        nums=[r.decimal_value(r.text(cells[k])) for k in ('LINE_ITEM_QUANTITY','LINE_ITEM_UNIT_PRICE','LINE_ITEM_TOTAL')]
        if any(n is None for n in nums) or abs(nums[0]*nums[1]-nums[2])>Decimal('.05'):
            events.append({'kind':'table_arithmetic_review','row':i})
            continue
        end=anchors[i+1]['bbox'][1]-2 if i+1<len(anchors) and anchors[i+1]['page']==p else min(y+80,940)
        description=[]
        for line in sorted(r.rows(r.selected(words,p,240 if sorofi else 183,548 if sorofi else 560,y-2,end)),key=lambda line:line[0]['bbox'][1]):
            raw=r.folded(r.text(line))
            if re.match(r'soit\s*:|n.?\s*interne|eco|deee|cli\s|selon\s|--|total',raw):
                break
            description.extend(line)
        if description:
            cells['LINE_ITEM_DESCRIPTION']=description
        for f in fields:
            m=r.LINE.search(f['path'])
            if not m or int(m.group(1))!=i:
                continue
            if cells.get(f['label']):
                r.set_source(f,r.text(cells[f['label']]),cells[f['label']],
                             'Cellule relue dans le tableau inspecté, hors sous-lignes de frais et références internes.',force=True)
            elif f['label']=='LINE_ITEM_NUMBER' or not sorofi and f['label']=='LINE_ITEM_UNIT':
                r.absent(f,'Pas de colonne de numéro ordinal/unité textuelle imprimée. Ne pas transformer les colonnes de conversion UA/UP en unité.')

    fees=[]
    for w in words:
        if w['text'] not in ({'Eco-taxe','DEEE'} if sorofi else {'Eco-Participation'}):
            continue
        if not (230 if sorofi else 180)<w['bbox'][0]<(550 if sorofi else 560) or not 340<w['bbox'][1]<850:
            continue
        amount=r.selected(words,w['page'],895 if sorofi else 885,975 if sorofi else 950,w['bbox'][1]-2,w['bbox'][1]+7)
        if r.decimal_value(r.text(amount)) is not None:
            fees.append((w,amount))
    fees.sort(key=lambda item:(item[0]['page'],item[0]['bbox'][1]))
    if sum((r.decimal_value(r.text(amount)) for _,amount in fees),Decimal(0))!=expected_fees:
        events.append({'kind':'fee_sum_review','expected':str(expected_fees),'found':len(fees)})
        return
    previous=[f for f in fields if f['label'].startswith('ADDITIONAL_CHARGE_')]
    for f in previous:
        fields.remove(f)
        events.append({'kind':'replaced_charge_field','field':f,'reason':'Reconstruction depuis les frais unitaires imprimés, sans doubler leur total récapitulatif.'})
    for i,(w,amount) in enumerate(fees):
        for label,suffix,source in [('ADDITIONAL_CHARGE_LABEL','description',[w]),('ADDITIONAL_CHARGE_AMOUNT','amount',amount)]:
            f={'label':label,'path':f'order.additional_charges[{i}].{suffix}','extracted_value':None,'status':'UNREVIEWED'}
            r.set_source(f,r.text(source),source,'Frais imprimé indépendant ; somme des montants vérifiée contre le total éco-taxe/éco-participation.',force=True)
            fields.append(f)
