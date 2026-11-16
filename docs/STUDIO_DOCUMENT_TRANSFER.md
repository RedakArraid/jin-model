# Transférer les PDF et annotations par MCP

Deux instances JIN Studio peuvent désormais échanger un dossier complet par
leur endpoint MCP authentifié `/mcp`, sans SCP et sans accès au disque de la VM.
Les deux instances doivent exécuter une version contenant ces outils. Un simple
`git pull` ne met pas à jour les conteneurs : reconstruire/recréer les services
ou déployer la nouvelle image complète avant de transférer.

## Ce qui est conservé

- Le PDF original, contrôlé par SHA-256.
- Son identifiant Studio, son nom, sa provenance et son extraction JIN.
- Toutes les révisions d'annotation : valeurs, corrections, cadres, pages,
  états, relecteurs, dates et empreintes d'intégrité, sans réannotation.

Les chemins internes du PDF sont reconstruits relativement au stockage de la
destination. Les éventuels anciens chemins descriptifs dans les métadonnées
de provenance restent informatifs et ne servent pas à lire un fichier.

Ce transfert ne copie **pas** les comptes, sessions, jetons, masques, datasets,
exécutions d'entraînement, modèles ou référentiels BAN. Pour reproduire toute
l'installation, utiliser la [sauvegarde et l'image complète](BACKUP_AND_RESTORE.md).
Importer une annotation ne déclenche ni entraînement ni promotion du modèle.

## Annotations déjà validées

Un statut `HUMAN_VALIDATED` n'est jamais rétrogradé ou créé silencieusement.
Si l'historique contient une validation humaine, la destination exige :

1. `JIN_STUDIO_MCP_ALLOW_VALIDATION=1` dans son environnement, puis recréation
   du service MCP pour prendre en compte ce réglage ;
2. `confirm_trusted_source=true` lors du début du transfert.

Cette confirmation atteste que la source et ses validations sont fiables ;
elle ne prétend pas qu'une nouvelle relecture humaine a eu lieu. Le relecteur
et la date d'origine restent inchangés. Le contrat de la dernière annotation
est recontrôlé à l'import. Une annotation incompatible avec le contrat actuel
doit être mise à niveau/revue sur la source ; elle n'est pas corrigée en cachette.
Le contrôle SHA-256 vérifie l'intégrité, **pas** la justesse des annotations.

## Outils MCP

| Outil | Usage |
|---|---|
| `export_document_bundle(document_id)` | Manifeste portable avec extraction et historique, sans octets PDF |
| `read_document_pdf_chunk(document_id, offset, max_bytes)` | PDF en blocs base64 de 256 Kio maximum |
| `begin_document_import(document_export, confirm_trusted_source)` | Contrôle du manifeste, création/reprise d'une session |
| `upload_document_pdf_chunk(transfer_id, offset, data_base64, sha256)` | Écriture d'un bloc vérifié, dans l'ordre |
| `get_document_import_status(transfer_id)` | État et nombre d'octets reçus pour reprendre |
| `finish_document_import(transfer_id, confirm_import=true)` | Vérification complète puis publication atomique |
| `cancel_document_import(transfer_id)` | Suppression du fichier partiel, jamais du document importé |

`list_documents` accepte `offset` et renvoie `next_offset` / `has_more`, pour
parcourir toute la collection, au-delà de la limite d'une seule page.
Éviter les ajouts/modifications sur la source pendant l'inventaire paginé.

Le dossier n'apparaît dans Studio qu'après la fin de l'import. La transaction
publie ensemble le document et tout son historique. Un échec avant publication
ne crée pas de dossier partiellement annoté.

## Script de transfert entre instances

Le script utilise uniquement Python standard et le protocole MCP HTTP JSON.
Il n'enregistre ni PDF ni jeton dans le dépôt. Fournir les jetons par les
variables d'environnement `JIN_SOURCE_MCP_TOKEN` et `JIN_TARGET_MCP_TOKEN`
(gestionnaire de secrets ou saisie locale, sans les mettre dans Git).
Le jeton de destination n'est pas nécessaire en simulation.

Simulation : inventaire et vérification de la source, sans import sur la VM
(ce n'est pas un test de connectivité/autorisation de la destination) :

```powershell
python scripts/transfer_studio_documents.py --source-url http://localhost:8080/mcp --target-url https://jin-vm.exemple/mcp --all
```

Transfert réel, en conservant les validations de la source de confiance :

```powershell
python scripts/transfer_studio_documents.py --source-url http://localhost:8080/mcp --target-url https://jin-vm.exemple/mcp --all --execute --confirm-trusted-source
```

Pour une sélection, remplacer `--all` par un ou plusieurs
`--document-id jdoc-...`. Les URL et les identifiants ci-dessus sont des exemples.
HTTPS est exigé hors boucle locale ; `--allow-http` est une exception explicite
pour un réseau privé maîtrisé. Les redirections HTTP sont refusées pour éviter
de transmettre un jeton à un autre hôte. Les listes d'hôtes MCP doivent
autoriser le nom DNS utilisé, sans joker global.

Chaque ligne de résultat indique l'identifiant, le nombre de révisions et
l'état (`DRY_RUN`, `COMPLETE`, `ALREADY_TRANSFERRED` ou `FAILED`). Le script
continue sur les autres dossiers si l'un échoue et retourne un code non nul
s'il reste des échecs. Relancer la même commande reprend les transferts
incomplets et reconnaît ceux déjà terminés ; aucun entraînement n'est lancé.

## Sécurité et limites

- Toute opération requiert le jeton MCP ; export, début, fin et annulation
  sont audités. Les octets et valeurs client ne sont pas écrits dans l'audit.
- Un identifiant de document déjà présent n'est jamais écrasé. Un transfert
  déjà terminé reste reconnu même si quelqu'un a ensuite corrigé la copie
  cible : la relance ne remplace pas ces corrections. Pour une source modifiée,
  il faut résoudre le conflit explicitement, pas faire une synchronisation forcée.
- Aucun chemin de fichier ni URL de téléchargement n'est fourni par le client.
- Les reprises d'un même bloc sont acceptées seulement si les octets sont identiques.
- PDF : 100 Mio maximum ; manifeste : 8 Mio ; historique : 1 000 révisions.
- Sessions : 24 heures ; au plus 16 actives, 512 Mio de PDF réservés au total.
  Les fichiers partiels expirés sont nettoyés au prochain début de transfert.
  L'annulation ne supprime que le fichier partiel et conserve la trace d'audit.
- Un arrêt brutal entre écriture du fichier final et commit SQLite peut laisser
  un répertoire orphelin. L'import refuse de l'écraser : vérifier son origine
  avant toute intervention manuelle, puis relancer le transfert.

Ne transférer les données client que vers une instance autorisée par
l'organisation. Ce mécanisme ne remplace pas une sauvegarde complète.
