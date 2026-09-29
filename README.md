# JIN Model - Universal Document AI V5.4 Real PDF CPU Learning

`RedakArraid/jin-model` contient la couche Docker/UI autour du moteur JIN empaqueté et une nouvelle couche d'apprentissage statistique versionnée.

JIN commence maintenant à apprendre à partir des documents historiques corrigés au lieu d'évoluer uniquement par accumulation de règles.

## V5.4 - apprentissage sur les vrais PDF

Le corpus complet est maintenant matérialisé et utilisé réellement : **1 572 PDF d'entraînement + 396 PDF de validation = 1 968 PDF réels**, soit **2 877 pages**. Aucun pointeur Git LFS ne reste dans le corpus utilisé pour le benchmark et aucun SHA-256 identique ne traverse train/validation.

La baseline de production est désormais **CPU-only** : texte PDF natif + géométrie des mots/pages + représentation visuelle 16×16 de la première page. Aucun GPU n'est requis.

Résultats sur les 396 PDF de validation, jamais utilisés pour ajuster les poids :

- famille documentaire : **99,75 %** (395/396), macro-F1 **99,30 %** ;
- `keep/review/remove` : **93,43 %**, macro-F1 **88,12 %**.

Le classifieur famille est calibré par validation croisée uniquement sur le split d'entraînement ; le classifieur de décision conserve le modèle brut, qui est plus précis sur ce corpus.

Le modèle attendu par le runtime :

```text
data/learning/jin-pdf-fusion-router-v2-cpu.joblib
```

Voir [V5.4 - entraînement vrais PDF](docs/V5_4_REAL_PDF_TRAINING.md).

## V5.2

La mémoire statistique apprend :

- les rôles d'adresse (`ship_to`, `bill_to`, `supplier`, `buyer`, etc.) ;
- les composants structurés d'une adresse : bâtiment, numéro, suffixe, type/nom de voie, zones, BP/TSA/CS, code postal, ville, CEDEX, INSEE, région, pays, etc.

Les corrections sont envoyées à `POST /feedback`, persistées localement puis réutilisées sur les extractions suivantes.

La couche reste prudente : elle complète d'abord les champs manquants, ne remplace un rôle que si le signal existant est faible et ne positionne jamais `is_verified_real_address=true`. La validation d'existence reste du ressort de la BAN/Géoplateforme ou d'un référentiel officiel approuvé.

Voir [la documentation statistique](docs/STATISTICAL_LEARNING.md).

## Layout / vision

La voie de production ne dépend plus d'un GPU : le routeur V5.4 apprend directement les vrais PDF avec PyMuPDF, une représentation visuelle basse résolution, la géométrie et le texte natif.

Le code LayoutLMv3 reste disponible comme piste de recherche, mais il n'est plus une dépendance de déploiement. Pour apprendre les **champs** eux-mêmes (adresses, lignes, totaux), le blocage restant est la présence de labels région/token revus, pas la puissance de calcul.

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

Corrections JSON -----------------> mémoire statistique adresses
Vrais PDF + labels documentaires -> routeur multimodal CPU V5.4
JSON corrigés + vrais PDF --------> pré-annotations champs à revoir
Annotations revues ---------------> futur extracteur champ-par-champ CPU
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
- routeur PDF CPU : POST http://localhost:8080/api/learning/pdf-route

Le ZIP moteur doit contenir `requirements.txt` et le package `uda/`. `prepare-model.sh` accepte une archive V4.8 ou V5.x.

## API

```bash
curl http://localhost:8080/api/health
curl -F "file=@commande.pdf" http://localhost:8080/api/extract
curl http://localhost:8080/api/learning/status
curl -X POST http://localhost:8080/api/feedback -H "Content-Type: application/json" -d @my-corrected-extraction.json
```

## Entraînement CPU sur les vrais PDF

```bash
pip install -r requirements-pdf-training.txt

python training/pdf/train_cpu_multimodal.py \
  --corpus-root /chemin/vers/corpus \
  --metadata-dir /chemin/vers/metadata \
  --output data/learning/jin-pdf-fusion-router-v2-cpu.joblib \
  --metrics data/learning/jin-pdf-fusion-router-v2-cpu-metrics.json
```

Le dossier corpus doit contenir `test/` et `validation/`. Le script vérifie les labels par SHA-256 lorsque le manifeste le permet.

Test direct d'un PDF via l'API :

```bash
curl -F "file=@commande.pdf" http://localhost:8080/api/learning/pdf-route
```

## Préparer les labels champ-par-champ

Le corpus actuel fournit des labels documentaires, pas des boîtes revues pour chaque champ. V5.4 ajoute donc un bootstrap à partir des **sorties JIN corrigées** :

```bash
python training/annotations/bootstrap_from_extractions.py \
  --pdf-root /chemin/vers/corpus \
  --extractions-dir /chemin/vers/json-corriges \
  --output-dir corpus/annotations \
  --report data/annotation_bootstrap_report.json
```

Le script retrouve les valeurs structurées dans les mots/coordonnées du PDF et produit des boîtes normalisées avec :

```json
{
  "annotation_source": "jin_structured_output_weak_label",
  "requires_review": true
}
```

Ces pré-annotations ne sont **pas** considérées comme vérité terrain avant revue. Elles servent à construire le dataset nécessaire pour apprendre ensuite les adresses, lignes, dates, références et totaux au niveau champ.

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

`5.4.0-real-pdf-cpu-learning`
