# Documentation JIN dans Docupedia

Le point d'entrée est [Développer et livrer JIN](https://inside-docupedia.bosch.com/confluence2/pages/viewpage.action?pageId=1342609478),
sous **Business Intelligence > Genie Commande > 03 - Pour les developpeurs**.
Cette page conserve son identifiant et distingue l'extracteur `jin-model`
du comparateur `jin-adresse-model`.

## Organisation et sources

La branche **JIN-model — extraction documentaire** contient les guides suivants :

| Guide Docupedia | Références techniques dans ce dépôt |
| --- | --- |
| Architecture et fonctionnement | `README.md`, `docs/RUNTIME_SETUP.md`, `docs/STATISTICAL_LEARNING.md` |
| API et contrat JSON | `jin_runtime/schemas/jin-clean-extraction-v3.schema.json`, `docs/OUTPUT_QUALITY.md` |
| JIN Studio — annotation et masques | `docs/JIN_STUDIO.md` |
| Datasets entraînement et benchmarks | `docs/JIN_STUDIO.md`, `docs/PRODUCTION_READINESS.md`, `training/vision/` |
| Déploiement stockage et transferts | `docs/BACKUP_AND_RESTORE.md`, `docs/STUDIO_DOCUMENT_TRANSFER.md` |
| Versions limites et dépannage | `CHANGELOG.md`, `docs/RELEASE_V5_20_0.md`, `docs/PRODUCTION_READINESS.md` |

Les rubriques ADV, administrateurs, IT et gouvernance donnent accès à ces pages
par des liens, sans copier leur contenu. La page **Amélioration continue
GenieCommande et JIN** porte les responsabilités, les conditions de promotion
et la distinction entre défaut d'extraction, matching et règle métier.

## Publication et maintenance

Les spécifications détaillées et tests restent ici. Les contenus du portail
et leur arborescence sont versionnés dans le dépôt `Geniecommande`, sous
`docs/docupedia/jin/`. Son script `scripts/publish_docupedia_geniecommande.py`
charge ces sources aussi bien en publication ciblée qu'en publication complète.

Depuis ce dépôt GenieCommande :

```powershell
python scripts/publish_docupedia_geniecommande.py --jin-only --preview-dir C:/Temp/jin-docupedia-preview
python scripts/publish_docupedia_geniecommande.py --jin-only --dry-run
python scripts/publish_docupedia_geniecommande.py --jin-only
```

Le premier appel est hors ligne ; les suivants requièrent `DOCUPEDIA_TOKEN`
injecté par un mécanisme sûr, jamais écrit dans Git. La publication sauvegarde
l'existant et relit le résultat. Elle ne modifie ni les archives ESKER ni le
calendrier client. Voir le guide `docs/DOCUPEDIA.md` du dépôt GenieCommande.

Le jeton doit appartenir à l'instance `/confluence2` et autoriser l'édition.
Un jeton de `/confluence` ne suffit pas : une lecture anonyme réussie n'est
pas une preuve d'authentification. Le contrôle d'identité bloque la publication
avant toute modification dans ce cas.

Lors d'une release, vérifier ensemble contrat JSON, version du runtime,
empreintes d'artefacts et état réel des fonctionnalités. Un benchmark produit
ne signifie pas promotion automatique. Ne jamais publier de PDF client, de
valeurs d'annotation réelles ou de secrets dans Docupedia.
