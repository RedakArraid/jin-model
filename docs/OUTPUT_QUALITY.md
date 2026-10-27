# Qualité des sorties JIN 5.20

JIN applique plusieurs contrôles successifs. Aucun score isolé n'est considéré
comme une preuve d'exactitude et une réponse HTTP réussie ne signifie pas que
le document peut être intégré sans revue.

## Chaîne de décision

1. Le cœur 5.1 extrait les champs, lignes, totaux et preuves.
2. `core_overrides/` compare plusieurs hypothèses structurées et applique les
   garde-fous métier versionnés.
3. Le runtime réconcilie les champs géométriques, les rôles et les adresses.
4. `output_quality` recherche les incohérences JSON et répare uniquement les
   défauts de présentation explicitement sûrs.
5. `extraction_decision` agrège les résultats et produit
   `CHECKS_PASSED` ou `REVIEW_REQUIRED`.
6. `normalized_output` expose la même décision dans le contrat
   `jin-clean-extraction-v3` sous `quality`.

`CHECKS_PASSED` signifie que les contrôles automatiques connus sont satisfaits.
Il ne constitue pas une vérité terrain ni une autorisation automatique de
traitement sans la gouvernance métier applicable.

## Numéros et références de commande

Le numéro de commande client reste dans `purchase_order.number` et dans
`order.customer_order_number` dans la vue propre. Il est distinct de :

- la référence client libre ;
- la référence fournisseur ;
- un devis ou une dérogation ;
- un code d'agence ;
- une référence produit ;
- un numéro de téléphone, SIREN/SIRET ou code postal.

La preuve conserve page, boîte, texte source et méthode. Un numéro interne
identique à une référence article de ligne est supprimé. Les commandes groupées
peuvent exposer plusieurs numéros source sans inventer artificiellement un
numéro primaire.

## Lignes de commande et montants

Les hypothèses de tableau sont classées avec quatre familles de signaux :

- complétude et couverture des lignes ;
- forme plausible des références article, apprise sur la distribution de la
  masterdata mais sans exiger une correspondance exacte dans cette base ;
- cohérence `quantité × prix net = montant` lorsqu'un montant est imprimé ;
- réconciliation de la somme des produits et frais avec le total document.

Une référence article ne peut pas être une capture arbitraire de toute la
ligne. Les références client et fournisseur restent séparées lorsque les
colonnes ou libellés les distinguent. Les ports, frais et contributions
environnementales explicites sont déplacés vers `additional_charges` au lieu
d'être conservés comme produits.

Les prix ERP français à quatre à six décimales, par exemple `620,0000`, sont
relus depuis le jeton source exact. JIN ne remplace jamais un montant distinct
imprimé sur la ligne. Il recalcule seulement un montant qu'il avait lui-même
dérivé de la quantité et du prix. Si ce montant dérivé reste incompatible avec
le total du document, la confiance est plafonnée et la ligne passe en revue.

## Dates

`order_date` représente la date du bon de commande.
`requested_delivery_date` représente la date demandée ou estimée de livraison.
Une date de livraison ne peut remplacer la date de commande lorsque l'en-tête
porte une date explicite distincte. Les dates de réception propres à chaque
ligne restent attachées à la ligne ; elles ne sont promues au niveau commande
que si toutes les lignes datées concordent.

## Parties et rôles

Pour un bon de commande client :

- `buyer` est le client émetteur de la commande ;
- `supplier` est le fournisseur destinataire ;
- `bill_to` est l'entité facturée ;
- `ship_to` est le site livré.

Une inversion client/fournisseur n'est corrigée que lorsque le titre de bon de
commande, un bloc de facturation explicite, le destinataire d'en-tête et un
rôle fournisseur inutilisable concordent. Les SIREN/SIRET non qualifiés suivent
la même sémantique client par défaut uniquement après les preuves explicites et
les correspondances de noms.

Les collisions de téléphone ou d'e-mail entre acheteur et fournisseur, les
noms absents ou non informatifs et les rôles non résolus déclenchent une revue.

## Adresses

Le formateur traite séparément numéro, suffixe, type et nom de voie, bâtiment,
résidence, zone industrielle, lieu-dit, BP/TSA/CS, routage postal, code postal,
ville, CEDEX et pays. Il élimine les doublons de présentation sans modifier la
preuve source.

Lorsqu'une adresse géométrique propre remplace une extraction polluée, un
complément de site court comme `ALBANORD` est conservé. Les fragments contenant
produits, prix, références longues, mentions légales, conditions ou transport
ne sont pas réinjectés dans l'adresse.

La BAN locale vérifie l'existence et la forme postale, jamais le rôle métier.
Une voie proche reste une suggestion. Une absence BAN n'est pas la preuve
qu'une adresse est fausse, notamment pour les CEDEX, BP ou données récentes.

## Identifiants fiscaux

Une TVA imprimée est contrôlée par checksum lorsqu'il s'agit d'un numéro
français. Si la TVA est absente, JIN peut la calculer depuis un SIREN ou SIRET
français de longueur exacte et checksum valide. La TVA calculée conserve :

- l'immatriculation source ;
- la formule déterministe ;
- le rôle de partie résolu ;
- le statut `DERIVED_FROM_VALID_SIREN` ou
  `DERIVED_FROM_VALID_SIRET` ;
- la preuve du SIREN/SIRET, sans prétendre que la TVA calculée était imprimée.

## Codes principaux de revue

La liste exacte reste dans `extraction_decision.reasons`. Les catégories les
plus importantes sont :

- commande, date, total ou lignes manquants ;
- référence produit manquante ou invraisemblable ;
- validation financière incomplète ;
- parties manquantes, non informatives ou contacts en conflit ;
- adresse de livraison/facturation manquante, ambiguë ou incomplète ;
- rôle d'adresse inconnu ou incertain ;
- référence BAN indisponible, proche ou non concluante ;
- désaccord entre candidats de champs.

Chaque raison contient un `code` et un `path` afin que l'UI ou le système
appelant puisse diriger la revue vers le champ concerné.
