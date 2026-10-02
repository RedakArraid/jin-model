# Contrat de feedback humain v1

`POST /feedback` accepte uniquement une annotation explicitement revue par un
humain. L'endpoint est désactivé tant que `JIN_FEEDBACK_TOKEN` est vide.

## Configuration

Créer un secret aléatoire hors du dépôt, puis le placer dans `.env` :

```bash
openssl rand -hex 32
JIN_FEEDBACK_TOKEN=<secret-genere>
```

Les deux fichiers Compose transmettent ce secret au conteneur. Le client doit
envoyer `Authorization: Bearer <secret>`. Une absence de secret côté serveur
retourne `503`; un secret absent ou incorrect côté client retourne `401`.

`JIN_FEEDBACK_RETRAIN_ON_WRITE=0` est la valeur recommandée et la valeur par
défaut. L'API journalise alors rapidement l'annotation avec
`pending_retrain=true`. L'entraînement est exécuté séparément, dans Docker :

```bash
docker compose run --rm --no-deps api \
  python training/train_statistical.py \
  --feedback /app/training/feedback/learning_feedback.jsonl \
  --model-dir /app/data/learning
```

Le mode `JIN_FEEDBACK_RETRAIN_ON_WRITE=1` existe pour un petit environnement de
développement, mais relit et réentraîne tout l'historique à chaque appel.

## Requête minimale

```json
{
  "schema_version": 1,
  "annotation_id": "gc:ord-1234:8da87f1c",
  "document_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "validation": {
    "status": "human_validated",
    "reviewed_by": "user-id",
    "reviewed_at": "2026-10-02T08:00:00Z",
    "source": "geniecommande"
  },
  "extraction": {
    "business_addresses": [
      {
        "formatted_address": "10 RUE DE LA PAIX 75001 PARIS",
        "role": "unknown"
      }
    ]
  },
  "corrections": {
    "business_addresses": [
      {
        "index": 0,
        "formatted_address": "10 RUE DE LA PAIX 75001 PARIS FRANCE",
        "role": "ship_to",
        "address": {
          "house_number": "10",
          "street_type": "RUE",
          "street_name": "DE LA PAIX",
          "postal_code": "75001",
          "city": "PARIS",
          "country_code": "FR"
        }
      }
    ]
  }
}
```

Exemple d'appel :

```bash
curl -X POST http://localhost:8080/api/feedback \
  -H "Authorization: Bearer ${JIN_FEEDBACK_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @feedback-v1.json
```

## Garanties et refus

- `annotation_id` contient 8 à 128 caractères sûrs et sert de clé
  d'idempotence ; le même identifiant et le même contenu sont acceptés comme
  doublon, tandis qu'un contenu différent est refusé ;
- `document_sha256` est obligatoire et contient le SHA-256 du document source ;
- `validation.status` vaut exactement `human_validated`, avec reviewer, source
  et horodatage ISO-8601 incluant le fuseau ;
- seuls les rôles canoniques JIN sont appris ; `role_label` libre n'est jamais
  utilisé comme classe ;
- un index explicite doit pointer une adresse extraite existante et ne peut pas
  être négatif ;
- le JSON doit être fini, sérialisable et inférieur ou égal à 2 Mo ;
- un digest `annotation_digest` est calculé par JIN et contrôlé lors des
  relectures ;
- les anciens événements sans contrat v1 restent dans le journal mais sont
  comptés comme rejetés et ne participent pas à l'entraînement.

Le SHA-256 prouve l'identité déclarée du document mais JIN ne possède pas les
octets du PDF dans cet endpoint. Le système émetteur doit donc calculer ce hash
sur le PDF effectivement revu et conserver l'audit associé.

## Déploiement de la mémoire v2

Un modèle statistique v1 n'est pas chargé par la mémoire v2, car il peut avoir
été entraîné à partir de corrections non attestées. Déployer d'abord la v2 en
shadow, contrôler `events`, `rejected_feedback_records` et
`malformed_feedback_records` dans `GET /learning/status`, puis seulement
promouvoir le profil. Les anciennes corrections doivent être revues et
réémises sous le contrat v1 ; elles ne sont jamais migrées automatiquement.
