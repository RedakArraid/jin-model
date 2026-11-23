# Extraction des offres de prix fournisseur

Une offre de prix n'est pas un bon de commande client. Si JIN reconnaît une
`quotation` avec le titre « Offre de prix » et le tableau d'articles du modèle
elm.leblanc, il renseigne `normalized_output.quotation` sans créer `order`.

L'objet `quotation` contient : numéro et date de l'offre, date de validité,
affaire/projet, vendeur et contact commercial, destinataire et coordonnées,
informations spécifiques au projet, familles et lignes d'articles (code,
désignation, quantité, prix unitaire, éco-participation, montant HT), sous-totaux,
total HT, période du chantier/marché et textes des conditions commerciales.
Les valeurs explicites conservent leur page et leur zone source.

La validation compare chaque montant de ligne à quantité × prix unitaire, la
somme des lignes aux sous-totaux, puis le total HT aux lignes et à
l'éco-participation. Un champ indispensable absent ou un écart financier
entraîne `REVIEW_REQUIRED`. `CHECKS_PASSED` ne dispense pas d'une vérification
humaine avant d'utiliser l'offre comme source contractuelle.

Cette extraction est limitée au tableau et à la mise en page des offres
elm.leblanc fournies en exemples. Elle lit la couche texte du PDF : une offre
scannée sans texte, ou une autre mise en page, reste à examiner et ne doit pas
être considérée comme extraite complètement. Le numéro d'offre n'est jamais
recopié dans `order.customer_order_number`.
