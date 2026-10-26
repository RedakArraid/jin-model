# JIN Studio — annotations et datasets

JIN Studio sépare les activités de construction du modèle du parcours métier
de GenieCommande. L'interface est disponible sur `/studio.html` et son API sous
`/studio` (donc `/api/studio` derrière le proxy Nginx).

## Capacités du premier incrément

- dépôt d'un PDF puis extraction par le runtime JIN actif ;
- file de documents à annoter ;
- préremplissage des champs canoniques reconnus ;
- statuts par champ : correct, corrigé, absent, ambigu ou à vérifier ;
- dessin d'une zone normalisée sur chaque page du PDF, avec relecture automatique
  du texte natif puis OCR local de secours pour les scans ;
- synchronisation immédiate de la valeur relue dans l'aperçu JSON ;
- zoom jusqu'à 400 %, cadrage sur le champ actif et agrandissement visuel des
  rectangles sans modifier leurs coordonnées normalisées ;
- ajout et retrait de champs, dont SIREN/SIRET génériques ou rattachés à un rôle ;
- choix assisté du type de document, avec une valeur libre via « Autre » ;
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
JIN_STUDIO_SERVICE_TOKEN_FILE=/app/data/studio/service-token
JIN_STUDIO_MCP_TOKEN=<secret-mcp-optionnel>
JIN_STUDIO_MCP_ALLOW_VALIDATION=0
JIN_STUDIO_MCP_ALLOWED_HOSTS=mcp,mcp:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*
JIN_STUDIO_MCP_ALLOWED_ORIGINS=
JIN_WEB_BIND=127.0.0.1
JIN_WEB_PORT=8080
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

`JIN_STUDIO_SERVICE_TOKEN` est réservé aux appels machine-à-machine. Lorsqu'il
est vide sous Compose, l'API et le serveur MCP créent ensemble un secret
aléatoire dans `JIN_STUDIO_SERVICE_TOKEN_FILE`. Le fichier partagé évite de
mettre le secret dans Git ou dans `docker-compose.yml`. `JIN_STUDIO_TOKEN`, puis
`JIN_FEEDBACK_TOKEN`, restent des alias temporaires de compatibilité pour les
anciens clients techniques ; une nouvelle installation doit utiliser le nom
explicite `JIN_STUDIO_SERVICE_TOKEN` ou le fichier généré.

Le jeton technique permet d'annoter, de créer des datasets et de mettre des
entraînements en file, mais jamais d'administrer les comptes. Le jeton MCP peut
être séparé avec `JIN_STUDIO_MCP_TOKEN`; sinon le jeton Studio partagé est
utilisé. Une rotation du secret révoque immédiatement les clients distants.

## Accès distant par API et MCP

Compose reste volontairement lié à `127.0.0.1` par défaut. Pour une machine du
réseau interne, renseigner par exemple `JIN_WEB_BIND=0.0.0.0` dans `.env`, puis
reconstruire les trois services :

```powershell
docker compose up -d --build api mcp web
```

Ne pas publier directement ce port sur Internet. La cible recommandée est un
VPN ou un pare-feu d'entreprise, complété par un reverse proxy HTTPS. Le jeton
Bearer donne accès aux données d'annotation : il doit rester uniquement dans un
coffre à secrets ou dans la configuration protégée du client MCP.

Le secret généré peut être lu et renouvelé depuis le conteneur API :

```powershell
docker compose exec api python -m jin_runtime.studio_admin show-service-token
docker compose exec api python -m jin_runtime.studio_admin rotate-service-token --confirm
```

Un administrateur connecté peut aussi ouvrir **Utilisateurs**, puis
**Accès distant API / MCP** et cliquer sur **Générer / remplacer le jeton**.
Le nouveau secret n'est affiché qu'une fois et sa création révoque
immédiatement l'ancien pour l'API et pour MCP lorsque
`JIN_STUDIO_MCP_TOKEN` n'est pas défini. Cette opération est protégée par la session ADMIN et
le contrôle CSRF, puis inscrite dans le journal d'activité. Même si le jeton
actif vient initialement de `JIN_STUDIO_SERVICE_TOKEN`, le bouton reste actif
dès que `JIN_STUDIO_SERVICE_TOKEN_FILE` est configuré. La génération crée alors
un jeton géré par Studio dans ce fichier partagé ; il devient immédiatement
prioritaire pour l'API et pour MCP lorsque celui-ci partage le jeton Studio.

Après rotation, mettre à jour tous les clients. Les deux façades utilisent :

- API REST : `https://<hôte>/api/studio` ;
- MCP Streamable HTTP : `https://<hôte>/mcp` ;
- authentification : `Authorization: Bearer <secret>`.

