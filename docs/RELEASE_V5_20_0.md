# JIN runtime 5.20.0 — contrat canonique

La version `5.20.0-canonical-contract` corrige la source structurelle des
doublons d'adresse. La sortie propre utilise désormais
`jin-clean-extraction-v3`.

## Propriété unique des données

- `order.parties.<role>` contient l'identité, les contacts et les identifiants
  légaux de la partie ;
- `order.addresses.<role>.selected` contient son unique adresse postale
  sélectionnée ;
- `order.addresses.<role>.alternatives` ne contient que les candidats non
  résolus et n'est jamais traité comme vérité d'annotation ;
- `deliver_to` et `consignee` sont fusionnés dans `ship_to`, tandis que
  `invoice_to` est fusionné dans `bill_to` ;
- `parties.*.address`, `order.delivery_address` et
  `delivery_address_selection` ne sont plus produits.

`normalized_value` est une projection imprimable dérivée. Elle ne remplace ni
les composants source ni leurs rectangles de preuve.

## Migration de JIN Studio

La migration V2 vers V3 est volontairement unidirectionnelle. Avant la remise
à zéro, la base SQLite est sauvegardée. Les PDF, comptes, sessions et événements
d'audit restent conservés ; les annotations, masques, datasets et entraînements
liés à l'ancien contrat sont recréés depuis le contrat V3. Chaque champ revient
à l'état **À vérifier**.

Le schéma V2 reste exposé pour contrôler les archives, mais les nouvelles
extractions et annotations utilisent exclusivement V3.

## Préannotations et masques

La régénération complète peut réexécuter JIN-model sur tous les PDF Studio,
valider chaque extraction contre le schéma V3 et préparer les rectangles OCR
avant une écriture atomique. Un échec de document laisse la base inchangée et
la sauvegarde contient les résultats intermédiaires pour diagnostic ou reprise.

Les masques d'annotation ont un contrat distinct V2 limité aux en-têtes et aux
pieds de page. Ils ne mémorisent jamais les lignes de commande. Lors de leur
application dans l'interface, les lignes extraites par JIN sur le PDF cible
sont conservées et restent à relire.
