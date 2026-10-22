# Documentation JIN

Cette page distingue la documentation opérationnelle actuelle des rapports et
notes historiques. La source de vérité de version est
`jin_runtime/__init__.py`; la version active est
`5.19.0-studio-identity`.

## Versions à ne pas confondre

| Élément | Version | Compatibilité |
| --- | --- | --- |
| Runtime et UI | `5.19.0-studio-identity` | couche active de ce dépôt |
| Cœur d'extraction | `5.1.0` | moteur local reçu sous `model/` et superposé par `core_overrides/` |
| Sortie consommateur | `jin-clean-extraction-v2` | contrat JSON stable et indépendant du numéro du runtime |
| Routeur PDF | `jin-pdf-fusion-router-v3-cpu` | artefact statistique de routage documentaire |
| Routeur de champs | `jin-field-weak-router-v2-cpu` | artefact statistique de suggestions spatiales |
| Feedback humain | contrat v1 / mémoire v2 | protocole d'annotation et format du modèle appris |

Une note intitulée V5.7 décrit donc une étape historique de l'intelligence
spatiale, pas la version actuelle du runtime.

## Documentation opérationnelle actuelle

- [README principal](../README.md) : vue d'ensemble, architecture et commandes
  usuelles.
- [Installation et exploitation](RUNTIME_SETUP.md) : modes standalone et
  complet, endpoints, variables et vérifications.
- [Qualité des sorties](OUTPUT_QUALITY.md) : porte qualité, lignes, dates,
  rôles, adresses, TVA et preuves.
- [Apprentissage statistique](STATISTICAL_LEARNING.md) : séparation entre
  modèles préentraînés, suggestions et mémoire de feedback.
- [Contrat de feedback humain](FEEDBACK_CONTRACT_V1.md) : authentification,
  validation, idempotence et entraînement batch.
- [JIN Studio](JIN_STUDIO.md) : annotation spatiale, révisions immuables,
  datasets versionnés et entraînements candidats isolés.
- [Version 5.19.0](RELEASE_V5_19_0.md) : identité, rôles et audit JIN Studio.
- [Version 5.18.0](RELEASE_V5_18_0.md) : création initiale de JIN Studio.
- [Version 5.17.0](RELEASE_V5_17_0.md) : garde-fous financiers et rôles métier.
- [Changelog](../CHANGELOG.md) : historique condensé des versions.
- Schéma consommateur :
  [`jin-clean-extraction-v2.schema.json`](../jin_runtime/schemas/jin-clean-extraction-v2.schema.json).

## Convention métier actuelle

Sur un bon de commande client :

- `buyer` est le client qui émet et passe la commande ;
- `supplier` est le fournisseur auquel la commande est adressée ;
- `bill_to` et `ship_to` restent des rôles distincts, même lorsqu'ils
  appartiennent au même client ;
- le code article fournisseur est la référence produit canonique quand une
  colonne fournisseur explicite existe ; le code interne client reste dans
  `customer_material_number` ;
- `order_date` est la date du bon et `requested_delivery_date` la date de
  livraison demandée ;
- une TVA calculée n'est acceptée que depuis un SIREN/SIRET français validé et
  conserve sa provenance de calcul.

`CHECKS_PASSED` signifie que les règles automatiques sont satisfaites. Ce statut
n'est ni une vérité terrain, ni une autorisation implicite de traitement sans
la gouvernance métier applicable.

## Notes de version historiques

- [5.16.9 — dates de livraison](RELEASE_V5_16_9.md)
- [5.16.8 — rôles des immatriculations](RELEASE_V5_16_8.md)
- [5.16.7 — TVA dérivée fiable](RELEASE_V5_16_7.md)
- [5.16.6 — normalisation OCR contextuelle](RELEASE_V5_16_6.md)
- [5.16.5 — récupération sémantique des tableaux](RELEASE_V5_16_5.md)
- [5.16.4 — doubles références empilées](RELEASE_V5_16_4.md)
- [5.16.3 — complétude des hypothèses de lignes](RELEASE_V5_16_3.md)
- [5.16.2 — références empilées et prix nets](RELEASE_V5_16_2.md)
- [5.16.1 — garde d'en-tête de tableau](RELEASE_V5_16_1.md)
- [5.16.0 — fiabilité des tableaux scannés](RELEASE_V5_16_0.md)
- [5.15.0 — confiance des références article](RELEASE_V5_15_0.md)
- [5.12.0 — métadonnées commerciales](RELEASE_V5_12_0.md)
- [5.11.1 — préfixes des références client](RELEASE_V5_11_1.md)
- [5.11.0 — profils de références article](RELEASE_V5_11_0.md)
- [5.10.0 — fiabilité des en-têtes](RELEASE_V5_10_0.md)

## Rapports historiques et recherche

Les documents ci-dessous décrivent l'état observé au moment de leur rédaction.
Leurs métriques restent utiles pour la traçabilité mais ne remplacent pas les
tests et campagnes de la version actuelle :

- [Rapport du corpus initial](CORPUS_TRAINING_REPORT.md)
- [Corpus Git LFS et piste LayoutLMv3](LAYOUT_VISION_LFS.md)
- [Entraînement sur les vrais PDF V5.4](V5_4_REAL_PDF_TRAINING.md)
- [Apprentissage faible V5.5](V5_5_WEAK_FIELD_LEARNING.md)
- [Durcissement géométrique V5.5.1](V5_5_1_GEOMETRY_HARDENING.md)
- [Généralisation V5.5.2](V5_5_2_TEMPLATE_GENERALIZATION.md)
- [Zones spatiales V5.6](V5_6_SPATIAL_ZONE_INTELLIGENCE.md)
- [Cellules et sous-zones V5.7](V5_7_CELL_SUBZONE_INTELLIGENCE.md)

Le benchmark open-weight possède son propre protocole dans
[benchmarks/open_weight_comparison/README.md](../benchmarks/open_weight_comparison/README.md).
