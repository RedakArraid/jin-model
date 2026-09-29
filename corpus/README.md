# Corpus d'entraînement JIN

Les PDF sources doivent être placés sous `corpus/pdfs/` et suivis par Git LFS. Les annotations JSON restent dans Git normal sous `corpus/annotations/`.

Le manifeste réel attendu est `corpus/manifest.jsonl`, une ligne JSON par document :

```json
{"document_id":"doc_001","pdf":"corpus/pdfs/customer/doc_001.pdf","annotation":"corpus/annotations/doc_001.json"}
```

Les annotations sont définies par page et par région :

```json
{"pages":[{"page":1,"regions":[
  {"label":"SHIP_TO_STREET_NAME","bbox":[170,220,650,250],"bbox_space":"pixel"},
  {"label":"LINE_ITEM_REFERENCE","bbox":[90,530,250,555],"bbox_space":"pixel"}
]}]}
```

Avant entraînement :

```bash
./scripts/materialize-lfs-corpus.sh
python training/vision/prepare_layout_dataset.py --manifest corpus/manifest.jsonl --output data/layout
python training/vision/train_layoutlmv3.py --data data/layout --output artifacts/layoutlmv3-jin
```

Ne pas commiter de PDF client hors Git LFS. `data/`, `feedback/` et `artifacts/` restent hors Git.
