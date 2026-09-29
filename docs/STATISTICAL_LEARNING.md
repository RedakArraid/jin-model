# Couche d'apprentissage statistique

JIN V5.2 ajoute une mémoire supervisée apprise à partir des extractions historiques corrigées.

## Modèles appris

- un classifieur de rôle d'adresse : `ship_to`, `bill_to`, `supplier`, `buyer`, etc. ;
- un classifieur token par token des composants : bâtiment, numéro, suffixe, type/nom de voie, zone, BP/TSA/CS, code postal, ville, CEDEX, INSEE, région, pays, etc.

Les événements corrigés sont stockés dans `feedback/learning_feedback.jsonl`. Les poids sont persistés sous `data/learning/address_models.joblib`.

## API

- `POST /feedback` enregistre une correction et réentraîne la mémoire légère ;
- `GET /learning/status` expose l'état, le nombre d'exemples et les garde-fous ;
- `/health` et `/extract` conservent le payload du moteur cœur et ajoutent les métadonnées de la couche statistique.

## Garde-fous

Par défaut :

- remplacement d'un rôle uniquement si le rôle existant est explicitement faible et si la confiance apprise atteint `0.93` ;
- remplissage uniquement des composants manquants à partir de `0.88` ;
- aucune valeur structurée existante n'est écrasée par défaut ;
- la couche statistique ne positionne jamais `is_verified_real_address=true`.

BAN/Géoplateforme ou un autre référentiel officiel approuvé reste l'autorité pour déclarer qu'une adresse existe réellement.

## Entraînement hors API

```bash
python training/train_statistical.py --feedback feedback/learning_feedback.jsonl --model-dir data/learning
```

Les métriques de production doivent être calculées sur un holdout séparé par famille documentaire, avec précision, rappel, F1, exact-match et faux positifs par champ.
