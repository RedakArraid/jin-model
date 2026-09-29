# JIN Model - Universal Document AI V5.2 Statistical Learning

`RedakArraid/jin-model` contient la couche Docker/UI autour du moteur JIN empaqueté et une nouvelle couche d'apprentissage statistique versionnée.

JIN commence maintenant à apprendre à partir des documents historiques corrigés au lieu d'évoluer uniquement par accumulation de règles.

## V5.2

La mémoire statistique apprend :

- les rôles d'adresse (`ship_to`, `bill_to`, `supplier`, `buyer`, etc.) ;
- les composants structurés d'une adresse : bâtiment, numéro, suffixe, type/nom de voie, zones, BP/TSA/CS, code postal, ville, CEDEX, INSEE, région, pays, etc.

Les corrections sont envoyées à `POST /feedback`, persistées localement puis réutilisées sur les extractions suivantes.

La couche reste prudente : elle complète d'abord les champs manquants, ne remplace un rôle que si le signal existant est faible et ne positionne jamais `is_verified_real_address=true`. La validation d'existence reste du ressort de la BAN/Géoplateforme ou d'un référentiel officiel approuvé.

Voir [la documentation statistique](docs/STATISTICAL_LEARNING.md).

## Layout / vision

Le dépôt prépare également la prochaine évolution : fine-tuning de `microsoft/layoutlmv3-base` (~133 M paramètres) sur les pages PDF complètes, positions de champs et cellules de tableaux.

Au moment de cette évolution, aucun objet/pointeur PDF Git LFS réel n'était présent dans `jin-model`. Le pipeline est donc prêt, mais aucun entraînement sur ce corpus absent n'est prétendu.

Voir [Git LFS et layout/vision](docs/LAYOUT_VISION_LFS.md).

## Architecture

```text
Moteur JIN empaqueté (model/ local)
        |
        v
jin_runtime.app
  |-- proxy des routes cœur
  |-- enrichissement statistique de /extract
  |-- POST /feedback
  |-- GET /learning/status
        |
        v
Docker Compose / Nginx

Corrections JSON ----------> modèles statistiques légers
PDF Git LFS + annotations -> LayoutLMv3 (hors ligne / workflow manuel)
```

## Installation

```bash
git clone https://github.com/RedakArraid/jin-model.git
cd jin-model
./prepare-model.sh <jin-engine.zip>
docker compose up --build
```

Accès :

- UI : http://localhost:8080
- santé : http://localhost:8080/api/health
- apprentissage : http://localhost:8080/api/learning/status

Le ZIP moteur doit contenir `requirements.txt` et le package `uda/`. `prepare-model.sh` accepte une archive V4.8 ou V5.x.

## API

```bash
curl http://localhost:8080/api/health
curl -F "file=@commande.pdf" http://localhost:8080/api/extract
curl http://localhost:8080/api/learning/status
curl -X POST http://localhost:8080/api/feedback -H "Content-Type: application/json" -d @my-corrected-extraction.json
```

## Entraînement statistique

```bash
python training/train_statistical.py --feedback feedback/learning_feedback.jsonl --model-dir data/learning
```

Seuils runtime par défaut :

```text
JIN_ROLE_OVERRIDE_THRESHOLD=0.93
JIN_COMPONENT_FILL_THRESHOLD=0.88
JIN_ALLOW_ROLE_OVERRIDE=1
JIN_FILL_MISSING_COMPONENTS=1
```

## Corpus PDF Git LFS

Les PDF réels doivent être placés sous `corpus/pdfs/` et suivis via `.gitattributes`.

```bash
git lfs install
./scripts/materialize-lfs-corpus.sh
pip install -r requirements-vision.txt
python training/vision/prepare_layout_dataset.py --manifest corpus/manifest.jsonl --output data/layout
python training/vision/train_layoutlmv3.py --data data/layout --output artifacts/layoutlmv3-jin
```

Le workflow GitHub Actions **Train layout vision model** peut préparer les données et lancer le fine-tuning une fois le vrai corpus LFS disponible.

## Tests

```bash
pip install -r runtime-requirements.txt
python -m unittest discover -s tests -v
```

La CI compile aussi les sources Python et valide les scripts shell.

## Données

`model/`, `data/`, `feedback/`, `artifacts/` et les ZIP locaux sont ignorés par Git. Les PDF clients doivent être stockés via Git LFS et soumis à la gouvernance de données applicable.

## Version

`5.2.0-statistical-learning`
