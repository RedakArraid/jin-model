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

Les Compose utilisent `runc` par défaut afin de rester indépendants d'un
runtime global Docker Desktop incompatible. Surcharge possible avec
`CONTAINER_RUNTIME`.

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

Seules les corrections explicitement validées par un humain sont envoyées à
`POST /feedback`. Elles sont persistées localement puis intégrées par un
entraînement en lot avant d'être réutilisées sur les extractions suivantes.

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
curl -X POST http://localhost:8080/api/feedback \
  -H "Authorization: Bearer ${JIN_FEEDBACK_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @feedback-v1.json
```

`POST /feedback` est désactivé tant que `JIN_FEEDBACK_TOKEN` n'est pas
configuré. Il accepte uniquement le
[contrat de feedback humain v1](docs/FEEDBACK_CONTRACT_V1.md). Le
réentraînement est exécuté en batch dans Docker par défaut, afin que l'ingestion
reste rapide lorsque l'historique grandit.

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

## Extraction locale CPU et contrôle qualité

Le Compose complet active `JIN_OFFLINE=1` : backend local, pas de géocodage
Internet, pas de moteur Azure ni de modèle distant. L'API rejoint uniquement
le réseau Docker interne `inference`; l'interface reste sur
http://localhost:8080. La construction initiale de l'image et l'installation
des dépendances nécessitent encore Internet.

Les correctifs du moteur empaqueté sont versionnés dans `core_overrides/`.
Ils sont appliqués par `prepare-model.sh` et au build Docker, même si `model/`
reste ignoré par Git. Ces correctifs sont validés avec le cœur local 5.1.0 ;
une autre version d'archive doit repasser les tests avant utilisation.
Ils couvrent notamment les libellés d'adresse sur
plusieurs lignes, les contacts liés à leur rôle, le routage commande/devis/CGV,
les tableaux multipages et les PDF hybrides texte + en-têtes raster.

`/extract` expose `extraction_decision` après tous les enrichissements :

- `REVIEW_REQUIRED` : champs manquants, conflits ou contrôles non satisfaits,
  avec des codes et chemins explicites ;
- `CHECKS_PASSED` : contrôles automatiques satisfaits, **pas une garantie
  d'exactitude ni une autorisation d'intégration automatique**.

Les rôles livraison/facturation sont requis pour passer ces contrôles ; leur
absence peut être légitime dans la source mais nécessite alors une revue.
Le numéro de commande client reste dans `purchase_order.number`, distinct de
`customer_reference` (référence client), `quote_number` (devis) et des codes
produit. `order_number_check` conserve sa preuve page/coordonnées/texte,
signale les preuves manquantes, les dates suspectes et les candidats
contradictoires, sans modifier le numéro ni supprimer ses zéros initiaux.
Les adresses ne sont pas déclarées réelles/vérifiées sans référentiel local
approprié. Les corrections financières ne remplacent jamais les chiffres
imprimés. Aucun apprentissage n'est fait sur les prédictions non revues.

### Référentiel d'adresses local

La BAN peut être indexée localement sans conserver les CSV décompressés :

```powershell
python scripts/build_ban_index.py --all --output data/reference/ban
```

L'index est reprenable, construit une empreinte exacte des adresses et un
dictionnaire départemental des voies/communes. Une correspondance exacte peut
valider l'existence d'une adresse française ; une voie proche reste une simple
suggestion avec revue obligatoire. Le référentiel ne change jamais le rôle
`ship_to`, `bill_to`, `buyer` ou `supplier`. Source : Base Adresse Nationale,
Licence Ouverte 2.0. Une absence BAN n'est jamais présentée comme preuve que
l'adresse est fausse (cas possibles : CEDEX, BP, donnée récente ou incomplète).
La BAN publiant des fichiers quotidiens, relancer la commande avec `--force`
reconstruit les départements à partir de la version courante.

### JSON métier propre

La réponse complète contient désormais `normalized_output` avec un contrat
stable `jin-clean-extraction-v1` : document, commande, parties, adresses,
lignes, frais, totaux, qualité et preuves. Les détails OCR restent dans la
réponse historique pour le diagnostic. Pour recevoir uniquement le contrat :

```bash
curl -F "file=@commande.pdf" "http://localhost:8080/api/extract?view=clean"
```

Le schéma JSON formel du contrat est disponible sur
`http://localhost:8080/api/schemas/jin-clean-extraction-v1` et dans
`jin_runtime/schemas/jin-clean-extraction-v1.schema.json`.

Le bouton de téléchargement de l'interface exporte cette vue propre.

Pour la livraison, `order.delivery_address` fournit directement le bloc
sélectionné avec `formatted_lines`, `formatted`, `components`, `normalization`,
`verification` et la valeur source. Le contact et la société restent séparés
du libellé postal. Plusieurs candidats `ship_to` ne sont jamais départagés
silencieusement : la décision passe en revue avec `DELIVERY_ADDRESS_AMBIGUOUS`.

Les codes d'agence, de site ou de succursale du client sont exposés séparément
dans `order.customer_agency_code` et, lorsqu'ils sont liés à la livraison, dans
`order.delivery_address.customer_agency_code`. Un code non explicitement
libellé n'est promu que s'il est corroboré dans l'en-tête et le bloc de
livraison ; il est alors exclu du libellé postal propre mais conservé dans la
preuve source.

Benchmark local reproductible, depuis un environnement ayant les dépendances
du moteur et du runtime ainsi que Tesseract :

```powershell
python scripts/benchmark_cpu.py ARCHIVES_CDES_ESKER_PDF_002001-005000 --limit 20 --seed 42 --output data/evaluation/cpu-sample.json
```

Le script déduplique par SHA-256, interdit le réseau dans le processus et ne
modifie pas les PDF. `--file-list liste.json` rejoue une sélection précise ;
`--expected attendus.json` compare uniquement des valeurs relues explicitement
fournies (numéro, type, nombre de lignes, total HT, composants par rôle et
contacts). Les rapports mesurent couverture, alertes et temps CPU ; ils ne
prétendent pas mesurer une précision terrain sans annotations indépendantes.
Les fichiers de résultats et les valeurs client attendues restent dans
`data/evaluation/`, ignoré par Git.

## Version

`5.9.1-customer-agency-code`
