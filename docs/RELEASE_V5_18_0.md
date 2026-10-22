# JIN runtime 5.18.0 — JIN Studio

La version `5.18.0-jin-studio` ajoute un espace autonome pour construire les
futures versions du modèle sans ajouter d'écrans techniques à GenieCommande.

## Annotation fiable

- dépôt et extraction initiale d'un PDF ;
- revue champ par champ avec correction, absence ou ambiguïté explicite ;
- sélection de la preuve dans la page PDF en coordonnées normalisées ;
- validation humaine obligatoire et historique de révisions immuables ;
- liaison de chaque vérité terrain au SHA-256 du document source.

## Datasets et entraînement

- export des seuls documents `HUMAN_VALIDATED` dans un dataset versionné ;
- seuil minimal configurable, fixé à 20 documents par défaut ;
- file persistante des entraînements LayoutLMv3 ;
- worker Docker optionnel, séparé de l'API d'extraction ;
- heartbeat, reprise d'une exécution abandonnée et journal par entraînement ;
- exécution sans shell, limitée aux scripts d'entraînement autorisés et aux
  chemins internes au stockage JIN Studio.

Cette version fabrique des artefacts candidats mais ne les promeut pas encore
automatiquement en production. L'évaluation comparative, le registre
candidat/shadow/principal et le rollback restent des étapes distinctes.
