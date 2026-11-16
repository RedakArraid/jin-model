# Sauvegarde et restauration de JIN Model

## Ce qui est protégé

Pour transférer seulement les PDF et leurs annotations entre deux instances
sans accès SSH/SCP, voir le [transfert par MCP](STUDIO_DOCUMENT_TRANSFER.md).
Il ne remplace pas la sauvegarde complète décrite ici : modèles, BAN, comptes,
masques et datasets restent hors de ce transfert documentaire.

Une sauvegarde standard contient toutes les données JIN qui ne peuvent pas être
reconstruites depuis Git ou depuis une source publique :

- les PDF déposés dans JIN Studio ;
- les annotations, leurs révisions et les datasets exportés ;
- les comptes utilisateurs, les rôles et le journal d'audit ;
- les exécutions d'entraînement enregistrées dans Studio ;
- les modèles statistiques appris sous `data/learning` ;
- le feedback humain sous `feedback`.
- le cœur JIN installé sous `model/`, car ce paquet est volontairement absent
  de Git et peut être nécessaire pour reconstruire l'image complète.

La copie de `studio.sqlite3` utilise l'API de sauvegarde SQLite. Elle reste donc
cohérente même si l'API est démarrée. Les fichiers sont empaquetés avec un
manifeste, leur taille et leur somme SHA-256. `verify` contrôle les sommes, les
archives, les chemins internes et `PRAGMA quick_check` sur SQLite.

Ne sont volontairement jamais copiés : `.env`, les secrets et le jeton de
service Studio. Celui-ci est régénéré après restauration. Les comptes et mots
de passe restent conservés, mais toutes les sessions actives sont révoquées à
la restauration.

La BAN sous `data/reference` est incluse par défaut : reconstruire un index
depuis une source plus récente ne reproduit pas nécessairement les mêmes
résultats. `--exclude-reference` permet une sauvegarde allégée explicite, sans
garantie de parité. Un répertoire de référence manquant fait échouer la
sauvegarde complète au lieu d'omettre silencieusement la BAN.

