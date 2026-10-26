# Sauvegarde et restauration de JIN Model

## Ce qui est protégé

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

La BAN sous `data/reference` représente plusieurs centaines de Mo et peut être
reconstruite depuis sa source officielle. Elle est donc exclue par défaut. Pour
une sauvegarde autonome incluant aussi la BAN, ajouter `--include-reference`.

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

Pour inclure exceptionnellement la BAN, exécuter directement le script depuis
le dépôt :

```bash
python scripts/jin_backup.py create --include-reference
```

## Test de restauration isolé

Une restauration ne remplace jamais une donnée existante. Les cibles gérées
(`data/studio`, `data/learning`, `feedback`, `model`, et éventuellement
`data/reference`) doivent être absentes ou vides et la confirmation littérale
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
3. installer Docker, Python 3 et Azure CLI sur la VM ;
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

Ajouter `--restore-reference` uniquement si la sauvegarde a été créée avec
`--include-reference`.

Un exercice de restauration isolé doit être effectué au minimum après la mise
en production, après toute évolution du stockage et périodiquement. Une
sauvegarde non restaurée en test n'est pas encore une preuve de reprise.