Exemple de lecture REST :

```powershell
$headers = @{ Authorization = "Bearer $env:JIN_STUDIO_REMOTE_TOKEN" }
Invoke-RestMethod "https://jin.exemple/api/studio/status" -Headers $headers
Invoke-RestMethod "https://jin.exemple/api/studio/documents?limit=100" -Headers $headers
```

Exemple de création du dataset puis de mise en file d'un candidat :

```powershell
$dataset = Invoke-RestMethod `
  "https://jin.exemple/api/studio/datasets" `
  -Method Post -Headers $headers -ContentType "application/json" `
  -Body (@{ name = "commandes-revues"; documentIds = $documentIds } | ConvertTo-Json)

Invoke-RestMethod `
  "https://jin.exemple/api/studio/training-runs" `
  -Method Post -Headers $headers -ContentType "application/json" `
  -Body (@{ datasetId = $dataset.datasetId; modelFamily = "layoutlmv3" } | ConvertTo-Json)
```

L'écriture d'une annotation utilise
`PUT /api/studio/documents/{documentId}/annotation` avec le contrat v2 décrit
plus bas. Un état `DRAFT` enregistre une révision sans la rendre entraînable ;
`HUMAN_VALIDATED` est réservé à une révision réellement relue. Chaque mutation
REST ou MCP est ajoutée au journal d'audit.

L'API REST permet aussi d'importer un PDF, d'enregistrer une annotation, de
créer un dataset et de mettre un entraînement en file. MCP expose les outils :

- `get_workflow_status`, `list_field_catalog` ;
- `list_documents`, `get_document`, `save_annotation_draft` ;
- `validate_annotation` ;
- `list_datasets`, `create_versioned_dataset` ;
- `list_training_runs`, `queue_candidate_training`.

Un client MCP doit être configuré en transport **Streamable HTTP**, URL
`https://<hôte>/mcp`, avec le même en-tête Bearer. Les brouillons distants sont
autorisés. `validate_annotation` est désactivé par défaut, car une validation
fabrique de la vérité terrain. Il ne devient disponible qu'avec
`JIN_STUDIO_MCP_ALLOW_VALIDATION=1` et exige le nom du relecteur humain ainsi
qu'une attestation explicite. Même activé, chaque appel est journalisé.

`JIN_STUDIO_MCP_ALLOWED_HOSTS` et `JIN_STUDIO_MCP_ALLOWED_ORIGINS` protègent le
transport contre le DNS rebinding. Si le MCP est appelé directement sans le
proxy Nginx, ajouter précisément le nom DNS et l'origine HTTPS utilisés ; ne
pas remplacer ces listes par un joker.

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

## Campagne de préannotation assistée

Une campagne peut partir d'une liste JSON de chemins PDF relatifs à un dossier
source. La sélection doit être variée par famille client, nombre de pages et
nature du PDF (texte natif ou scan). Le générateur appelle le runtime JIN,
conserve la réponse complète, localise les valeurs dans le PDF et produit des
zones normalisées. Les valeurs non localisées restent explicitement ambiguës :
elles ne sont jamais supprimées silencieusement.

```powershell
python scripts/select_varied_annotation_sample.py `
  "C:\chemin\vers\Commandes_ESKER\tout" `
  --count 100 `
  --seed campagne-001 `
  --output data/annotations/ma-campagne/sample_files.json

python scripts/build_annotation_review_campaign.py `
  "C:\chemin\vers\Commandes_ESKER\tout" `
  --sample-file data/annotations/ma-campagne/sample_files.json `
  --output-dir data/annotations/ma-campagne/review `
  --api http://localhost:8080/api/extract

python scripts/import_annotation_campaign_to_studio.py `
  data/annotations/ma-campagne/review `
  "C:\chemin\vers\Commandes_ESKER\tout" `
  --studio-dir data/studio `
  --campaign-id ma-campagne
```

L'import est reprenable et idempotent : une interruption ne duplique ni le PDF
ni sa révision. Les PDF sont copiés uniquement dans le stockage local ignoré
par Git. Chaque document importé reste `DRAFT`, chaque zone trouvée reste
`UNREVIEWED`, et chaque valeur sans zone reste `AMBIGUOUS`.

La revue humaine se fait dans `/studio.html` :

1. contrôler le PDF entier, y compris les pages annexes ;
2. confirmer, corriger, marquer absent ou ambigu chaque champ ;
3. redessiner la zone lorsqu'elle est manquante ou incorrecte ;
4. valider le document avec le compte nominatif du relecteur.

