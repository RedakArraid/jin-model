"""Export a read-only, reproducible Studio/PDF review snapshot inside Linux."""
from pathlib import Path
import json
import fitz
from jin_runtime.studio_store import StudioStore


def main():
    destination = Path('data/evaluation/annotation-complete-review-20261116')
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.glob('jdoc-*.json')):
        raise RuntimeError('Immutable review snapshot already exists; use a new campaign directory.')
    store = StudioStore()
    documents = store.list_documents(limit=10000)
    for index, document in enumerate(documents):
        path = store.pdf_path(document['documentId'])
        with fitz.open(path) as pdf:
            document['source_pages'] = [
                {'number': page.number + 1, 'size': list(page.rect),
                 'text': page.get_text(), 'words': page.get_text('words')}
                for page in pdf
            ]
        document['pdf_path'] = str(path)
        (destination / (document['documentId'] + '.json')).write_text(
            json.dumps(document, ensure_ascii=False), encoding='utf-8')
        if index % 20 == 0:
            print(f'{index + 1}/{len(documents)}', flush=True)
    print(f'Snapshot: {len(documents)} documents, {destination}', flush=True)


if __name__ == '__main__':
    main()
