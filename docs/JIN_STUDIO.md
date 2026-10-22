# JIN Studio — annotations et datasets

JIN Studio sépare les activités de construction du modèle du parcours métier
de GenieCommande. L'interface est disponible sur `/studio.html` et son API sous
`/studio` (donc `/api/studio` derrière le proxy Nginx).

## Capacités du premier incrément

- dépôt d'un PDF puis extraction par le runtime JIN actif ;
- file de documents à annoter ;
- préremplissage des champs canoniques reconnus ;
- statuts par champ : correct, corrigé, absent, ambigu ou à vérifier ;
- dessin d'une zone normalisée sur chaque page du PDF ;
- brouillons et révisions immuables ;
- validation humaine explicite avec identifiant de l'annotateur ;
- création de datasets versionnés depuis les seules annotations validées ;
- export d'un manifeste et de régions compatibles avec le pipeline layout ;
- mise en file et exécution isolée d'un entraînement candidat.

Le circuit historique `POST /feedback` reste disponible pour la mémoire
statistique d'adresses. JIN Studio utilise un contrat général v2 indépendant et
ne migre aucune ancienne correction implicitement.

## Configuration

```text
JIN_STUDIO_TOKEN=<secret-long-et-aleatoire>
JIN_STUDIO_DIR=/app/data/studio
JIN_STUDIO_SHARED_STORAGE=1
JIN_STUDIO_MIN_TRAINING_DOCUMENTS=20
JIN_TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
```

Lorsque `JIN_STUDIO_TOKEN` est vide, le runtime accepte temporairement
`JIN_FEEDBACK_TOKEN` pour assurer la compatibilité locale. En environnement
partagé, utiliser deux secrets distincts.

Le navigateur conserve le token dans `sessionStorage`, uniquement pour
l'onglet courant. Il l'envoie dans `Authorization: Bearer ...`; il n'est jamais
placé dans l'URL. La route et le stockage doivent rester derrière HTTPS et un
contrôle d'accès réseau.

`JIN_STUDIO_SHARED_STORAGE=1` est activé par Compose pour permettre à l'API et
au worker, isolés dans deux espaces utilisateurs Docker, d'écrire dans la même
base. Il élargit les droits Unix uniquement sous `JIN_STUDIO_DIR`, jamais sur
le reste du volume `data/`.

## Stockage

Toutes les données sont conservées sous `JIN_STUDIO_DIR` :

```text
studio.sqlite3
documents/<document-id>/source.pdf
datasets/<dataset-id>/manifest.jsonl
datasets/<dataset-id>/annotations/*.json
training/<run-id>/
```

Les volumes `data/` restent ignorés par Git. Les PDF client et annotations ne
doivent pas être ajoutés au dépôt source.

## Contrat d'annotation v2

Une annotation référence obligatoirement :

- l'identifiant technique du document ;
- le SHA-256 du PDF ;
- une révision monotone ;
- un champ canonique et un label contrôlé ;
- la valeur extraite et, si nécessaire, la valeur corrigée ;
- le statut de revue ;
- éventuellement une page et une boîte `[x0,y0,x1,y1]` normalisée sur 1000 ;
- l'annotateur et l'horodatage pour `HUMAN_VALIDATED` ;
- un digest immuable.

Une validation est refusée si un champ reste `UNREVIEWED`. Un dataset refuse
tout document dont la dernière révision n'est pas `HUMAN_VALIDATED`.

## Dataset et entraînement

Le dataset est `READY` à partir de 20 documents validés contenant au moins une
zone spatiale. Ce seuil est une garde minimale, pas une preuve que le corpus est
suffisamment diversifié.

L'API place les entraînements dans une file SQLite. Le conteneur `trainer`
réclame une exécution, prépare les pages OCR puis entraîne LayoutLMv3. Il publie
un heartbeat et conserve le journal ainsi que l'artefact sous
`training/<run-id>/`. Une exécution abandonnée est remise en file après le délai
configuré. L'API de production n'importe pas PyTorch et ne lance jamais ces
calculs.

Le worker est volontairement derrière le profil Docker `training` :

```powershell
docker compose --profile training up -d --build trainer
```

Sans ce profil, les demandes restent simplement au statut `QUEUED`. Le premier
entraînement peut télécharger le modèle de base dans `data/huggingface`; en
environnement sans accès réseau, ce cache doit être alimenté à l'avance. Les
commandes stockées ne sont jamais exécutées dans un shell : le worker n'accepte
que les deux scripts de la chaîne LayoutLMv3 et vérifie les chemins de données.
L'image locale installe PyTorch CPU par défaut afin de rester raisonnable en
taille. L'index des roues peut être remplacé au build via
`JIN_TORCH_INDEX_URL` pour une future machine GPU qualifiée.

Avant promotion, un futur incrément doit encore fournir :

1. les métriques exact-match/F1 par champ et par famille documentaire ;
2. un registre d'artefacts candidat/shadow/principal ;
3. la comparaison au modèle actif et le rollback.

## API principale

```text
GET    /studio/status
GET    /studio/field-catalog
POST   /studio/documents
GET    /studio/documents
GET    /studio/documents/{id}
GET    /studio/documents/{id}/pdf
PUT    /studio/documents/{id}/annotation
GET    /studio/documents/{id}/annotations
POST   /studio/datasets
GET    /studio/datasets
POST   /studio/training-runs
GET    /studio/training-runs
```

## Intégration future avec GenieCommande

GenieCommande ne doit recevoir aucune interface technique supplémentaire. Son
backend pourra créer un document JIN Studio en envoyant le PDF, l'extraction
JIN originale et le résultat métier corrigé. L'interface GenieCommande
affichera uniquement « Ouvrir dans JIN Studio ».

Cette liaison devra utiliser une session courte et signée plutôt que transmettre
le token permanent au navigateur. Elle constitue un incrément séparé afin de
ne pas fragiliser le parcours ADV existant.
