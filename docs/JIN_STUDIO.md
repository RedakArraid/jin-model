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
- comptes nominatifs, sessions sécurisées et droits par rôle ;
- validation humaine explicitement attribuée à l'utilisateur connecté ;
- journal d'audit des connexions et des opérations sensibles ;
- création de datasets versionnés depuis les seules annotations validées ;
- export d'un manifeste et de régions compatibles avec le pipeline layout ;
- mise en file et exécution isolée d'un entraînement candidat.

Le circuit historique `POST /feedback` reste disponible pour la mémoire
statistique d'adresses. JIN Studio utilise un contrat général v2 indépendant et
ne migre aucune ancienne correction implicitement.

## Configuration

```text
JIN_STUDIO_SERVICE_TOKEN=<secret-machine-optionnel>
JIN_STUDIO_PASSWORD_MIN_LENGTH=8
JIN_STUDIO_SESSION_HOURS=12
JIN_STUDIO_COOKIE_SECURE=auto
JIN_STUDIO_DIR=/app/data/studio
JIN_STUDIO_SHARED_STORAGE=1
JIN_STUDIO_MIN_TRAINING_DOCUMENTS=20
JIN_TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
```

Les utilisateurs humains ne manipulent plus de jeton. Après authentification,
le serveur crée une session aléatoire conservée dans SQLite et remet au
navigateur un cookie `HttpOnly`, `SameSite=Strict`. Les mutations exigent aussi
un en-tête CSRF propre à la session. En HTTPS, `JIN_STUDIO_COOKIE_SECURE=auto`
active automatiquement l'attribut `Secure` grâce à `X-Forwarded-Proto`.

`JIN_STUDIO_SERVICE_TOKEN` est facultatif et réservé aux appels
machine-à-machine, par exemple l'import futur depuis GenieCommande. Il ne donne
jamais accès à l'administration des utilisateurs. `JIN_STUDIO_TOKEN`, puis
`JIN_FEEDBACK_TOKEN`, restent des alias temporaires de compatibilité pour les
anciens clients techniques ; une nouvelle installation doit utiliser le nom
explicite `JIN_STUDIO_SERVICE_TOKEN`.

## Premier administrateur

La création initiale se fait dans un terminal interactif afin que le mot de
passe n'apparaisse ni dans Git, ni dans l'historique du shell :

```powershell
docker compose exec api python -m jin_runtime.studio_admin create-user `
  --username khadara `
  --display-name "Khadara" `
  --role ADMIN
```

La commande demande deux fois le mot de passe. Les comptes suivants sont créés
depuis l'onglet **Utilisateurs** de JIN Studio. Les mots de passe sont hachés
avec `scrypt`; cinq échecs successifs verrouillent le compte pendant quinze
minutes. Modifier un mot de passe révoque toutes les sessions du compte.

## Rôles

| Rôle | Capacités |
|---|---|
| Lecture | Consulter documents, datasets et entraînements |
| Annotation | Importer et enregistrer des brouillons |
| Validation | Annoter et valider les données d'apprentissage |
| Entraînement | Créer les datasets et lancer les entraînements |
| Administration | Gérer les comptes, les rôles et le journal d'audit |

Un utilisateur peut cumuler plusieurs rôles. Le dernier administrateur actif
ne peut être désactivé ou rétrogradé. Les droits sont relus à chaque requête :
une désactivation est donc effective immédiatement.

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
GET    /studio/auth/config
POST   /studio/auth/login
GET    /studio/auth/me
POST   /studio/auth/logout
POST   /studio/auth/change-password
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
GET    /studio/users
POST   /studio/users
PATCH  /studio/users/{id}
POST   /studio/users/{id}/password
GET    /studio/audit-events
```

## Intégration future avec GenieCommande

GenieCommande ne doit recevoir aucune interface technique supplémentaire. Son
backend pourra créer un document JIN Studio en envoyant le PDF, l'extraction
JIN originale et le résultat métier corrigé. L'interface GenieCommande
affichera uniquement « Ouvrir dans JIN Studio ».

Cette liaison utilisera `JIN_STUDIO_SERVICE_TOKEN` côté serveurs. Aucun secret
technique ne devra être transmis au navigateur GenieCommande.
