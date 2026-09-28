# JIN Model - Universal Document AI V4.8

Interface web + API Docker Compose pour tester JIN Model V4.8 sur des bons de commande et autres documents metier.

Le moteur traite notamment les PDF natifs, scans/images et documents Office, puis expose les numeros et dates de commande, acteurs, adresses avec roles metier, lignes produits, charges/eco-participations, totaux, validations financieres et scores de confiance.

## Installation

### 1. Cloner le depot

```bash
git clone https://github.com/RedakArraid/jin-model.git
cd jin-model
```

### 2. Ajouter le moteur V4.8

Le code de deploiement et l'interface sont versionnes dans ce depot. Le connecteur GitHub utilise pour la publication ne pouvant pas transferer l'archive binaire du modele depuis le sandbox, place le fichier `jin-model-v4.8-source.zip` fourni avec la livraison dans la racine du depot.

Puis :

```bash
chmod +x prepare-model.sh
./prepare-model.sh jin-model-v4.8-source.zip
```

Le script cree le dossier local `model/`, ignore par Git.

### 3. Lancer

```bash
docker compose up --build
```

Ouvrir ensuite :

- Interface : **http://localhost:8080**
- Health check : **http://localhost:8080/api/health**

## Interface

L'interface permet :

- glisser-deposer ou selectionner un PDF/image/Office/CSV/JSON ;
- forcer un type de document ou fournir un schema JSON ;
- afficher le numero/date de commande ;
- afficher acheteur, fournisseur et contacts ;
- afficher les adresses avec roles metier (`buyer`, `supplier`, `ship_to`, `bill_to`, etc.) ;
- afficher les lignes produits, quantites, unites, prix et montants ;
- afficher DEEE, eco-participations et autres charges ;
- afficher totaux, validation, confiance et besoin de revue humaine ;
- consulter et telecharger le JSON complet.

## API

```bash
curl http://localhost:8080/api/health
curl -F "file=@commande.pdf" http://localhost:8080/api/extract
```

## Arreter la stack

```bash
docker compose down
```

## Logs

```bash
docker compose logs -f api
docker compose logs -f web
```

## Validation de la livraison

La suite V4.8 a ete rejouee apres ajout de l'API/UI : **48/48 tests passent**.

L'API a egalement ete lancee directement avec Uvicorn dans l'environnement de construction et `/health` a repondu avec la version **4.8.0**.

Le binaire Docker n'etait pas present dans cet environnement, donc l'execution de `docker compose up` n'a pas pu etre validee ici. La configuration Compose a ete preparee pour Docker Compose v2.
