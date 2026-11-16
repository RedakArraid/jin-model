"""Local OCR evidence for scanned pages and raster letterheads; no remote OCR."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
import json
import fitz
import pytesseract
from PIL import Image

ROOT = Path('data/evaluation/annotation-complete-review-20261116')


def recognize(snapshot):
    doc = json.loads(snapshot.read_text(encoding='utf-8'))
    destination = ROOT / 'ocr' / (doc['documentId'] + '.json')
    if destination.exists():
        return doc['documentId'], 'cached'
    result, errors = [], []
    with fitz.open(doc['pdf_path']) as pdf:
        for page in pdf:
            if page.rotation:
                page.remove_rotation()
            regions = [page.rect] if len(page.get_text('words')) < 20 else [
                fitz.Rect(block['bbox']) & page.rect for block in page.get_text('dict')['blocks']
                if block['type'] == 1 and fitz.Rect(block['bbox']).get_area() > page.rect.get_area() * .003
            ]
            for rect in regions:
                if rect.is_empty:
                    continue
                pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False)
                digest = sha256(pix.samples + str((pix.width, pix.height)).encode()).hexdigest()
                cache = ROOT / 'ocr' / (digest + '.json')
                try:
                    if cache.exists():
                        words = json.loads(cache.read_text(encoding='utf-8'))
                    else:
                        raw = pytesseract.image_to_data(Image.frombytes('RGB', (pix.width, pix.height), pix.samples),
                            lang='fra+eng', config='--psm 3' if rect == page.rect else '--psm 6',
                            output_type=pytesseract.Output.DICT, timeout=45)
                        words = [dict(text=text, confidence=float(raw['conf'][i]),
                            bbox=[raw['left'][i]/pix.width, raw['top'][i]/pix.height,
                                  (raw['left'][i]+raw['width'][i])/pix.width,
                                  (raw['top'][i]+raw['height'][i])/pix.height],
                            group=[raw['block_num'][i], raw['par_num'][i], raw['line_num'][i]])
                            for i, text in enumerate(raw['text']) if text.strip()]
                        cache.write_text(json.dumps(words, ensure_ascii=False), encoding='utf-8')
                    for word in words:
                        x0,y0,x1,y1 = word['bbox']
                        result.append({**word, 'page':page.number+1, 'source':'ocr', 'crop':digest,
                            'bbox':[(rect.x0+x0*rect.width)/page.rect.width*1000,
                                    (rect.y0+y0*rect.height)/page.rect.height*1000,
                                    (rect.x0+x1*rect.width)/page.rect.width*1000,
                                    (rect.y0+y1*rect.height)/page.rect.height*1000]})
                except Exception as exc:
                    errors.append({'page':page.number+1,'error':str(exc)})
    destination.write_text(json.dumps({'words':result,'errors':errors},ensure_ascii=False),encoding='utf-8')
    return doc['documentId'], len(result)


def main():
    (ROOT/'ocr').mkdir(exist_ok=True)
    snapshots = sorted(ROOT.glob('jdoc-*.json'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recognize,p) for p in snapshots]
        for i,future in enumerate(as_completed(futures)):
            print(i+1,len(snapshots),*future.result(),flush=True)


if __name__ == '__main__':
    main()
