# Préparation à la production et amélioration continue

Ce document est la feuille de route opérationnelle de JIN Model. Il sépare ce
qui est déjà techniquement disponible de ce qui doit encore être démontré avec
des données métier avant une utilisation autonome dans GenieCommande.

## Décision actuelle

| Usage | Décision | Justification |
| --- | --- | --- |
| Annotation et revue dans JIN Studio | **GO** | interface, comptes, audit et stockage opérationnels |
| Exécution locale du moteur JIN | **GO** | API saine et suites de régression validées |
| Comparaison en shadow dans GenieCommande | **GO contrôlé** | aucun impact métier sur le résultat principal |
| Entraînement d'un premier candidat | **NO-GO actuellement** | corpus humainement validé insuffisant |
| Promotion automatique d'un candidat | **NO-GO** | évaluation, registre et rollback non automatisés |
| Déploiement VM avec reprise hors VM | **NO-GO actuellement** | Azure Blob et exercice de reprise VM à configurer |

La mention **GO** ci-dessus valide une capacité technique précise. Elle ne
signifie pas que JIN est autorisé à déclencher seul un envoi SAP.

## Photographie vérifiée au 26 octobre 2026

| Élément | État constaté |
| --- | ---: |
| Runtime | `5.19.0-studio-identity` |
| Baseline applicative analysée | `088f920` |
| Documents Studio | 102 |
| Documents au statut `DRAFT` | 101 |
| Documents au statut `HUMAN_VALIDATED` | 1 |
| Révisions d'annotation | 208 |
| Révisions `HUMAN_VALIDATED` | 3 |
| Datasets versionnés | 0 |
| Entraînements candidats | 0 |
| Utilisateurs Studio | 1 |
| Sauvegarde locale vérifiée | oui |
| Sauvegarde Azure hors VM | non configurée |
| API locale via Nginx | saine (`HTTP 200`) |

Ces chiffres sont une photographie datée, pas un tableau de bord. JIN Studio et
le futur suivi Power BI doivent devenir les sources courantes.

## Ce qui est terminé

- moteur CPU, contrat JSON propre et garde-fous de qualité ;
- extraction et régression sur les principales structures déjà rencontrées ;
- JIN Studio avec comptes nominatifs, rôles, sessions et journal d'audit ;
- préannotation, correction, validation humaine et révisions immuables ;
- création de datasets versionnés à partir des seules annotations validées ;
- worker d'entraînement isolé et production d'artefacts candidats ;
- sauvegarde cohérente des PDF, annotations, comptes, audit, modèles appris et
  cœur JIN ;
- restauration protégée, contrôle SHA-256 et révocation des sessions restaurées ;
- mécanisme d'externalisation Azure Blob et timer systemd prêts à installer ;
- résolution dynamique des services Docker par le proxy web ;
- tests backend, cœur d'extraction et frontend automatisés.

## Porte 1 — Constituer la vérité terrain

Le seuil technique de JIN Studio est de 20 documents validés avec des zones
spatiales. Il permet seulement de créer un dataset `READY`; ce n'est pas une
preuve de diversité suffisante.

Pour un premier candidat exploitable, la cible recommandée est :

- 50 à 100 commandes humainement validées ;
- plusieurs clients et plusieurs mises en page ;
- PDF natifs et documents scannés ;
- documents d'une et de plusieurs pages ;
- adresses, références, quantités, prix, offres et dérogations représentés ;
- documents difficiles et documents qui ne sont pas des commandes ;
- chaque champ classé `CONFIRMED`, `CORRECTED`, `ABSENT` ou `AMBIGUOUS` ;
- aucune annotation `UNREVIEWED` dans une révision validée.

Un lot de test indépendant doit être réservé avant l'entraînement. Il ne doit
être ni utilisé pour créer les labels faibles, ni pour régler les seuils.

**Critère de sortie :** dataset versionné, diversité documentée et holdout gelé.

## Porte 2 — Entraîner et évaluer un candidat

Le premier entraînement doit produire un artefact candidat, jamais remplacer le
modèle actif directement. Le rapport d'évaluation doit contenir :

- exact-match, précision, rappel et F1 par champ ;
- métriques séparées par client, famille de document, scan/PDF natif et nombre
  de pages ;
- exactitude du type de document et qualité de sa confiance ;
- exactitude des références, quantités, unités, prix et nombre de lignes ;
- qualité des adresses et de leurs rôles ;
- faux positifs, faux rejets et champs inventés ;
- latence médiane, P95 et taux d'erreur ;
- comparaison stricte avec le modèle principal actif.

