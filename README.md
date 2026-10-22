# Catalogue Pokémon

Site statique (GitHub Pages) protégé par mot de passe : ta collection avec prix du jour et courbes, une page
Recherche (acheter en loose, faire grader en CCC) et une page « À surveiller » (cartes dont le prix monte).

## Sources de prix

- **Prix des cartes** : uniquement des prix français. Cote Cardmarket de la version française en Near Mint,
  relevée sur [Pokoya](https://pokoya.app) (`scripts/pokoya_prices.py`). Un prix saisi à la main passe avant.
  Sans prix FR, la carte n'a pas de prix (jamais de prix toutes langues).
- **Prix CCC** : ventes réussies sur Graded Card Center (`scripts/gcc_prices.py`), sans estimation.
- **Identification des cartes** (images, extension, lien Cardmarket) : API publique TCGdex.

## Fichiers

- `index.html` : tout le site (aucun serveur ni base de données).
- `data/vault.json` : ta collection et son historique, chiffrés (AES-256-GCM, clé dérivée du mot de passe).
- `data/research.json`, `data/watch.json` : Recherche et cartes à surveiller, chiffrées de la même façon.
- `data/auth.json`, `data/keyring.json` : connexion (mot de passe modifiable, code de récupération) et comptes invités.
- `scripts/update_prices.py` : met à jour la collection (workflow « Mise à jour quotidienne », 6 h UTC).
- `scripts/surveiller.py` puis `scripts/research.py` : À surveiller et Recherche (workflow « Recherche quotidienne »,
  4 h 15 UTC). L'historique complet des prix FR est gardé chiffré dans le cache GitHub Actions.

## Sécurité

- Rien n'est en clair dans le dépôt : tout est chiffré avec le secret GitHub `SITE_PASSWORD`.
- La clé d'accès GitHub collée dans le site reste dans ton navigateur, chiffrée avec ton mot de passe.
- Verrouillage automatique après inactivité (Réglages), freinage des essais de mot de passe, site non affichable
  dans un cadre, politique de sécurité du contenu stricte (aucun script externe).
- Les actions GitHub et la bibliothèque de chiffrement sont figées à une version précise.

## Raccourcis

Touche `?` sur le site pour la liste : `1` `2` `3` pour changer de page, `a` ajouter, `/` chercher,
`e` évolution, `g` grille/liste, `m` cacher les montants, `l` verrouiller, `Échap` fermer.
Adresses directes à mettre en favori : `#recherche`, `#surveiller`, `#ajouter`, `#evolution`.

## Lancer à la main

Onglet **Actions** du dépôt → « Mise à jour quotidienne » ou « Recherche quotidienne » → **Run workflow**.