Les réglages fonctionnels non secrets (seuils, langues OCR, activation des
compléments, nombre minimal de documents d'entraînement et référence de
l'image) sont conservés dans `runtime-settings.env`, avec une liste blanche.
Ils sont restaurés dans `config/runtime-settings.env`. Les chemins hôtes,
réglages réseau, identifiants et jetons restent à configurer séparément.

## Test local avec Docker

Créer une sauvegarde sans arrêter JIN :

```bash
docker compose --profile ops run --rm --build backup
```

Le résultat apparaît sous `backups/jin-model-<date UTC>/`. Le dossier
`backups/` est exclu de Git.

Vérifier une sauvegarde existante :

```bash
docker compose --profile ops run --rm backup \
  verify /app/backups/jin-model-20261026T120000.000000Z
```

La même commande existe pour le Compose standalone :

```bash
docker compose -f docker-compose.standalone.yml --profile ops run --rm --build backup
```

Pour une sauvegarde exceptionnellement allégée :

```bash
python scripts/jin_backup.py create --exclude-reference
```

## Test de restauration isolé

Une restauration ne remplace jamais une donnée existante. Les cibles gérées
(`data/studio`, `data/learning`, `feedback`, `model`, `config`, et
`data/reference` si présente dans la sauvegarde) doivent être absentes ou vides et la confirmation littérale
est obligatoire :

```bash
python scripts/jin_backup.py restore backups/jin-model-<date> \
  --target-root /tmp/jin-restore-test \
  --confirm RESTORE_EMPTY_TARGET
```

Contrôler ensuite les PDF, les utilisateurs et les annotations dans cette copie
avant tout exercice sur la vraie installation.

## Externalisation hors de la VM

La cible recommandée est un conteneur Azure Blob **privé**, dans un compte de
stockage distinct ou protégé contre la suppression. Activer le versionnement,
la suppression réversible et une règle de rétention adaptée à la politique de
l'entreprise. La VM s'authentifie avec son identité managée : aucune clé Azure
n'est enregistrée dans ce dépôt.

Pré-requis Azure :

1. créer le conteneur privé `jin-model-backups` ;
2. attribuer à l'identité managée de la VM le rôle
   `Storage Blob Data Contributor` sur ce conteneur ;
3. installer Docker avec Compose >= 2.17, Python >= 3.12 et Azure CLI sur la VM ;
4. renseigner `/etc/jin-model-backup.env` à partir de
   `ops/systemd/jin-model-backup.env.example`.

Envoi manuel d'une sauvegarde déjà créée :

```bash
az login --identity
python scripts/jin_backup.py azure-upload backups/jin-model-<date>
```

Le script crée une archive de transport, l'envoie en HTTPS, inscrit son SHA-256
dans les métadonnées du blob puis vérifie la taille distante.

Pour automatiser chaque nuit, adapter `User` et `WorkingDirectory` si le dépôt
n'est pas installé sous `/opt/jin-model`, puis installer le timer fourni :

```bash
sudo cp ops/systemd/jin-model-backup.service /etc/systemd/system/
sudo cp ops/systemd/jin-model-backup.timer /etc/systemd/system/
sudo cp ops/systemd/jin-model-backup.env.example /etc/jin-model-backup.env
sudo systemctl daemon-reload
sudo systemctl enable --now jin-model-backup.timer
sudo systemctl start jin-model-backup.service
sudo journalctl -u jin-model-backup.service -n 100 --no-pager
```

Le démarrage manuel ci-dessus constitue le test initial obligatoire. Le timer
ne doit être considéré opérationnel qu'après présence du blob dans Azure.

## Reprise depuis Azure

Télécharger, contrôler puis décompresser une sauvegarde :

```bash
az login --identity
python scripts/jin_backup.py azure-download \
  production/jin-model-<date>.tar.gz \
  --destination-root /var/tmp/jin-recovery
python scripts/jin_backup.py verify /var/tmp/jin-recovery/jin-model-<date>
```

Pour une restauration réelle :

1. arrêter `api`, `mcp` et `trainer` ;
2. déplacer les répertoires actuels vers un répertoire de quarantaine daté ;
3. restaurer avec `--confirm RESTORE_EMPTY_TARGET` ;
4. redémarrer les conteneurs et laisser Compose générer un nouveau jeton de
   service ;
5. tester la connexion, l'ouverture d'un PDF, les annotations et un appel
   `/health` ;
6. conserver la quarantaine jusqu'à validation fonctionnelle.

La référence est restaurée automatiquement lorsqu'elle est présente. L'option
`--skip-reference` l'omet explicitement ; `--restore-reference` exige sa présence
(notamment pour contrôler une ancienne sauvegarde).

Un exercice de restauration isolé doit être effectué au minimum après la mise
en production, après toute évolution du stockage et périodiquement. Une
sauvegarde non restaurée en test n'est pas encore une preuve de reprise.

## Déploiement sur disque indépendant du dépôt et de la VM

Un chemin extérieur au dépôt n'est pas automatiquement un stockage extérieur à
la VM. Faire provisionner et monter un disque persistant indépendant (système
de fichiers local au sens SQLite), conservé lors de la suppression de la VM.
Ne pas monter la base SQLite active dans BlobFuse ni partager son fichier entre
le PC et la VM. Les clients distants utilisent l'API/MCP de la VM.

Les fichiers Compose acceptent désormais `JIN_DATA_ROOT`, `JIN_FEEDBACK_ROOT`,
`JIN_CORE_ROOT`, `JIN_WEB_ROOT` et `JIN_BACKUP_ROOT`. Le cœur de build externe
utilise un contexte nommé Docker (`additional_contexts`, Compose >= 2.17).
Les chemins internes des conteneurs restent inchangés. Les montages de données
refusent de créer automatiquement un répertoire source absent.

Exemple de configuration **à adapter**, dans `/etc/jin-model/runtime.env` :

```dotenv
JIN_DATA_ROOT=/mnt/jin-data/data
JIN_FEEDBACK_ROOT=/mnt/jin-data/feedback
JIN_CORE_ROOT=/mnt/jin-data/model
JIN_BACKUP_ROOT=/mnt/jin-data/backups
JIN_WEB_BIND=127.0.0.1
JIN_WEB_PORT=8080
```

Avant le démarrage, vérifier `mountpoint -q /mnt/jin-data`. Si le disque manque,
le service doit rester arrêté. Ne pas créer une nouvelle base vide pour
contourner cette erreur. Conserver des droits adaptés au compte de service ;
ne pas rendre le disque accessible à tous les utilisateurs de la VM.

### Préparer les deux livrables sur la machine source

1. Terminer les écritures/entraînements et arrêter temporairement `api`, `mcp`
   et `trainer` pour la migration initiale. La copie SQLite en ligne est
   cohérente, mais elle ne fige pas simultanément les modèles et tous les
   fichiers externes à la base.
2. Créer et vérifier la sauvegarde complète avec le profil `ops`, puis utiliser
   `bundle`/`azure-upload`. Elle contient les données et le cœur source privé.
3. Construire une image depuis un commit propre, avec le cœur disponible :

```bash
export JIN_BUILD_REVISION=$(git rev-parse HEAD)
docker compose -f docker-compose.yml build api
python scripts/jin_release.py export --image jin-model-api:latest \
  --output /mnt/transfert/jin-release
python scripts/jin_release.py verify /mnt/transfert/jin-release
```

En PowerShell, utiliser `$env:JIN_BUILD_REVISION = git rev-parse HEAD` et des
chemins Windows. Si `JIN_RUNTIME_IMAGE` est personnalisé, passer cette même
référence à `--image`.

L'export comprend `runtime-image.tar`, `release.json` (commit, plateforme,
identifiant immuable Docker et SHA-256 de l'archive) et `image.env`. Il vérifie
que le cœur JIN est réellement importable. Les PDF et annotations ne sont pas
embarqués dans l'image : ils restent dans la sauvegarde privée séparée.

Déposer ces deux livrables dans un stockage privé approuvé (Blob ou disque de
transfert). Aucun SCP ni document client dans Git n'est nécessaire. Le dépôt
seul ne peut pas reconstruire les données privées absentes. Le transfert Blob
doit être configuré avec le compte/conteneur de l'organisation ; il n'est pas
activé automatiquement par un `git pull`.

### Réception sur la VM

Télécharger les livrables, vérifier l'archive avec `jin_backup.py verify`, puis
restaurer vers un emplacement vide du disque monté :

```bash
python3 scripts/jin_backup.py restore /mnt/transfert/jin-model-<date> \
  --target-root /mnt/jin-data --confirm RESTORE_EMPTY_TARGET
python3 scripts/jin_release.py load /mnt/transfert/jin-release

docker compose -f docker-compose.yml \
  --env-file /mnt/jin-data/config/runtime-settings.env \
  --env-file /mnt/transfert/jin-release/image.env \
  --env-file /etc/jin-model/runtime.env \
  up -d --no-build --wait api mcp web
```

L'ordre des fichiers d'environnement est volontaire : réglages sauvegardés,
image exacte, puis paramètres hôtes. Ne pas redéfinir l'image dans le dernier
fichier. `load` contrôle l'architecture et l'image importée, puis régénère
`image.env` avec l'identifiant `sha256:...` plutôt qu'un tag mutable. Le code web
monté depuis Git doit être au commit indiqué dans `release.json`.

Le démarrage depuis l'image importée ne nécessite **aucun rebuild du runtime**.
Pour reconstruire volontairement une nouvelle version, le cœur restauré est
accessible via `JIN_CORE_ROOT` ; choisir un nouveau tag `JIN_RUNTIME_IMAGE` et
une nouvelle révision avant le build, jamais l'identifiant immuable existant.
Le trainer reste un service séparé, à construire si l'entraînement est utilisé.
Son cache Hugging Face n'est pas inclus dans la sauvegarde standard : prévoir
un téléchargement autorisé ou un transfert contrôlé pour entraîner hors ligne.

Les nouveaux manifests utilisent des références relatives pour les PDF,
annotations et images préparées. Les chemins des datasets et jobs sont stockés
relativement à Studio ; l'API les résout sur la machine courante. Les anciens
chemins Windows/Linux reconnus sont relocalisés à la lecture, sans réécrire
les valeurs annotées ni leurs révisions.

Avant bascule : contrôler les nombres de documents/révisions/masques, les
empreintes des PDF et modèles, l'ouverture des documents, une sauvegarde et
une restauration isolée, puis les extractions d'un même lot témoin. Garder
l'installation source intacte jusqu'à validation. Le timer fourni nécessite
la même configuration : `JIN_RUNTIME_SETTINGS_FILE`, `JIN_RELEASE_ENV_FILE`
et `JIN_COMPOSE_ENV_FILE` désignent, dans cet ordre, les trois fichiers
d'environnement utilisés ci-dessus. Le chiffrement, la rétention Blob et la conservation du disque
doivent être configurés par l'administrateur de l'infrastructure.
