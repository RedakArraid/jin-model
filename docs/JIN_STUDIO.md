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
- affichage ou masquage des autres rectangles, sans jamais masquer le champ actif ;
- ajout et retrait de champs, dont SIREN/SIRET génériques ou rattachés à un rôle ;
- masques d'annotation nommés et versionnés pour réutiliser les champs et leurs
  positions sur des documents de même mise en page ;
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

Ce même bloc **Accès distant API / MCP** contient un générateur de
configuration. Renseigner l'adresse HTTPS ou VPN de la VM, puis générer un
jeton ou coller un jeton existant dans le champ prévu. Studio produit alors :

- un bloc `~/.codex/config.toml` compatible avec le transport MCP Streamable
  HTTP de Codex ;
- au choix, la variante JSON MCP générique ;
- la base URL et l'en-tête Bearer de l'API REST.

Les configurations se mettent à jour immédiatement quand l'URL, le jeton ou le
format change. Pour un accès distant, remplacer impérativement l'adresse
locale proposée par l'adresse réellement joignable via le VPN ou le reverse
proxy HTTPS. Le jeton collé n'est pas envoyé au serveur par ce générateur,
mais il apparaît en clair dans la configuration produite : le fichier copié
doit donc être protégé.

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

Le mode de journal SQLite (`JIN_STUDIO_SQLITE_JOURNAL_MODE`, `DELETE` par
défaut) est appliqué une seule fois pendant l'initialisation sérialisée du
schéma. Les connexions API, MCP et worker ne tentent donc plus de reprendre un
verrou exclusif à chaque requête.

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

La conservation hors VM et les exercices de reprise sont décrits dans
[Sauvegarde et restauration](BACKUP_AND_RESTORE.md). La sauvegarde couvre ce
stockage, la base SQLite, les modèles appris et le feedback humain.

## Campagne de préannotation assistée

Une campagne peut partir d'une liste JSON de chemins PDF relatifs à un dossier
source. La sélection doit être variée par famille client, nombre de pages et
nature du PDF (texte natif ou scan). Le générateur appelle le runtime JIN,
conserve la réponse complète, localise les valeurs dans le PDF et produit des
zones normalisées. Les valeurs non localisées restent explicitement ambiguës :
elles ne sont jamais supprimées silencieusement.

Un PDF déposé directement dans Studio suit désormais exactement le même
parcours : JIN-model extrait d'abord les valeurs, puis le serveur les relocalise
dans le PDF et enregistre une première révision machine avec des cadres sur
1000. Le bouton **Relire avec JIN-model** reconstruit aussi ces cadres à partir
du PDF complet. Cette relecture reste locale tant que le brouillon n'est pas
enregistré. Studio sélectionne en priorité le premier champ possédant un cadre,
ce qui garantit sa visibilité même lorsque **Afficher les autres cadres** est
désactivé.

Une campagne ciblée ne doit pas être choisie uniquement par récence. Pour un
client multi-sites tel qu'ISERBA, il faut répartir l'échantillon par code de
site et par signature d'adresse de livraison afin qu'une adresse fréquente ne
devienne pas une fausse constante du modèle.

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

Pour réexécuter le runtime courant sur les seuls brouillons machine encore
intacts, sans toucher aux validations ni aux brouillons déjà modifiés par un
humain :

```powershell
docker compose exec -T api python /app/scripts/reprocess_studio_documents.py `
  --studio-dir /app/data/studio `
  --endpoint http://localhost:8000/extract?view=clean `
  --confirm REPROCESS-STUDIO-DOCUMENTS
```

La commande sauvegarde SQLite avant traitement, prépare toutes les nouvelles
extractions hors transaction, puis ne les publie que si le lot est complet.
Les documents `HUMAN_VALIDATED` et les brouillons portant une relecture ou une
correction humaine sont systématiquement ignorés et consignés dans le rapport.

Pour préparer une annotation ciblée par client, les noms déjà présents dans
Studio peuvent recevoir un préfixe stable tel que `REXEL_...` ou
`IZI_CONFORT_...` :

```powershell
# Aperçu sans écriture
python scripts/prefix_studio_documents_with_client.py --studio-root data/studio

