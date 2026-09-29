# Changelog

## 5.2.0-statistical-learning

- Couche runtime versionnée au-dessus du moteur JIN empaqueté.
- Mémoire statistique supervisée à partir des corrections historiques.
- Classifieurs appris pour rôles et composants d'adresse avec garde-fous.
- Endpoints `POST /feedback` et `GET /learning/status`.
- Enrichissement de `/health` et `/extract`.
- Vérification officielle d'adresse maintenue séparée de l'inférence statistique.
- Politique Git LFS pour PDF/images du corpus.
- Contrôle des pointeurs LFS non matérialisés.
- Préparation OCR/layout des pages PDF complètes.
- Pipeline de fine-tuning LayoutLMv3 pour champs et cellules de tableaux.
- CI runtime et workflow manuel d'entraînement layout/vision.
- `prepare-model.sh` généralisé aux archives moteur V4.8/V5.x contenant `uda/`.

## 4.8.0

- Docker Compose et interface web autour du moteur Universal Document AI empaqueté.