Quand une nouvelle zone est tracée, Studio relit sa valeur et passe le champ à
`CONFIRMED` si elle correspond à JIN, sinon à `CORRECTED`. L'aperçu JSON reflète
immédiatement cette modification ; `Ctrl+S` enregistre le brouillon. Les boutons
de zoom ne changent jamais la boîte `[x0,y0,x1,y1]` stockée sur 1000 : ils ne
font qu'agrandir le PDF et tous ses rectangles à l'écran.

Les identifiants `SIREN`, `SIRET`, `BUYER_SIREN`, `BUYER_SIRET`,
`SUPPLIER_SIREN` et `SUPPLIER_SIRET` sont annotables. Le runtime ne demande pas
d'annoter une TVA qui n'est pas imprimée : il extrait d'abord le SIREN/SIRET,
vérifie son checksum, puis calcule la TVA française dans la couche métier. Le
JSON propre publie le SIREN/SIRET sous la partie concernée et conserve sur la
TVA calculée la méthode, la source et le statut `DERIVED_FROM_VALID_SIREN` ou
`DERIVED_FROM_VALID_SIRET`.

Seules les révisions `HUMAN_VALIDATED` peuvent entrer dans un dataset. Une
préannotation Codex/JIN, même très confiante, n'est donc jamais une vérité
terrain avant cette validation.

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

Chaque entrée du catalogue de champs contient une aide `help`, affichée dans
l'annotateur sous **Que faut-il encadrer ?** et également exposée par REST et
MCP. La règle par défaut consiste à encadrer uniquement la valeur imprimée,
sans son libellé et sans les valeurs voisines. Par exemple, une référence
article ne doit jamais couvrir la description complète ou toute la ligne.
Le catalogue fournit également un `defaultPath`. Quand l'annotateur change le
type d'un champ, son chemin canonique est recalculé automatiquement tout en
conservant, lorsque c'est pertinent, l'indice de ligne, d'adresse, de frais ou
d'identifiant fiscal. Le bouton **＋ Ajouter un champ** se trouve sous le dernier
champ afin que l'ajout reste accessible à la fin de la revue.

Le type de document constitue un cas particulier : sa valeur est normalisée
(`purchase_order`, `invoice`, etc.) et n'existe pas nécessairement telle quelle
sur la page. Il faut donc encadrer le titre qui apporte la preuve textuelle la
plus explicite, par exemple **BON DE COMMANDE** ou **COMMANDE**, puis choisir le
type normalisé dans la liste. Il ne faut encadrer ni toute la page, ni un logo,
ni une adresse pour justifier le type.

Une validation est refusée si un champ reste `UNREVIEWED`. Un dataset refuse
tout document dont la dernière révision n'est pas `HUMAN_VALIDATED`.

## Dataset et entraînement

L'enchaînement après annotation est volontairement explicite :

1. **Enregistrer le brouillon** conserve le travail, mais n'alimente aucun
   dataset et ne change aucun modèle.
2. **Valider pour l'apprentissage** est un bouton par document. Tous les champs
   doivent avoir été confirmés, corrigés, déclarés absents ou ambigus. Cette
   action produit une révision `HUMAN_VALIDATED` attribuée au relecteur.
3. Dans **Datasets**, **Créer depuis les documents validés** fige une version
   immuable des révisions sélectionnées. Une correction ultérieure n'altère
   jamais rétroactivement cette version : il faut créer une nouvelle version.
4. Quand le dataset est `READY`, le bouton **Entraîner un candidat** place une
   exécution dans la file. Le conteneur `trainer` doit être démarré pour la
   consommer.
5. Un entraînement réussi produit seulement un **artefact candidat**. Il ne
   remplace pas le runtime JIN actif. Il doit encore être évalué sur un jeu de
   test indépendant, comparé au modèle actif, puis promu explicitement avec une
   possibilité de rollback.

La réponse courte à « faut-il appuyer sur un bouton ? » est donc **oui** :
validation par document, création du dataset, puis entraînement du candidat.
Il n'existe aucune prise en compte automatique à la simple sauvegarde d'une
annotation. Et, dans la version actuelle, l'évaluation/promotion vers le modèle
actif n'est pas encore automatisée : un statut `SUCCEEDED` signifie que le
candidat a été entraîné, pas qu'il est déployé.

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

La promotion automatique reste bloquée tant qu'un incrément ne fournit pas :

1. les métriques exact-match/F1 par champ et par famille documentaire ;
2. un registre d'artefacts candidat/shadow/principal ;
3. la comparaison au modèle actif et le rollback.

## API principale

Les routes ci-dessous acceptent soit la session web et son jeton CSRF, soit le
Bearer machine pour les opérations non administratives. Derrière Nginx, leur
préfixe public est `/api/studio`.

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
POST   /studio/documents/{id}/region-text
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