# Sauvegarde automatique de SQLite, puis application
python scripts/prefix_studio_documents_with_client.py --studio-root data/studio --apply
```

Le répertoire client de `sourceRelativePath` est prioritaire ; l'acheteur
extrait sert uniquement de repli pour un document non classé. La migration ne
modifie ni le `documentId`, ni le PDF interne, ni les annotations. Le nouveau
nom est celui affiché, recherché et proposé au téléchargement. L'ancien nom et
la provenance du préfixe restent consignés dans les métadonnées source.

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
La case **Afficher les autres cadres** permet de masquer temporairement les
autres annotations pour travailler sans surcharge visuelle. Le cadre du champ
sélectionné reste affiché et le choix est conservé par le navigateur.

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

## Contrat d'annotation v3

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

Chaque entrée du catalogue de champs contient une aide `help` et un exemple
concret `example`, affichés dans l'annotateur sous **Que faut-il encadrer ?** et
également exposés par REST et MCP. L'exemple montre une courte portion de texte
source puis la valeur exacte à encadrer. La règle par défaut consiste à
encadrer uniquement la valeur imprimée, sans son libellé et sans les valeurs
voisines. Par exemple, une référence article ne doit jamais couvrir la
description complète ou toute la ligne.
Le catalogue fournit également un `defaultPath`. Quand l'annotateur change le
type d'un champ, son chemin canonique est recalculé automatiquement tout en
conservant, lorsque c'est pertinent, l'indice de ligne, d'adresse, de frais ou
d'identifiant fiscal. Le bouton **＋ Ajouter un champ** est disponible dans la
barre de revue, juste avant **Confirmer les non revus**, et sous le dernier
champ. Les flèches ↑/↓ déplacent visuellement un champ à l'intérieur de son
compartiment ; pour une ligne de commande, le déplacement reste volontairement
limité à la ligne concernée. L'ordre sauvegardé suit l'ordre affiché, tout en
conservant le bloc des lignes de commande à la fin.

Le bouton respecte également le compartiment affiché : un champ ajouté depuis
**Pied de page** reste dans ce compartiment après l'enregistrement, le
rechargement ou l'application d'un masque. Dans **Lignes de commande**, il
ajoute un champ à la ligne sélectionnée ou crée la première ligne si nécessaire.

La barre d'actions affiche en permanence le résultat d'un enregistrement ou
d'une validation. Si des champs restent à vérifier, JIN Studio indique leur
nombre, sélectionne le premier blocage et le ramène automatiquement à l'écran.
Un statut **Corrigé** sans valeur corrigée ou **Confirmé** sans valeur extraite
est signalé avant l'appel serveur.

### Adresses et rôles métier

Chaque composant d'adresse entraînable doit être rattaché à la partie qu'il
décrit. Par exemple :

- **Livraison — numéro de voie** correspond à
  `order.addresses.ship_to.selected.components.house_number` ;
- **Acheteur — numéro de voie** correspond à
  `order.addresses.buyer.selected.components.house_number` ;
- **Fournisseur — numéro de voie** correspond à
  `order.addresses.supplier.selected.components.house_number`.

Les champs sans rôle de la famille historique `ADDRESS_*` ne font plus partie
du catalogue V3. Les anciens brouillons ont été sauvegardés puis remplacés par
des préannotations V3 : l'annotateur ne doit jamais choisir entre deux chemins
qui décrivent le même composant postal.

Les préannotations issues de `verification`, `matched_components`, des
suggestions BAN, de `quality`, de `reference_data` ou des `alternatives` ne sont
pas des preuves imprimées dans le PDF. Elles sont ignorées. Studio annote
uniquement le bloc `selected` d'un rôle ; un rôle `AMBIGUOUS` doit être arbitré
avant de devenir une vérité d'apprentissage.

Une même région ne peut pas porter deux labels différents dans le modèle de
classification de tokens. La validation et la création du dataset refusent
donc les zones conflictuelles. La voie complète historique (`*_STREET`) reste
lisible, mais n'est plus proposée si les composants atomiques numéro, suffixe,
type et nom de voie sont annotés séparément.

Enfin, un champ **Confirmé** ou **Corrigé** doit toujours posséder une page et
un rectangle source. Une valeur calculée, enrichie ou seulement recopiée dans
le JSON ne doit pas être encadrée si elle n'est pas imprimée dans le PDF : le
champ doit être marqué **Absent**. Deux champs distincts ne peuvent pas non
plus réutiliser exactement le même rectangle. Ces règles empêchent notamment
un pays déduit du code postal d'entrer silencieusement dans le dataset.

### Masques d'annotation

Un masque accélère la revue de documents récurrents sans transformer une
ancienne commande en vérité terrain pour la suivante. Après avoir positionné
les cadres d'un document, **Enregistrer comme masque** demande un nom et crée
une version immuable. Le masque conserve uniquement :

- le label du champ et son chemin JSON canonique ;
- le numéro de page et la boîte normalisée sur 1000 ;
- le type de document et une signature géométrique sans contenu ;
- le document source, l'auteur, la version et la date de création.

Le périmètre d'un masque est volontairement limité aux **en-têtes et pieds de
page**. Tous les champs `order.line_items[index]` sont exclus lors de la
création, y compris via API ou MCP. Lors de l'application dans l'interface,
les lignes relues par JIN sur le document cible sont conservées : elles ne sont
ni copiées depuis la commande source, ni remplacées par le masque.

Les valeurs extraites, corrigées et les commentaires ne sont jamais copiés
dans le masque. Une nouvelle sauvegarde sous le même nom crée `v2`, `v3`, etc.
La liste courante présente seulement la dernière version de chaque nom ; les
versions précédentes restent accessibles par API ou MCP pour l'audit.

Les chemins des PDF sont conservés relativement au stockage Studio. Les anciens
documents qui contiennent encore un chemin absolu Windows ou Docker sont
résolus automatiquement à partir de leur identifiant ; un déplacement entre la
VM, l'hôte et les conteneurs ne rend donc pas le document introuvable.

Sur un autre PDF, **Appliquer le masque** remplace la préannotation affichée,
compare d'abord le PDF source au PDF cible et recale les cadres par translation.
Le recalage V3 utilise uniquement des mots alphabétiques uniques comme ancres,
exige au moins deux ancres concordantes et un consensus de 75 %, limite le
décalage accepté et traite séparément les zones d'en-tête et de pied de page.
Le texte des ancres n'est jamais enregistré dans le masque. Si le décalage
n'est pas démontré — notamment sur un PDF image sans texte natif — JIN conserve
les coordonnées du masque au lieu d'inventer une position.

Studio relit ensuite le texte natif ou OCR à l'intérieur de chaque cadre, puis
recrée tous les champs avec le statut `UNREVIEWED`. La réponse API expose le
détail non sensible du recalage dans `alignment` : stratégie, confiance,
nombre d'ancres et translations `dx`/`dy`, sans texte documentaire. Rien n'est
enregistré tant que
l'annotateur n'utilise pas **Enregistrer le brouillon** ou **Valider pour
l'apprentissage**. Studio avertit avant application et après relecture lorsque
le type, le nombre de pages, l'orientation ou le ratio des pages diffèrent.

Les anciens masques V1/V2 restent consultables et bénéficient aussi du recalage
si leur document source est encore disponible. Les éventuels champs de ligne
historiques sont toujours filtrés côté serveur lors de l'application.

Les opérations sont disponibles à distance avec
`GET/POST /api/studio/annotation-masks` et
`POST /api/studio/documents/{document_id}/annotation-masks/{mask_id}/apply`.
Les outils MCP correspondants sont `list_annotation_masks`,
`create_annotation_mask` et `apply_annotation_mask`. La création et
l'application sont inscrites dans le journal d'audit.

### Lignes de commande

Les lignes sont des groupes répétables et non une suite de champs indépendants.
Studio affiche donc explicitement **Ligne de commande 1**, **Ligne de commande
2**, etc. Le numéro visible est humain (il commence à 1), tandis que le chemin
JSON reste un tableau indexé à partir de zéro :

- ligne visible 1 : `order.line_items[0]` ;
- ligne visible 2 : `order.line_items[1]` ;
- ligne visible 3 : `order.line_items[2]`.

Le bouton **Ajouter une ligne de commande** crée ensemble les six champs de
base : référence, description, quantité, unité, prix unitaire et montant. Le
bouton **+ Champ de ligne** ajoute ensuite le prochain champ manquant, notamment
le numéro imprimé de ligne ou sa date de livraison. **Retirer la ligne** supprime
le groupe complet et réindexe automatiquement les groupes suivants sans toucher
aux valeurs ni aux rectangles.

Le bloc complet des lignes de commande reste toujours en dernière position dans
l'annotateur et dans l'annotation enregistrée. Un champ général ajouté après
coup est automatiquement inséré au-dessus de ce bloc, sans déplacer les champs
à l'intérieur d'une ligne ni modifier leurs valeurs.

La préannotation ne conserve qu'une occurrence sémantique par ligne : par
exemple `article_number` et `material_number` identiques ne créent plus deux
champs « Référence article », et `unit_price` / `net_unit_price` identiques ne
dupliquent plus le prix. L'enregistrement REST ou MCP refuse un label de ligne
sans `line_items[index]` ainsi qu'une suite comportant un trou d'index. Cette
validation empêche une deuxième ligne de retomber silencieusement dans
`line_items[0]`.

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
5. Studio réserve dès la création du dataset des documents entiers en
   entraînement/validation/test (70/15/15), sans mélanger les pages d'un même
   PDF. Après le fine-tuning, le worker évalue le candidat sur le test figé et
   le compare aux valeurs de l'extraction JIN d'origine.
6. Un entraînement réussi produit un **artefact candidat** et un rapport de
   benchmark par champ. Il ne remplace pas le runtime JIN actif. La promotion
   reste une décision explicite, suivie d'une campagne shadow et d'un rollback
   possible.

La réponse courte à « faut-il appuyer sur un bouton ? » est donc **oui** :
validation par document, création du dataset, puis entraînement du candidat.
Il n'existe aucune prise en compte automatique à la simple sauvegarde d'une
annotation. L'évaluation technique est maintenant exécutée automatiquement à
la fin de l'entraînement, mais la promotion vers le modèle actif ne l'est pas :
un statut `SUCCEEDED` signifie que le candidat a été entraîné et benchmarké,
pas qu'il est déployé.

Le dataset est `READY` à partir de 20 documents validés contenant au moins une
zone spatiale. Ce seuil est une garde minimale, pas une preuve que le corpus est
suffisamment diversifié.

L'API place les entraînements dans une file SQLite. Le conteneur `trainer`
réclame une exécution, prépare les pages OCR, entraîne LayoutLMv3 puis exécute
le benchmark du holdout. Il publie un heartbeat et conserve le journal,
l'artefact, `split_manifest.json` et `benchmark.json` sous
`training/<run-id>/`. Le rapport affiche l'exact-match du JIN actif et du
candidat, leur écart, le F1 hors classe `O`, la couverture OCR et les résultats
par label. Une exécution abandonnée est remise en file après le délai configuré.
L'API de production n'importe pas PyTorch et ne lance jamais ces calculs.

Le worker est volontairement derrière le profil Docker `training` :

```powershell
docker compose --profile training up -d --build trainer
```

Sans ce profil, les demandes restent simplement au statut `QUEUED`. Le premier
entraînement peut télécharger le modèle de base dans `data/huggingface`; en
environnement sans accès réseau, ce cache doit être alimenté à l'avance. Les
commandes stockées ne sont jamais exécutées dans un shell : le worker n'accepte
que les trois scripts de préparation, fine-tuning et benchmark LayoutLMv3 et
vérifie les chemins de données.
L'image locale installe PyTorch CPU par défaut afin de rester raisonnable en
taille. L'index des roues peut être remplacé au build via
`JIN_TORCH_INDEX_URL` pour une future machine GPU qualifiée.

Le rapport automatique constitue la première porte de qualité. Un holdout de
moins de 10 documents reçoit le verdict `INSUFFICIENT_HOLDOUT`, même lorsque ses
scores sont bons. La promotion automatique reste bloquée tant qu'un incrément
ne fournit pas encore :

1. les métriques par famille documentaire, client, qualité de scan et latence ;
2. un registre d'artefacts candidat/shadow/principal ;
3. une campagne shadow et un rollback testé de bout en bout.

L'état réel du corpus Studio et les portes à franchir avant une promotion sont
maintenus dans [Préparation à la production](PRODUCTION_READINESS.md).

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

## Déplacement visuel de plusieurs champs

Chaque carte d'annotation possède une case **Sélectionner**. Les boutons
**Monter la sélection** et **Descendre la sélection** déplacent ensemble les
champs cochés tout en conservant leur ordre. En montant, les champs se regroupent
sans espace auprès du champ sélectionné le plus haut, puis le bloc monte d'un
cran. En descendant, ils se regroupent auprès du plus bas, puis le bloc descend.
Les limites structurelles restent
strictes : un champ d'en-tête ne traverse pas le pied de page et les champs
d'une ligne de commande restent dans cette ligne. La sélection multiple est
visuelle et temporaire ; seul le nouvel ordre des champs est enregistré.

## Espace d'annotation restructuré

L'écran d'annotation est organisé en trois zones synchronisées : la file des
documents, le PDF et l'inspecteur de champs. L'inspecteur propose deux modes :

- **Annotation** garde les champs inactifs compacts et ouvre complètement le
  champ courant ;
- **Relecture** met en avant les champs non vérifiés, ambigus ou incohérents,
  avec un filtre et un bouton **Prochain problème**.

Les champs sont aussi accessibles par groupes métier : informations générales,
client, fournisseur, livraison, facturation, payeur, expéditeur, totaux,
identifiants légaux et lignes de commande. Cette organisation est une vue de
travail ; elle ne duplique aucun champ dans le contrat JSON.

Les lignes de commande sont présentées sous forme de tableau. Une cellule
correspond à un champ canonique d'une ligne et ouvre son inspecteur ainsi que
son rectangle dans le PDF. Les index `line_items[n]` restent continus et tout le
bloc des lignes reste après les en-têtes et pieds de page.

Le bouton **Ajouter un champ** ouvre un sélecteur explicite qui affiche l'aide
et un exemple avant création. Le compartiment en-tête ou pied de page choisi est
enregistré dans l'annotation. Un champ sémantique déjà présent ne peut pas être
ajouté une seconde fois.

### Sécurité de saisie et validation

JIN Studio conserve jusqu'à 80 états locaux pour **Annuler/Rétablir**. Une
copie de secours du brouillon non enregistré est également placée dans le
stockage local du navigateur ; au prochain chargement du même PDF, l'utilisateur
choisit de la restaurer ou de l'ignorer. Cette copie ne remplace jamais une
sauvegarde serveur.

**Valider pour l'apprentissage** ouvre d'abord un contrôle préalable. Il liste
les champs non relus, les valeurs vides incompatibles avec leur statut, les
problèmes de structure des lignes et les rectangles source conflictuels. Chaque
blocage associé à un champ ramène directement à celui-ci. Le bouton final reste
désactivé tant que ces contrôles ne sont pas satisfaits.

Les raccourcis principaux sont : `Entrée` pour confirmer et avancer, `A` pour
Absent, `M` pour Ambigu, `Alt+↑/↓` pour déplacer, `Ctrl+Z/Y` pour
annuler/rétablir et `Ctrl+S` pour enregistrer le brouillon sur le serveur.

Les masques sont regroupés dans **Modèles et masques**. Ils conservent seulement
les champs et rectangles stables d'en-tête ou de pied de page ; les lignes de
commande sont toujours relues sur le document cible.
