"""Render proposed evidence boxes without modifying the source PDFs."""
import argparse
import json
from pathlib import Path
import fitz
from scripts.studio_complete_ambiguity_review import ROOT, ACTIVE


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('document_id')
    parser.add_argument('--page',type=int,default=1)
    args=parser.parse_args()
    plan=json.loads((ROOT/'plans'/(args.document_id+'.json')).read_text(encoding='utf-8'))
    destination=Path('tmp/pdfs/full-review')
    destination.mkdir(parents=True,exist_ok=True)
    with fitz.open(Path('data/studio/documents')/args.document_id/'source.pdf') as pdf:
        page=pdf[args.page-1]
        for f in plan['field_annotations']:
            e=f.get('evidence') or {}
            if f['status'] not in ACTIVE or e.get('page')!=args.page or not e.get('bbox'):
                continue
            a,b,c,d=e['bbox']
            bounds=fitz.Rect(a*page.rect.width/1000,b*page.rect.height/1000,c*page.rect.width/1000,d*page.rect.height/1000)
            # Annotation coordinates refer to the displayed, rotated page;
            # drawing primitives instead expect unrotated PDF coordinates.
            bounds = bounds * page.derotation_matrix
            color=(0,.45,.8) if f['label'].startswith('LINE_ITEM_') else (0,.6,.2)
            page.draw_rect(bounds,color=color,width=.6)
        output=destination/(args.document_id+'-review-'+str(args.page)+'.png')
        page.get_pixmap(matrix=fitz.Matrix(1.8,1.8),alpha=False).save(output)
        print(output)


if __name__=='__main__':
    main()
