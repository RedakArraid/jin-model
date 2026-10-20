# Couche d'apprentissage statistique

JIN sépare trois familles de modèles afin qu'une prédiction non revue ne
devienne jamais automatiquement une vérité d'entraînement.

## Modèles préentraînés livrés

- `corpus_text_router.joblib` route les familles documentaires à partir des
  métadonnées et du texte ;
- `jin-pdf-fusion-router-v3-cpu.joblib` combine texte, géométrie et aperçu
  visuel pour la famille et la décision documentaire ;
- `jin-field-weak-router-v2-cpu.joblib` propose des champs, zones et cellules
  à partir du PDF.

Le routeur de champs reste une source de suggestions. Ses candidats sont
contraints par les zones compatibles et portent `requires_review=true`; ils ne
sont promus que par les règles conservatrices de réconciliation du runtime.

## Mémoire supervisée issue du feedback

La mémoire optionnelle `address_models.joblib` est la seule couche réentraînée
depuis `POST /feedback`. Elle apprend à partir d'adresses corrigées et
explicitement validées par un humain :

- rôle d'adresse (`ship_to`, `bill_to`, `supplier`, `buyer`, etc.) ;
- composants d'adresse : bâtiment, numéro, voie, zone, BP/TSA/CS, code postal,
  ville, CEDEX, INSEE, région et pays.

Les événements sont stockés dans `feedback/learning_feedback.jsonl`. Les poids
sont persistés dans `data/learning/address_models.joblib`. Une mémoire vide ou
créée à partir des propres prédictions de JIN n'est pas un modèle valide.

## API et ingestion

- `POST /feedback` journalise une annotation v1 authentifiée et idempotente ;
- `GET /learning/status` expose les exemples acceptés, rejetés, mal formés ou
  dupliqués ainsi que les garde-fous ;
- `/health` et `/extract` conservent le payload du moteur cœur et ajoutent les
  métadonnées des modèles chargés ;
- `POST /learning/pdf-route` interroge le routeur PDF ;
- `POST /learning/field-route` expose les suggestions de champs, zones et
  cellules sans les présenter comme vérité terrain.

Le réentraînement synchrone est désactivé par défaut. Cela garde la latence de
`POST /feedback` stable quand le journal grandit. Le batch s'exécute dans le
conteneur :

```bash
docker compose run --rm --no-deps api \
  python training/train_statistical.py \
  --feedback /app/training/feedback/learning_feedback.jsonl \
  --model-dir /app/data/learning
```

Le format complet, l'authentification, l'idempotence et la stratégie de
migration sont décrits dans [FEEDBACK_CONTRACT_V1.md](FEEDBACK_CONTRACT_V1.md).

## Garde-fous d'inférence

Pour la mémoire d'adresses, par défaut :

- remplacement d'un rôle uniquement si le signal existant est faible et si la
  confiance apprise atteint `0.93` ;
- remplissage uniquement des composants manquants à partir de `0.88` ;
- aucun composant d'adresse non vide n'est écrasé par la mémoire ;
- la couche statistique ne positionne jamais
  `is_verified_real_address=true`.

Les corrections déterministes de dates, lignes, prix, totaux, identifiants et
rôles du runtime 5.17 ne sont pas des sorties de cette mémoire. Elles exigent
des preuves source et possèdent leurs propres tests de non-régression.

La BAN/Géoplateforme ou un autre référentiel officiel approuvé reste l'autorité
qui déclare qu'une adresse existe réellement.

## Validation avant promotion

Les métriques de production sont calculées sur un holdout séparé par famille
documentaire, avec précision, rappel, F1, exact-match et faux positifs par
champ. Une mémoire v2 est d'abord utilisée en shadow. Elle n'est promue que si
le statut confirme des événements v1 valides et si les régressions métier
restent sous les seuils convenus. Les campagnes sans vérité terrain revue
mesurent la couverture et les alertes, pas l'exactitude métier.
