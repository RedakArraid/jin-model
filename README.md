# JIN Model - Universal Document AI V5.7 Cell & Sub-Zone Intelligence

`RedakArraid/jin-model` contient la couche Docker/UI autour du moteur JIN empaqueté et une nouvelle couche d'apprentissage statistique versionnée.

## Démarrage immédiat

Le dépôt supporte maintenant un mode **standalone CPU** qui n'a pas besoin du package `uda` pour utiliser les routeurs document/PDF/champs/zones/cellules.

1. Place `JIN_MODELS_AVAILABLE.zip` à la racine du dépôt.
2. Installe et vérifie les modèles :

```bash
./scripts/install-models.sh JIN_MODELS_AVAILABLE.zip
```

Sous PowerShell :

```powershell
.\\scripts\\install-models.ps1 .\\JIN_MODELS_AVAILABLE.zip
```

3. Lance :

```bash
docker compose -f docker-compose.standalone.yml up --build
```

4. Teste :

```bash
curl http://localhost:8000/health
curl -F "file=@commande.pdf" http://localhost:8000/analyze
```

Pour le mode complet `/extract`, prépare d'abord le moteur cœur avec `./prepare-model.sh <jin-core-engine.zip>`, puis utilise `docker compose up --build`.

Guide complet : [Runtime Setup](docs/RUNTIME_SETUP.md).

JIN commence maintenant à apprendre à partir des documents historiques corrigés au lieu d'évoluer uniquement par accumulation de règles.

## V5.7 - Cell & Sub-Zone Intelligence

V5.7 découpe les zones V5.6 en **colonnes, lignes et cellules sémantiques**. Les tableaux `LINE_ITEMS` exposent désormais `subzones[]`, `rows[]` et `cells[]`; les composants d'adresse, métadonnées commande et totaux reçoivent également un `cell_id`.

Sur les 15 PDF uniques disponibles pour la régression :
- **218/219 composants d'adresse (99,54 %)** sont rematchés à une cellule précise dans leur zone V5.6 ;
- **13/14 PDF natifs** ont un tableau de lignes segmenté ;
- 41 lignes de données et 309 cellules non-header ont été reconstruites.

Ces chiffres mesurent la **couverture de localisation**, pas une accuracy spatiale vérité-terrain. V5.7 ajoute donc aussi un évaluateur IoU/contamination à utiliser dès que des boîtes humaines revues seront disponibles.

Voir [V5.7 Cell & Sub-Zone Intelligence](docs/V5_7_CELL_SUBZONE_INTELLIGENCE.md).

## V5.6 - Spatial Zone Intelligence

JIN segmente maintenant la page en **zones métier explicites** avant d'accepter les champs appris : fournisseur, livraison, facturation, métadonnées commande, tableau de lignes et totaux.

Chaque zone expose séparément `structural_bbox`, `content_bbox`, `content_regions[]` et un `search_bbox` compact. Sur les 15 PDF uniques disponibles pour la régression spatiale, le ratio médian `search_bbox / contenu utile` passe d'environ **6,4x à 2,17x**, soit ~**66 % de surface parasite en moins**.

Les spans statistiques `ADDRESS_*`, `ORDER_*` et `TOTAL_*` sont désormais rejetés lorsqu'ils tombent hors d'une zone compatible détectée.

Voir [V5.6 Spatial Zone Intelligence](docs/V5_6_SPATIAL_ZONE_INTELLIGENCE.md).

## V5.5.2 - généralisation multi-gabarits

Le moteur géométrique ne dépend plus d'une disposition gauche/droite fixe. Il associe maintenant les blocs d'adresse à leurs ancres par proximité, sait lire des tableaux `N° Document / Pièce / N° Commande / Date`, les tableaux de synthèse `NET H.T. / TVA / TTC / NET A PAYER`, et normalise les plages `124 126`, `124,126`, `123 - 125`.