Les seuils de promotion doivent être écrits avant de consulter les résultats du
holdout. Un gain moyen ne peut pas compenser une régression sur un champ
critique.

**Critère de sortie :** rapport reproductible, candidat identifié par version
et décision humaine `REJECTED` ou `APPROVED_FOR_SHADOW`.

## Porte 3 — Shadow dans GenieCommande

Le candidat approuvé est exécuté en parallèle sans modifier le dossier métier,
l'EDIFACT ou l'envoi SAP. La comparaison doit relier :

- PDF et empreinte SHA-256 ;
- version du runtime, du modèle principal et du candidat ;
- résultat principal, résultat shadow et correction humaine finale ;
- écarts par champ et par ligne ;
- temps d'exécution et erreur technique.

La campagne recommandée couvre au moins 100 commandes récentes et les clients
visés par la première vague de déploiement. Toute régression critique replace le
candidat au statut `REJECTED`.

**Critère de sortie :** résultats shadow stables et décision explicite de
promotion ou d'abandon.

## Porte 4 — Version, promotion et rollback

Avant la VM :

1. transformer `Unreleased` en une version publiée, probablement `5.20.0` ;
2. mettre à jour `jin_runtime.__version__` et la documentation ;
3. taguer le commit et construire des images Docker portant ce tag immuable ;
4. enregistrer modèle, métriques, dataset et commit dans un registre ;
5. conserver le modèle principal précédent ;
6. tester le retour arrière sans reconstruire un modèle.

La promotion automatique reste interdite tant que le registre, la comparaison
et le rollback ne sont pas disponibles de bout en bout.

## Porte 5 — Exploitation VM et sauvegarde hors VM

La procédure détaillée est dans
[Sauvegarde et restauration](BACKUP_AND_RESTORE.md). Les actions restantes sont :

- créer un conteneur Azure Blob privé ;
- activer versionnement, suppression réversible et rétention ;
- attribuer `Storage Blob Data Contributor` à l'identité managée de la VM ;
- renseigner `/etc/jin-model-backup.env` ;
- installer et activer `jin-model-backup.timer` ;
- vérifier le premier blob et son SHA-256 ;
- télécharger ce blob dans un environnement isolé et réussir une restauration ;
- définir une alerte si le dernier succès devient trop ancien.

**Critère de sortie :** sauvegarde réellement située hors VM et exercice de
restauration daté et concluant.

## Porte 6 — Sécurité et exploitation quotidienne

Avant ouverture aux utilisateurs :

- remplacer tout mot de passe temporaire, notamment `admin123` ;
- créer un compte nominatif par utilisateur et limiter les rôles ;
- retirer les jetons visibles des notebooks et utiliser un coffre de secrets ;
- placer JIN derrière HTTPS, VPN ou reverse proxy approuvé ;
- vérifier les cookies sécurisés et les origines MCP autorisées ;
- tester redémarrage Docker, redémarrage VM et indisponibilité temporaire du
  stockage ;
- documenter qui peut valider, entraîner, promouvoir et restaurer.

## Monitoring Power BI

Power BI peut porter le suivi métier et qualité. Le jeu de données doit exposer
au minimum :

- date, document, client, famille et version JIN ;
- durée, statut technique et motif d'échec ;
- confiance et correction finale par champ ;
- nombre de lignes extraites, corrigées, ajoutées ou supprimées ;
- taux de revue, taux de correction et taux de rejet ;
- résultat shadow/principal et décision de promotion ;
- résultat ultérieur dans GenieCommande et SAP lorsque disponible ;
- date de la dernière sauvegarde hors VM réussie.

Les PDF, textes OCR, adresses complètes et secrets ne doivent pas être copiés
dans Power BI. Les identifiants techniques doivent permettre de revenir au
dossier autorisé sans dupliquer les données sensibles.

## Déploiement progressif dans GenieCommande

L'ordre recommandé reste :

1. shadow sans impact ;
2. résultat JIN visible en revue humaine ;
3. activation pour quelques clients et Ship-to maîtrisés ;
4. extension client par client sur la base des métriques ;
5. envoi SAP automatique uniquement si les règles GenieCommande, les seuils
   qualité et l'autorisation client/Ship-to sont tous satisfaits.

Un succès sur un Sold-to n'autorise pas automatiquement tous ses Ship-to. La
configuration GenieCommande demeure l'autorité finale de l'envoi SAP.

## Prochaine action recommandée

La prochaine action n'est pas un nouvel entraînement immédiat. Il faut d'abord
valider dans JIN Studio un premier lot varié de documents, réserver le holdout,
puis figer le dataset v1. C'est cette vérité terrain qui permettra de mesurer
factuellement si le prochain JIN est meilleur.
