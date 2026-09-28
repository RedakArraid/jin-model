# JIN Model - Universal Document AI V4.8

Moteur d'extraction documentaire pour bons de commande et documents metier : PDF natifs, scans/images, Office, tableaux multi-pages, lignes produits, charges/eco-participations, adresses et roles metier, controles financiers, scores de confiance et revue humaine.

## Demarrage rapide avec Docker Compose

```bash
git clone https://github.com/RedakArraid/jin-model.git
cd jin-model
docker compose up --build
```

Ouvrir ensuite : **http://localhost:8080**

L'interface permet d'uploader un document et d'afficher :

- numero et date de commande ;
- acheteur, fournisseur et contacts ;
- adresses avec roles metier (`buyer`, `supplier`, `ship_to`, `bill_to`, etc.) ;
- lignes produits, quantites, prix et montants ;
- charges / DEEE / eco-participations ;
- totaux et validations financieres ;
- confiance globale et besoin de revue humaine ;
- JSON complet telechargeable.

## API

Via le proxy de l'interface :

```bash
curl http://localhost:8080/api/health
curl -F "file=@commande.pdf" http://localhost:8080/api/extract
```

## Source complet

Le connecteur GitHub utilise dans cet environnement n'accepte que les ecritures texte. Le source V4.8 complet est donc versionne dans `bundle_parts/` sous forme d'une archive TAR.XZ encodee en base64 et decoupee. Le `Dockerfile` la reconstruit automatiquement pendant le build.

Pour extraire le source sur votre machine :

```bash
chmod +x unpack.sh
./unpack.sh
cd source/universal_document_ai_rl_v48
```

Le repertoire extrait contient le code Python complet, la configuration, les tests, les scripts d'entrainement, les exemples et la documentation.

## Tests

La version empaquetee conserve la suite de tests V4.8 : **48 tests** passaient dans l'environnement de construction avant publication.

## Arret

```bash
docker compose down
```