Le nouveau lot représente **17 exécutions sur 15 PDF uniques** : les 9 précédents plus 8 uploads WENDEL, SISCA, GARANKA, ISERBA/GAZ SERVICE RAPIDE et une offre SFCP, dont 2 PDF POISSY déjà présents dans le lot précédent. Les 9 anciens restent stables et les nouveaux champs critiques attendus sont retrouvés sans réintroduire les faux CP monétaires.

Le routeur PDF passe aussi en V3 avec un a priori statistique appris `P(decision | family)` sur le train uniquement. Sur les 396 validations : décision **93,43 % -> 94,19 %**, sans réglage de poids sur le holdout.

Voir [V5.5.2 Template Generalization](docs/V5_5_2_TEMPLATE_GENERALIZATION.md).

## V5.5.1 - durcissement géométrique

Le test sur 9 bons de commande réels a révélé que certains PDF stockent une même ligne visuelle dans plusieurs blocs internes. V5.5.1 reconstruit donc les lignes par coordonnées Y et combine cette géométrie avec le modèle statistique V5.5.

Sur ce lot de régression, les champs auparavant manquants passent à **9/9** pour le n° de commande, la date, le Total HT, le CP/ville fournisseur et le CP/ville livraison. Les faux candidats `RODAS DE` passent de 2 à 0 et les montants pris pour des codes postaux de 7 documents à 0.

Les suggestions restent `requires_review=true`. Voir [V5.5.1 Geometry Hardening](docs/V5_5_1_GEOMETRY_HARDENING.md).

## V5.5 - apprentissage faible des champs

JIN apprend maintenant aussi des **champs token/position** sur les vrais PDF, toujours en CPU. Le modèle V2 est entraîné sur la première page des 1 572 documents train avec des labels faibles déterministes haute précision, puis évalué sur les 396 documents validation.

Sur les 372 PDF validation disposant de texte natif, l'accord avec les labels faibles atteint **98,97 % token accuracy** et **97,82 % macro-F1**. Ces métriques ne sont pas présentées comme une accuracy terrain : chaque sortie reste `requires_review=true`.

Le runtime préfère le texte/les coordonnées PDF natives et bascule sur Tesseract OCR pour les scans. Il propose notamment numéro, type/nom de voie, code postal, ville, CEDEX, BP/CS/TSA, n°/date de commande et certains totaux. Les suggestions sont exposées dans `weak_field_suggestions` et **ne remplacent jamais silencieusement les champs du moteur cœur**.

Modèle attendu :

```text
data/learning/jin-field-weak-router-v2-cpu.joblib
```

Endpoint :

```bash
curl -F "file=@commande.pdf" http://localhost:8080/api/learning/field-route
```

Voir [V5.5 Weak Field Learning](docs/V5_5_WEAK_FIELD_LEARNING.md).

## V5.4 - apprentissage sur les vrais PDF

Le corpus complet est maintenant matérialisé et utilisé réellement : **1 572 PDF d'entraînement + 396 PDF de validation = 1 968 PDF réels**, soit **2 877 pages**. Aucun pointeur Git LFS ne reste dans le corpus utilisé pour le benchmark et aucun SHA-256 identique ne traverse train/validation.

La baseline de production est désormais **CPU-only** : texte PDF natif + géométrie des mots/pages + représentation visuelle 16×16 de la première page. Aucun GPU n'est requis.

Résultats sur les 396 PDF de validation, jamais utilisés pour ajuster les poids :

- famille documentaire : **99,75 %** (395/396), macro-F1 **99,30 %** ;
- `keep/review/remove` : **93,43 %**, macro-F1 **88,12 %**.

Le classifieur famille est calibré par validation croisée uniquement sur le split d'entraînement ; le classifieur de décision conserve le modèle brut, qui est plus précis sur ce corpus.

Le modèle attendu par le runtime :

```text
data/learning/jin-pdf-fusion-router-v3-cpu.joblib
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
- suggestions champs CPU : POST http://localhost:8080/api/learning/field-route

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
  --output data/learning/jin-pdf-fusion-router-v3-cpu.joblib \
  --metrics data/learning/jin-pdf-fusion-router-v3-cpu-metrics.json
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

`5.7.0-cell-subzone-intelligence`
