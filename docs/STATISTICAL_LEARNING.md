# Couche d'apprentissage statistique

JIN maintient une mémoire supervisée à partir d'adresses corrigées et
explicitement validées par un humain.

## Modèles appris

- un classifieur de rôle d'adresse (`ship_to`, `bill_to`, `supplier`, etc.) ;
- un classifieur des composants d'adresse : bâtiment, numéro, voie, zone,
  BP/TSA/CS, code postal, ville, CEDEX, INSEE, région et pays.

Les événements sont stockés dans
`feedback/learning_feedback.jsonl`. Les poids sont persistés dans
`data/learning/address_models.joblib`.

## API et ingestion

- `POST /feedback` journalise une annotation v1 authentifiée et idempotente ;
- `GET /learning/status` expose les exemples acceptés, rejetés, mal formés ou
  dupliqués ainsi que les garde-fous ;
- `/health` et `/extract` conservent le payload du moteur cœur et ajoutent les
  métadonnées de la couche statistique.

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

Par défaut :

- remplacement d'un rôle uniquement si le signal existant est faible et si la
  confiance apprise atteint `0.93` ;
- remplissage uniquement des composants manquants à partir de `0.88` ;
- aucune valeur structurée existante n'est écrasée ;
- la couche statistique ne positionne jamais
  `is_verified_real_address=true`.

La BAN/Géoplateforme ou un autre référentiel officiel approuvé reste l'autorité
qui déclare qu'une adresse existe réellement.

## Validation avant promotion

Les métriques de production sont calculées sur un holdout séparé par famille
documentaire, avec précision, rappel, F1, exact-match et faux positifs par
champ. Une mémoire v2 est d'abord utilisée en shadow. Elle n'est promue que si
le statut confirme des événements v1 valides et si les régressions métier
restent sous les seuils convenus.
