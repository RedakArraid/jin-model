# Corpus Git LFS et entraînement layout/vision

> Document de recherche historique. La production actuelle est CPU-only et
> utilise les routeurs PDF/champs locaux ; LayoutLMv3 reste une piste optionnelle
> et n'est pas une dépendance du service d'extraction du runtime 5.19.0.

La mémoire statistique légère ne voit pas la géométrie complète de la page. La prochaine couche apprend les positions de champs et les tableaux sur les pages PDF complètes.

Le pipeline cible `microsoft/layoutlmv3-base` (~133 M paramètres) pour une classification de tokens sensible au layout.

## État réel du corpus

Lors de l'ajout de cette couche, `RedakArraid/jin-model` ne contenait aucun objet/pointeur PDF Git LFS utilisable. Le dépôt est donc préparé pour le corpus, mais aucun entraînement LayoutLMv3 sur ces PDF n'est revendiqué tant que les vrais objets n'ont pas été poussés.

## Chaîne

1. `./scripts/materialize-lfs-corpus.sh` récupère et vérifie les objets LFS ;
2. `prepare_layout_dataset.py` rend les pages, OCRise les mots, normalise les boîtes dans `[0,1000]` et matérialise la séparation train/validation/test figée par document ;
3. `train_layoutlmv3.py` fine-tune LayoutLMv3 sur champs et cellules de tableaux ;
4. `benchmark_layoutlmv3.py` compare l'artefact au JIN actif sur le test figé avant toute promotion runtime.

```bash
python training/vision/prepare_layout_dataset.py --manifest corpus/manifest.jsonl --output data/layout
python training/vision/train_layoutlmv3.py --data data/layout --output artifacts/layoutlmv3-jin
```

Le workflow GitHub Actions `Train layout vision model` exécute manuellement la même chaîne après matérialisation des objets LFS.

## Porte de release

Mesurer au minimum précision/rappel/F1 par label, exact-match des identifiants/dates/codes postaux/totaux/références, exactitude des composants d'adresse, reconstruction de tableaux, performance par famille de template et faux positifs.
