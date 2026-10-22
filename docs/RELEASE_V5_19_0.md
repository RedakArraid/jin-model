# JIN runtime 5.19.0 — identité JIN Studio

La version `5.19.0-studio-identity` remplace le jeton partagé de l'interface
JIN Studio par des comptes nominatifs. Les utilisateurs disposent de rôles
cumulables pour consulter, annoter, valider, entraîner ou administrer.

Les sessions sont persistées côté serveur et transmises par cookie `HttpOnly`.
Les opérations d'écriture exigent une preuve CSRF. Les mots de passe sont
hachés avec `scrypt`, les comptes sont temporairement verrouillés après cinq
échecs et toutes leurs sessions sont révoquées lors d'une désactivation ou
d'une réinitialisation du mot de passe.

L'auteur d'une annotation est désormais imposé par l'API à partir de la
session. Les connexions, validations, créations de datasets, lancements
d'entraînement et changements d'utilisateurs sont conservés dans le journal
d'audit.

Le jeton `JIN_STUDIO_SERVICE_TOKEN` reste disponible exclusivement pour les
intégrations serveur-à-serveur. Il n'accorde aucun droit d'administration.

Voir [JIN Studio](JIN_STUDIO.md) pour la création du premier administrateur et
la matrice des rôles.
